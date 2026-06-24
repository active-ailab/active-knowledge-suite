"""Lightweight persistent observability metrics and health summaries."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from active_knowledge_server.config.loader import ResolvedConfig
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.config.workdir import WorkdirLayout
from active_knowledge_server.indexing import IncrementalIndexResult
from active_knowledge_server.indexing.jobs import (
    RUNNING_JOB_STATUSES,
    SQLiteJobStore,
    list_task_states,
)
from active_knowledge_server.models.responses import QueryResult
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    configured_sqlite_paths,
)

OBSERVABILITY_SCHEMA_VERSION: Final = "observability.v1"
OBSERVABILITY_STATUS_SCHEMA_VERSION: Final = "observability_status.v1"
_RECENT_QUERY_LIMIT: Final = 20
_RECENT_INDEX_LIMIT: Final = 20
HealthState = Literal["ok", "degraded", "blocked", "missing"]


@dataclass(frozen=True)
class ObservabilityStore:
    """Persist small local observability summaries in the workdir logs directory."""

    path: Path

    @classmethod
    def from_layout(cls, layout: WorkdirLayout) -> ObservabilityStore:
        """Build the store for one resolved workdir layout."""

        return cls(layout.local_logs_dir / "observability.json")

    def load_snapshot(self) -> dict[str, object]:
        """Load the current persisted snapshot or return an empty one."""

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _default_snapshot()
        except (OSError, json.JSONDecodeError):
            return _default_snapshot()
        if not isinstance(payload, Mapping):
            return _default_snapshot()
        snapshot = _default_snapshot()
        snapshot["updated_at"] = _text(payload.get("updated_at")) or snapshot["updated_at"]
        snapshot["metrics"] = _merge_metrics(payload.get("metrics"))
        snapshot["recent_queries"] = _recent_records(payload.get("recent_queries"))
        snapshot["recent_index_runs"] = _recent_records(payload.get("recent_index_runs"))
        return snapshot

    def save_snapshot(self, snapshot: Mapping[str, object]) -> None:
        """Atomically write one snapshot payload."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        materialized = dict(snapshot)
        materialized["updated_at"] = utc_timestamp()
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(materialized, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def record_query_run(
        self,
        *,
        tool_name: str,
        result: QueryResult,
        latency_seconds: float,
    ) -> None:
        """Update counters and recent query samples for one query execution."""

        snapshot = self.load_snapshot()
        metrics = _metrics_dict(snapshot)
        _observe_histogram(metrics, "query_latency_seconds", latency_seconds)
        metrics["retrieval_candidates_total"] = int(metrics["retrieval_candidates_total"]) + (
            _retrieval_candidate_count(result)
        )
        metrics["evidence_items_returned"] = int(metrics["evidence_items_returned"]) + len(
            result.evidence_refs
        )
        metrics["warnings_total"] = int(metrics["warnings_total"]) + len(result.warnings)

        recent_queries = _recent_records(snapshot.get("recent_queries"))
        recent_queries.insert(
            0,
            {
                "timestamp": utc_timestamp(),
                "tool_name": tool_name,
                "result_status": result.result_status,
                "latency_seconds": round(latency_seconds, 6),
                "retrieval_candidates": _retrieval_candidate_count(result),
                "evidence_items_returned": len(result.evidence_refs),
                "warnings_total": len(result.warnings),
            },
        )
        snapshot["recent_queries"] = recent_queries[:_RECENT_QUERY_LIMIT]
        self.save_snapshot(snapshot)

    def record_index_run(
        self,
        *,
        result: IncrementalIndexResult | Mapping[str, object],
        duration_seconds: float,
        job_id: str | None,
    ) -> None:
        """Update counters and recent index samples for one completed index execution."""

        files_total, files_failed, warnings_total, result_status, snapshot_id = _index_run_metrics(
            result
        )
        snapshot = self.load_snapshot()
        metrics = _metrics_dict(snapshot)
        metrics["index_files_total"] = int(metrics["index_files_total"]) + files_total
        metrics["index_files_failed"] = int(metrics["index_files_failed"]) + files_failed
        _observe_histogram(metrics, "index_duration_seconds", duration_seconds)

        recent_runs = _recent_records(snapshot.get("recent_index_runs"))
        recent_runs.insert(
            0,
            {
                "timestamp": utc_timestamp(),
                "job_id": job_id,
                "snapshot_id": snapshot_id,
                "result_status": result_status,
                "duration_seconds": round(duration_seconds, 6),
                "files_total": files_total,
                "files_failed": files_failed,
                "warnings_total": warnings_total,
            },
        )
        snapshot["recent_index_runs"] = recent_runs[:_RECENT_INDEX_LIMIT]
        self.save_snapshot(snapshot)

    def collect_status(
        self,
        *,
        config: ActiveKnowledgeConfig,
        layout: WorkdirLayout,
        cwd: Path,
        index_status: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return the current observability status with refreshed runtime gauges."""

        snapshot = self.load_snapshot()
        metrics = _metrics_dict(snapshot)
        metrics["embedding_queue_size"] = _embedding_queue_size(config=config, cwd=cwd)
        metrics["storage_size_bytes"] = _storage_size_bytes(layout)
        self.save_snapshot(snapshot)

        latest_query = _first_record(snapshot.get("recent_queries"))
        latest_index = _first_record(snapshot.get("recent_index_runs"))
        health_summary = _build_health_summary(
            metrics=metrics,
            latest_query=latest_query,
            latest_index=latest_index,
            live_index_status=index_status,
        )
        return {
            "schema_version": OBSERVABILITY_STATUS_SCHEMA_VERSION,
            "metrics": dict(metrics),
            "health_summary": health_summary,
            "artifacts": {
                "snapshot_path": str(self.path),
                "snapshot_exists": self.path.exists(),
            },
        }


def observability_store_for_resolved(resolved: ResolvedConfig) -> ObservabilityStore:
    """Build the observability store directly from a resolved config."""

    from active_knowledge_server.config.workdir import layout_from_config

    return ObservabilityStore.from_layout(layout_from_config(resolved, cwd=Path.cwd()))


def utc_timestamp() -> str:
    """Return the current UTC timestamp in a stable JSON-friendly format."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _default_snapshot() -> dict[str, object]:
    return {
        "schema_version": OBSERVABILITY_SCHEMA_VERSION,
        "updated_at": utc_timestamp(),
        "metrics": {
            "index_files_total": 0,
            "index_files_failed": 0,
            "index_duration_seconds": _default_histogram(),
            "query_latency_seconds": _default_histogram(),
            "retrieval_candidates_total": 0,
            "evidence_items_returned": 0,
            "warnings_total": 0,
            "embedding_queue_size": 0,
            "storage_size_bytes": 0,
        },
        "recent_queries": [],
        "recent_index_runs": [],
    }


def _default_histogram() -> dict[str, object]:
    return {
        "count": 0,
        "sum": 0.0,
        "min": None,
        "max": None,
        "last": None,
    }


def _metrics_dict(snapshot: Mapping[str, object]) -> dict[str, object]:
    metrics = snapshot.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    merged = _merge_metrics(metrics)
    cast(dict[str, object], snapshot)["metrics"] = merged
    return merged


def _merge_metrics(value: object) -> dict[str, object]:
    merged = cast(dict[str, object], _default_snapshot()["metrics"])
    if not isinstance(value, Mapping):
        return merged
    for key in (
        "index_files_total",
        "index_files_failed",
        "retrieval_candidates_total",
        "evidence_items_returned",
        "warnings_total",
        "embedding_queue_size",
        "storage_size_bytes",
    ):
        merged[key] = _int(value.get(key))
    for key in ("index_duration_seconds", "query_latency_seconds"):
        merged[key] = _merge_histogram(value.get(key))
    return merged


def _merge_histogram(value: object) -> dict[str, object]:
    histogram = _default_histogram()
    if not isinstance(value, Mapping):
        return histogram
    histogram["count"] = _int(value.get("count"))
    histogram["sum"] = round(_float(value.get("sum")), 6)
    histogram["min"] = _float_or_none(value.get("min"))
    histogram["max"] = _float_or_none(value.get("max"))
    histogram["last"] = _float_or_none(value.get("last"))
    return histogram


def _observe_histogram(metrics: dict[str, object], key: str, value: float) -> None:
    observed = round(max(value, 0.0), 6)
    histogram = _merge_histogram(metrics.get(key))
    histogram["count"] = int(histogram["count"]) + 1
    histogram["sum"] = round(float(histogram["sum"]) + observed, 6)
    current_min = _float_or_none(histogram["min"])
    current_max = _float_or_none(histogram["max"])
    histogram["min"] = observed if current_min is None else round(min(current_min, observed), 6)
    histogram["max"] = observed if current_max is None else round(max(current_max, observed), 6)
    histogram["last"] = observed
    metrics[key] = histogram


def _recent_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _first_record(value: object) -> dict[str, object] | None:
    records = _recent_records(value)
    return None if not records else records[0]


def _build_health_summary(
    *,
    metrics: Mapping[str, object],
    latest_query: Mapping[str, object] | None,
    latest_index: Mapping[str, object] | None,
    live_index_status: Mapping[str, object] | None,
) -> dict[str, object]:
    query_health = _query_health(latest_query)
    index_health = _index_health(latest_index, live_index_status=live_index_status)
    overall = _overall_health_status(query_health["health_state"], index_health["health_state"])
    return {
        "generated_at": utc_timestamp(),
        "status": overall,
        "query": query_health,
        "index": index_health,
        "gauges": {
            "embedding_queue_size": int(metrics["embedding_queue_size"]),
            "storage_size_bytes": int(metrics["storage_size_bytes"]),
        },
    }


def _query_health(latest_query: Mapping[str, object] | None) -> dict[str, object]:
    if latest_query is None:
        return {
            "health_state": "missing",
            "message": "No query observations have been recorded yet.",
            "latest": None,
        }
    result_status = _text(latest_query.get("result_status")) or "unknown"
    warnings_total = _int(latest_query.get("warnings_total"))
    health_state: HealthState = "ok"
    if result_status in {"error", "blocked"}:
        health_state = "blocked"
    elif result_status == "partial_ready" or warnings_total > 0:
        health_state = "degraded"
    return {
        "health_state": health_state,
        "message": _query_health_message(health_state, result_status=result_status),
        "latest": dict(latest_query),
    }


def _query_health_message(health_state: HealthState, *, result_status: str) -> str:
    if health_state == "blocked":
        return f"The most recent query failed with result_status={result_status}."
    if health_state == "degraded":
        return f"The most recent query completed with result_status={result_status} and warnings."
    if health_state == "missing":
        return "No query observations have been recorded yet."
    return f"The most recent query completed successfully with result_status={result_status}."


def _index_health(
    latest_index: Mapping[str, object] | None,
    *,
    live_index_status: Mapping[str, object] | None,
) -> dict[str, object]:
    if latest_index is None and live_index_status is None:
        return {
            "health_state": "missing",
            "message": "No index observations have been recorded yet.",
            "latest": None,
            "current_result_status": None,
        }

    current_result_status = None if live_index_status is None else _text(
        live_index_status.get("result_status")
    )
    if current_result_status is None and latest_index is not None:
        current_result_status = _text(latest_index.get("result_status"))
    assert current_result_status is not None

    if current_result_status in {"failed", "blocked"}:
        health_state: HealthState = "blocked"
    elif current_result_status == "partial_ready":
        health_state = "degraded"
    elif current_result_status == "missing":
        health_state = "missing"
    else:
        health_state = "ok"

    message = (
        _text(live_index_status.get("message"))
        if isinstance(live_index_status, Mapping)
        else None
    ) or _index_health_message(health_state, result_status=current_result_status)
    return {
        "health_state": health_state,
        "message": message,
        "latest": None if latest_index is None else dict(latest_index),
        "current_result_status": current_result_status,
    }


def _index_health_message(health_state: HealthState, *, result_status: str) -> str:
    if health_state == "blocked":
        return f"The most recent index state is unhealthy with result_status={result_status}."
    if health_state == "degraded":
        return f"The index is serving with degraded coverage (result_status={result_status})."
    if health_state == "missing":
        return "No indexed snapshot is available yet."
    return f"The index is healthy with result_status={result_status}."


def _overall_health_status(query_state: HealthState, index_state: HealthState) -> HealthState:
    ordered = (query_state, index_state)
    if "blocked" in ordered:
        return "blocked"
    if "degraded" in ordered:
        return "degraded"
    if "missing" in ordered:
        return "missing"
    return "ok"


def _retrieval_candidate_count(result: QueryResult) -> int:
    diagnostics = result.diagnostics
    if isinstance(diagnostics, Mapping):
        retrieval_trace = diagnostics.get("retrieval_trace")
        if isinstance(retrieval_trace, Mapping):
            ranked = retrieval_trace.get("ranked_candidates")
            if isinstance(ranked, Sequence) and not isinstance(ranked, str):
                return len(ranked)
    return max(len(result.items), len(result.candidates))


def _index_run_metrics(
    result: IncrementalIndexResult | Mapping[str, object],
) -> tuple[int, int, int, str, str | None]:
    if isinstance(result, IncrementalIndexResult):
        return (
            _incremental_files_total(result),
            _warning_paths(result.warnings),
            len(result.warnings),
            result.result_status,
            result.snapshot_id,
        )

    result_mapping = dict(result)
    code_file_count = _int(result_mapping.get("code_file_count"))
    doc_file_count = _int(result_mapping.get("doc_file_count"))
    return (
        code_file_count + doc_file_count,
        0,
        0,
        _text(result_mapping.get("result_status")) or "ready",
        _text(result_mapping.get("snapshot_id")),
    )


def _incremental_files_total(result: IncrementalIndexResult) -> int:
    plan = result.plan
    if plan.reindex_all_code:
        code_total = len(plan.current_state.code_files) + len(plan.deleted_code_paths)
    else:
        code_total = len(set((*plan.changed_code_paths, *plan.deleted_code_paths)))
    if plan.reindex_all_docs:
        doc_total = len(plan.current_state.doc_files) + len(plan.deleted_doc_paths)
    else:
        doc_total = len(set((*plan.changed_doc_paths, *plan.deleted_doc_paths)))
    return code_total + doc_total


def _warning_paths(warnings: Sequence[Any]) -> int:
    paths: set[str] = set()
    for warning in warnings:
        details = getattr(warning, "details", {})
        if not isinstance(details, Mapping):
            continue
        path = _text(details.get("path"))
        if path:
            paths.add(path)
        raw_paths = details.get("paths")
        if isinstance(raw_paths, Sequence) and not isinstance(raw_paths, str):
            for item in raw_paths:
                text = _text(item)
                if text:
                    paths.add(text)
    return len(paths)


def _embedding_queue_size(*, config: ActiveKnowledgeConfig, cwd: Path) -> int:
    jobs_path = configured_sqlite_paths(config, cwd=cwd)["jobs"]
    if not jobs_path.exists():
        return 0
    adapter = SQLiteStorageAdapter.from_config(config, cwd=cwd)
    reader = adapter.reader()
    store = SQLiteJobStore(jobs_path)
    try:
        for job in reader.iter_jobs():
            if job.job_type != "index" or job.status not in RUNNING_JOB_STATUSES:
                continue
            tasks_by_phase = job.metadata.get("tasks_by_phase")
            total = 0
            if isinstance(tasks_by_phase, Mapping):
                total = _int(tasks_by_phase.get("vectors_apply"))
            applied = len(
                list_task_states(
                    store,
                    job.job_id,
                    phase="vectors_apply",
                    status="applied",
                )
            )
            return max(total - applied, 0)
    finally:
        adapter.close()
    return 0


def _storage_size_bytes(layout: WorkdirLayout) -> int:
    paths = (
        layout.baseline_dir / "db",
        layout.baseline_dir / "vectors",
        layout.baseline_dir / "artifacts",
        layout.local_db_dir,
        layout.local_vectors_dir,
        layout.local_artifacts_dir,
        layout.local_cache_dir,
    )
    total = 0
    for path in paths:
        total += _path_size(path)
    if layout.baseline_manifest_path.exists():
        total += _path_size(layout.baseline_manifest_path)
    return total


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        if not path.exists():
            return 0
        return sum(
            child.stat().st_size
            for child in path.rglob("*")
            if child.is_file()
        )
    except OSError:
        return 0


def _text(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _float(value: object) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    numeric = _float(value)
    return round(numeric, 6)
