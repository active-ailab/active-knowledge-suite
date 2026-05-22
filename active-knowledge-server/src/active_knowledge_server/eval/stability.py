"""Deterministic stability benchmark used by the E7-04 gate."""

from __future__ import annotations

import os
import platform
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from itertools import cycle
from pathlib import Path
from tempfile import TemporaryDirectory

from active_knowledge_server.eval.benchmark import (
	_BenchmarkProfileCollector,
	_build_indexed_fixture,
	_resolve_benchmark_config,
)
from active_knowledge_server.eval.cases import EvalCase, EvalCaseSuite
from active_knowledge_server.indexing.jobs import (
	INDEX_JOB_LOCK_ID,
	IndexJobRunner,
	SQLiteJobStore,
)
from active_knowledge_server.models.query import QueryRequest
from active_knowledge_server.models.responses import QueryResult, Warning
from active_knowledge_server.query import QueryService
from active_knowledge_server.query.retrievers import (
	FullTextRetriever,
	GraphRetriever,
	SymbolRetriever,
	VectorSearchRequest,
	VectorSearchResult,
)
from active_knowledge_server.query.router import QueryRouter
from active_knowledge_server.storage.sqlite_store import (
	LATEST_SQLITE_SCHEMA_VERSION,
	migrate_sqlite_store,
)


class StabilityBenchmark:
	"""Synthetic indexed corpus plus repeatable E7-04 stability probes."""

	def __init__(
		self,
		*,
		soak_seconds: int = 60,
		mixed_query_count: int = 500,
		readonly_workers: int = 8,
		readonly_query_count: int = 64,
		readonly_timeout_seconds: float = 5.0,
	) -> None:
		self._soak_seconds = max(int(soak_seconds), 1)
		self._mixed_query_count = max(int(mixed_query_count), 1)
		self._readonly_workers = max(int(readonly_workers), 1)
		self._readonly_query_count = max(int(readonly_query_count), self._readonly_workers)
		self._readonly_timeout_seconds = max(float(readonly_timeout_seconds), 0.1)
		self._tmpdir = TemporaryDirectory(prefix="active-kb-stability-")
		self._root = Path(self._tmpdir.name)
		self._resolved = _resolve_benchmark_config(
			self._root,
			overrides={"server": {"transport": "streamable-http"}},
		)
		self._config = self._resolved.model
		self._workspace_root = Path(self._config.project.workspace_root)
		self._docs_root = Path(self._config.runtime.source_docs_root)
		self._adapter = _build_indexed_fixture(self._root, self._config)
		self._router = QueryRouter.from_config(
			self._config,
			cwd=self._root,
			profile_collector=_BenchmarkProfileCollector(),
		)
		self._query_service = QueryService(
			self._config,
			router=self._router,
			metadata_adapter=self._adapter,
			symbol_retriever=SymbolRetriever.from_storage(self._adapter),
			fulltext_retriever=FullTextRetriever.from_storage(self._adapter),
			graph_retriever=GraphRetriever.from_config(
				self._config,
				metadata_adapter=self._adapter,
			),
		)
		self._partial_ready_service = QueryService(
			self._config,
			router=self._router,
			metadata_adapter=self._adapter,
			fulltext_retriever=FullTextRetriever.from_storage(self._adapter),
			vector_retriever=_DegradedVectorRetriever(),
		)

	def measure_suite(self, suite: EvalCaseSuite) -> dict[str, dict[str, object]]:
		query_cases = tuple(
			case for case in suite.cases if case.execution_mode == "query_quality"
		)
		if not query_cases:
			raise ValueError("stability benchmark requires query_quality cases")
		return {
			"mixed_query": self._measure_mixed_queries(query_cases),
			"soak": self._measure_soak(query_cases),
			"index_recovery": self._measure_index_recovery(),
			"migration_idempotence": self._measure_migration_idempotence(),
			"partial_ready_query": self._measure_partial_ready_query(),
			"readonly_concurrency": self._measure_readonly_concurrency(query_cases),
		}

	def environment(self) -> dict[str, object]:
		return {
			"platform": platform.platform(),
			"python_version": platform.python_version(),
			"cpu_count": os.cpu_count() or 1,
			"transport": self._resolved.model.server.transport,
			"readonly_workers": self._readonly_workers,
			"readonly_timeout_seconds": round(self._readonly_timeout_seconds, 6),
		}

	def dataset_scale(self) -> dict[str, object]:
		return {
			"workspace_files": _count_files(self._workspace_root),
			"workspace_bytes": _total_size(self._workspace_root),
			"source_docs_files": _count_files(self._docs_root),
			"source_docs_bytes": _total_size(self._docs_root),
		}

	def close(self) -> None:
		self._tmpdir.cleanup()

	def _measure_mixed_queries(
		self,
		query_cases: tuple[EvalCase, ...],
	) -> dict[str, object]:
		success_count = 0
		eligible_runs = 0
		exception_count = 0
		excluded_status_counts: dict[str, int] = {}
		failures: list[dict[str, object]] = []
		for run_index, case in zip(range(self._mixed_query_count), cycle(query_cases), strict=False):
			try:
				result = self._search(case)
				schema_compliant = self._schema_compliant(result)
				if result.result_status in {"blocked", "zero_result"}:
					excluded_status_counts[result.result_status] = (
						excluded_status_counts.get(result.result_status, 0) + 1
					)
					continue
				eligible_runs += 1
				if schema_compliant and result.result_status != "error":
					success_count += 1
					continue
				failures.append(
					{
						"run_index": run_index,
						"case_id": case.case_id,
						"result_status": result.result_status,
						"schema_compliant": schema_compliant,
					}
				)
			except Exception as exc:  # noqa: BLE001 - benchmark must record query exceptions.
				exception_count += 1
				eligible_runs += 1
				failures.append(
					{
						"run_index": run_index,
						"case_id": case.case_id,
						"error": str(exc),
					}
				)
		success_rate = 1.0 if eligible_runs == 0 else success_count / eligible_runs
		return {
			"configured_runs": self._mixed_query_count,
			"total_runs": self._mixed_query_count,
			"eligible_runs": eligible_runs,
			"success_count": success_count,
			"success_rate": round(success_rate, 6),
			"exception_count": exception_count,
			"excluded_status_counts": dict(sorted(excluded_status_counts.items())),
			"failure_count": len(failures),
			"failures": failures[:20],
		}

	def _measure_soak(self, query_cases: tuple[EvalCase, ...]) -> dict[str, object]:
		unhandled_exceptions = 0
		iterations = 0
		started = time.perf_counter()
		query_cycle = cycle(query_cases)
		while iterations == 0 or (time.perf_counter() - started) < self._soak_seconds:
			case = next(query_cycle)
			try:
				self._search(case)
			except Exception:  # noqa: BLE001 - soak gate only counts unhandled exceptions.
				unhandled_exceptions += 1
			iterations += 1
		actual_seconds = time.perf_counter() - started
		return {
			"configured_seconds": self._soak_seconds,
			"actual_seconds": round(actual_seconds, 6),
			"iterations": iterations,
			"unhandled_exceptions": unhandled_exceptions,
		}

	def _measure_index_recovery(self) -> dict[str, object]:
		jobs_path = Path(self._config.storage.jobs.path)
		store = SQLiteJobStore(jobs_path)
		job = store.create_job(job_id="bench-index-crash")
		runner = IndexJobRunner(store)

		def _interrupt(_path: str) -> None:
			raise KeyboardInterrupt("simulated interrupt")

		try:
			runner.run_files(job.job_id, ("components/health/main.c",), _interrupt)
		except KeyboardInterrupt:
			pass

		failed = store.transition_job(
			job.job_id,
			"failed",
			error_summary="simulated interrupt",
		)
		resume = store.resume_job(job.job_id)
		retry = store.retry_job(job.job_id)

		stale_owner = store.create_job(job_id="bench-index-stale-owner")
		reacquire_owner = store.create_job(job_id="bench-index-reacquire-owner")
		store.acquire_lock(
			INDEX_JOB_LOCK_ID,
			owner_job_id=stale_owner.job_id,
			ttl_seconds=-1,
		)
		reacquired = store.acquire_lock(
			INDEX_JOB_LOCK_ID,
			owner_job_id=reacquire_owner.job_id,
		)
		store.release_lock(INDEX_JOB_LOCK_ID, owner_job_id=reacquire_owner.job_id)
		final_lock = store.get_lock(INDEX_JOB_LOCK_ID)

		return {
			"checkpoint_resume_available": bool(resume.checkpoints.get("discovered_files")),
			"failed_state_recorded": failed.status == "failed",
			"retryable": retry.status == "pending",
			"stale_lock_reacquired": reacquired.owner_job_id == reacquire_owner.job_id,
			"lock_cleared": final_lock is None,
		}

	def _measure_migration_idempotence(self) -> dict[str, object]:
		path = self._root / "stability" / "overlay-idempotence.db"
		first = migrate_sqlite_store(path, target="overlay_metadata")
		second = migrate_sqlite_store(path, target="overlay_metadata")
		third = migrate_sqlite_store(path, target="overlay_metadata")
		return {
			"schema_version": LATEST_SQLITE_SCHEMA_VERSION,
			"applied_counts": [
				len(first.applied_migration_ids),
				len(second.applied_migration_ids),
				len(third.applied_migration_ids),
			],
			"history_count": _migration_history_count(path),
		}

	def _measure_partial_ready_query(self) -> dict[str, object]:
		result = self._partial_ready_service.search(
			QueryRequest(
				query="sensor open",
				domain="api",
				view="evidence",
				granularity="doc_section",
				profile_id="watch",
				snapshot_id="current",
				caller_tool="client",
				client_context={},
			)
		)
		return {
			"result_status": result.result_status,
			"warning_codes": [warning.code for warning in result.warnings],
			"schema_compliant": self._schema_compliant(result),
			"item_count": len(result.items),
		}

	def _measure_readonly_concurrency(
		self,
		query_cases: tuple[EvalCase, ...],
	) -> dict[str, object]:
		failures: list[dict[str, object]] = []
		latencies: list[float] = []
		timed_out = 0
		query_cycle = cycle(query_cases)
		cases = tuple(next(query_cycle) for _ in range(self._readonly_query_count))
		completed_queries = 0
		with ThreadPoolExecutor(max_workers=self._readonly_workers) as executor:
			futures = {
				executor.submit(self._timed_search, case): index
				for index, case in enumerate(cases)
			}
			try:
				completed = as_completed(
					futures,
					timeout=self._readonly_timeout_seconds * self._readonly_query_count,
				)
				for future in completed:
					index = futures[future]
					try:
						case_id, latency_seconds, result_status = future.result()
						completed_queries += 1
						latencies.append(latency_seconds)
						if result_status == "error":
							failures.append(
								{
									"query_index": index,
									"case_id": case_id,
									"result_status": result_status,
								}
							)
					except Exception as exc:  # noqa: BLE001 - capture worker failures in the gate.
						failures.append(
							{
								"query_index": index,
								"error": str(exc),
							}
						)
			except TimeoutError:
				pending = [
					index
					for future, index in futures.items()
					if not future.done()
				]
				timed_out = len(pending)
				for index in pending:
					failures.append(
						{
							"query_index": index,
							"error": "timeout",
						}
					)
		return {
			"workers": self._readonly_workers,
			"total_queries": self._readonly_query_count,
			"completed_queries": completed_queries,
			"timeout_count": timed_out,
			"failure_count": len(failures),
			"max_latency_seconds": round(max(latencies, default=0.0), 6),
			"mean_latency_seconds": round(
				sum(latencies) / len(latencies),
				6,
			)
			if latencies
			else 0.0,
			"failures": failures[:20],
		}

	def _timed_search(self, case: EvalCase) -> tuple[str, float, str]:
		started = time.perf_counter()
		result = self._search(case)
		return case.case_id, time.perf_counter() - started, result.result_status

	def _search(self, case: EvalCase) -> QueryResult:
		return self._query_service.search(
			QueryRequest.model_validate(case.request.to_dict())
		)

	def _schema_compliant(self, result: QueryResult) -> bool:
		try:
			QueryResult.model_validate(result.to_dict())
		except Exception:
			return False
		return True


class _DegradedVectorRetriever:
	"""Vector retriever stub that injects partial-ready index warnings."""

	def search(self, request: VectorSearchRequest) -> VectorSearchResult:
		return VectorSearchResult(
			request=request,
			warnings=(
				Warning(
					level="degraded",
					code="index.partial_ready",
					message="Widget and profile indexes are not ready yet.",
					details={
						"ready_sources": ["knowledge-sources/api"],
						"missing_sources": ["knowledge-sources/widgets"],
						"failed_jobs": ["job-17"],
						"degradation_chain": ["skip_widgets", "fts_only"],
					},
					actionable=True,
					suggested_action="Repair the failed job and rebuild the missing sources.",
				),
			),
		)


def _migration_history_count(path: Path) -> int:
	from sqlite3 import connect

	with connect(path) as connection:
		row = connection.execute("SELECT COUNT(*) FROM migration_history").fetchone()
	assert row is not None
	return int(row[0])


def _count_files(root: Path) -> int:
	return sum(1 for path in root.rglob("*") if path.is_file())


def _total_size(root: Path) -> int:
	return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
