"""Deterministic synthetic benchmark used by the E7-03 performance gate."""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from active_knowledge_server.config.workdir import initialize_workdir
from active_knowledge_server.eval.benchmark import _build_indexed_fixture, _resolve_benchmark_config
from active_knowledge_server.eval.cases import EvalCase, EvalCaseSuite
from active_knowledge_server.eval.metrics import PerformanceProbeObservation
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID
from active_knowledge_server.indexing.pipeline import IncrementalIndexPipeline
from active_knowledge_server.models.responses import QueryResult
from active_knowledge_server.server import build_server_app


class PerformanceBenchmark:
    """Synthetic corpus plus real runtime probes used by the performance gate."""

    def __init__(
        self,
        *,
        sample_count: int = 5,
        warmup_runs: int = 1,
        incremental_file_count: int = 100,
    ) -> None:
        self._sample_count = max(sample_count, 1)
        self._warmup_runs = max(warmup_runs, 0)
        self._incremental_file_count = max(incremental_file_count, 1)
        self._tmpdir = TemporaryDirectory(prefix="active-kb-performance-")
        self._root = Path(self._tmpdir.name)
        self._resolved = _resolve_benchmark_config(
            self._root,
            overrides={"server": {"transport": "streamable-http"}},
        )
        self._config = self._resolved.model
        self._workspace_root = Path(self._config.project.workspace_root)
        self._docs_root = Path(self._config.runtime.source_docs_root)
        self._metadata_adapter = _build_indexed_fixture(
            self._root,
            self._config,
            extra_workspace_files=self._incremental_file_count,
        )
        self._write_baseline_manifest()
        self._app = build_server_app(self._resolved, cwd=self._root)
        self._tool_handlers = {
            tool.name: tool.handler for tool in self._app.inventory.tools
        }
        self._incremental_pipeline = IncrementalIndexPipeline(self._config, cwd=self._root)
        self._incremental_probe_paths = tuple(
            sorted((self._workspace_root / "perf" / "generated").glob("bench_*.c"))
        )
        self._incremental_version = 0
        self._prime_incremental_state()

    def measure_suite(
        self,
        suite: EvalCaseSuite,
    ) -> tuple[PerformanceProbeObservation, ...]:
        observations: list[PerformanceProbeObservation] = [
            self._measure_serve_startup(),
            self._measure_init_reuse_baseline(),
        ]
        observations.extend(self._measure_query_probe(case) for case in suite.cases)
        observations.append(self._measure_incremental_index())
        observations.append(self._measure_resident_memory(suite))
        return tuple(observations)

    def environment(self) -> dict[str, object]:
        return {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count() or 1,
            "transport": self._resolved.model.server.transport,
        }

    def dataset_scale(self) -> dict[str, object]:
        return {
            "workspace_files": _count_files(self._workspace_root),
            "workspace_bytes": _total_size(self._workspace_root),
            "source_docs_files": _count_files(self._docs_root),
            "source_docs_bytes": _total_size(self._docs_root),
            "incremental_probe_files": len(self._incremental_probe_paths),
        }

    def close(self) -> None:
        self._tmpdir.cleanup()

    def _measure_serve_startup(self) -> PerformanceProbeObservation:
        return self._sample_latency(
            probe_id="serve_startup",
            display_name="Serve startup",
            operation=self._invoke_serve_startup,
            metadata={"transport": self._resolved.model.server.transport},
        )

    def _measure_init_reuse_baseline(self) -> PerformanceProbeObservation:
        for warmup in range(self._warmup_runs):
            self._invoke_init_reuse_baseline(run_id=f"warmup-{warmup}")

        samples: list[float] = []
        for sample_index in range(self._sample_count):
            probe_root = self._prepare_init_probe_root(run_id=f"sample-{sample_index}")
            resolved = _resolve_benchmark_config(
                probe_root,
                overrides={"server": {"transport": "streamable-http"}},
            )
            started = time.perf_counter()
            result = initialize_workdir(resolved, cwd=probe_root)
            samples.append(time.perf_counter() - started)
            if not result.baseline_manifest.exists or not result.baseline_manifest.readable:
                raise RuntimeError(
                    "init reuse-baseline probe did not observe a readable baseline manifest"
                )

        return PerformanceProbeObservation.from_samples(
            probe_id="init_reuse_baseline",
            display_name="Init with reuse-baseline",
            unit="seconds",
            samples=tuple(samples),
            metadata={"baseline_manifest": str(Path(self._config.storage.baseline.manifest))},
        )

    def _measure_query_probe(self, case: EvalCase) -> PerformanceProbeObservation:
        return self._sample_latency(
            probe_id=str(case.input_tool),
            display_name=f"{case.input_tool} latency",
            operation=lambda: self._invoke_query_tool(case),
            metadata={"case_id": case.case_id, "query": case.request.query},
        )

    def _measure_incremental_index(self) -> PerformanceProbeObservation:
        for _ in range(self._warmup_runs):
            self._mutate_incremental_probe_files()
            self._run_incremental_index()

        samples: list[float] = []
        for _ in range(self._sample_count):
            self._mutate_incremental_probe_files()
            started = time.perf_counter()
            self._run_incremental_index()
            samples.append(time.perf_counter() - started)

        return PerformanceProbeObservation.from_samples(
            probe_id="incremental_index_100_files",
            display_name="Incremental index for <=100 files",
            unit="seconds",
            samples=tuple(samples),
            metadata={
                "changed_files": len(self._incremental_probe_paths),
                "source": "code",
            },
        )

    def _measure_resident_memory(
        self,
        suite: EvalCaseSuite,
    ) -> PerformanceProbeObservation:
        for _ in range(self._warmup_runs):
            self._run_steady_state_workload(suite)

        samples: list[float] = []
        for _ in range(self._sample_count):
            self._run_steady_state_workload(suite)
            samples.append(float(_current_rss_bytes()))

        return PerformanceProbeObservation.from_samples(
            probe_id="serve_resident_memory",
            display_name="Serve resident memory",
            unit="bytes",
            samples=tuple(samples),
            metadata={"workload_case_ids": [case.case_id for case in suite.cases]},
        )

    def _sample_latency(
        self,
        *,
        probe_id: str,
        display_name: str,
        operation: Any,
        metadata: dict[str, object],
    ) -> PerformanceProbeObservation:
        for _ in range(self._warmup_runs):
            operation()

        samples: list[float] = []
        for _ in range(self._sample_count):
            started = time.perf_counter()
            operation()
            samples.append(time.perf_counter() - started)

        return PerformanceProbeObservation.from_samples(
            probe_id=probe_id,
            display_name=display_name,
            unit="seconds",
            samples=tuple(samples),
            metadata=metadata,
        )

    def _invoke_serve_startup(self) -> None:
        runtime = build_server_app(self._resolved, cwd=self._root)
        runtime.describe()
        runtime.http_app()

    def _invoke_init_reuse_baseline(self, *, run_id: str) -> None:
        probe_root = self._prepare_init_probe_root(run_id=run_id)
        resolved = _resolve_benchmark_config(
            probe_root,
            overrides={"server": {"transport": "streamable-http"}},
        )
        initialize_workdir(resolved, cwd=probe_root)

    def _prepare_init_probe_root(self, *, run_id: str) -> Path:
        probe_root = self._root / "init-probes" / run_id
        if probe_root.exists():
            shutil.rmtree(probe_root)
        (probe_root / "workspace").mkdir(parents=True, exist_ok=True)
        (probe_root / "knowledge-sources").mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            self._root / ".active-kb" / "baseline",
            probe_root / ".active-kb" / "baseline",
        )
        return probe_root

    def _invoke_query_tool(self, case: EvalCase) -> None:
        handler = self._tool_handlers.get(str(case.input_tool))
        if handler is None:
            raise ValueError(f"unsupported performance probe tool: {case.input_tool}")

        request = case.request
        if case.input_tool == "docs_search":
            result = handler(
                request.query,
                domain=request.domain,
                doc_type=_docs_search_doc_type(str(request.domain)),
                view=request.view,
                granularity=request.granularity,
                profile_id=request.profile_id,
                snapshot_id=request.snapshot_id,
            )
        elif case.input_tool == "code_resolve":
            result = handler(
                request.query,
                granularity=request.granularity,
                profile_id=request.profile_id,
                snapshot_id=request.snapshot_id,
            )
        elif case.input_tool == "workspace_view":
            result = handler(
                view=case.expected_route.selected_view,
                query=request.query,
                profile_id=request.profile_id,
                snapshot_id=request.snapshot_id,
                limit=self._config.query.default_top_k,
            )
        elif case.input_tool == "kb_search":
            result = handler(
                request.query,
                domain=request.domain,
                view=request.view,
                granularity=request.granularity,
                profile_id=request.profile_id,
                snapshot_id=request.snapshot_id,
            )
        elif case.input_tool == "evidence_bundle":
            result = handler(
                request.query,
                profile_id=request.profile_id,
                snapshot_id=request.snapshot_id,
            )
        else:
            raise ValueError(f"unsupported performance probe tool: {case.input_tool}")

        self._assert_query_result(result, tool_name=str(case.input_tool))

    def _assert_query_result(self, result: QueryResult, *, tool_name: str) -> None:
        QueryResult.model_validate(result.to_dict())
        if result.tool_name != tool_name:
            raise RuntimeError(
                f"performance probe returned tool_name {result.tool_name!r} for {tool_name!r}"
            )
        if result.result_status != "ok":
            raise RuntimeError(
                "performance probe "
                f"{tool_name!r} returned non-ok result_status "
                f"{result.result_status!r}"
            )

    def _run_incremental_index(self) -> None:
        result = self._incremental_pipeline.run(
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source="code",
        )
        if result.result_status != "ready":
            raise RuntimeError(
                f"incremental index probe returned unexpected status {result.result_status!r}"
            )

    def _mutate_incremental_probe_files(self) -> None:
        self._incremental_version += 1
        version = self._incremental_version
        for index, path in enumerate(self._incremental_probe_paths):
            path.write_text(
                (
                    f"int perf_generated_{index:03d}(void)\n"
                    "{\n"
                    f"    return {version + index};\n"
                    "}\n"
                ),
                encoding="utf-8",
            )

    def _prime_incremental_state(self) -> None:
        state, _, _, _ = self._incremental_pipeline.capture_state(
            snapshot_id=CURRENT_SNAPSHOT_ID
        )
        self._incremental_pipeline.save_state(state)

    def _run_steady_state_workload(self, suite: EvalCaseSuite) -> None:
        for case in suite.cases:
            self._invoke_query_tool(case)

    def _write_baseline_manifest(self) -> None:
        manifest_path = Path(self._config.storage.baseline.manifest)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "active_kb_baseline_manifest.v1",
            "project_id": self._config.project.id,
            "workspace_files": _count_files(self._workspace_root),
            "source_docs_files": _count_files(self._docs_root),
            "workspace_bytes": _total_size(self._workspace_root),
            "source_docs_bytes": _total_size(self._docs_root),
        }
        manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _docs_search_doc_type(domain: str) -> str | None:
    if domain in {"api", "widget", "product", "project"}:
        return domain
    return None


def _count_files(root: Path) -> int:
    return sum(1 for path in root.rglob("*") if path.is_file())


def _total_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _current_rss_bytes() -> int:
    status_path = Path("/proc/self/status")
    if status_path.exists():
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("VmRSS:"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024

    try:
        import resource
    except ImportError:
        return 0

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return int(usage.ru_maxrss) * 1024
