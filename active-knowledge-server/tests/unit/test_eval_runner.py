from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.eval import EvalRunner
from active_knowledge_server.eval.benchmark import QualityBenchmark
from active_knowledge_server.eval.metrics import (
    PERFORMANCE_GATE_THRESHOLDS,
    PerformanceProbeObservation,
)
from active_knowledge_server.eval.performance import PerformanceBenchmark
from active_knowledge_server.eval.reproducibility import ReproducibilityBenchmark
from active_knowledge_server.eval.stability import StabilityBenchmark

CASES_FILE = Path(__file__).resolve().parents[2] / "eval" / "cases.yaml"
QUALITY_CASES_FILE = Path(__file__).resolve().parents[2] / "eval" / "quality_cases.yaml"
PERFORMANCE_CASES_FILE = Path(__file__).resolve().parents[2] / "eval" / "performance_cases.yaml"
STABILITY_CASES_FILE = Path(__file__).resolve().parents[2] / "eval" / "stability_cases.yaml"
REPRODUCIBILITY_CASES_FILE = (
    Path(__file__).resolve().parents[2] / "eval" / "reproducibility_cases.yaml"
)


def _resolved_config(tmp_path: Path) -> object:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    source_docs.mkdir()

    overrides: ConfigDict = {
        "runtime": {
            "workdir": ".active-kb",
            "source_docs_root": "knowledge-sources",
        },
        "project": {
            "workspace_root": "workspace",
            "id": "active-test",
            "display_name": "Active Test",
        },
    }
    return resolve_config(cli_overrides=overrides, cwd=tmp_path)


def test_eval_runner_executes_v1_suite_and_reports_pass(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)
    runner = EvalRunner.from_config(resolved.model, cwd=tmp_path)

    report = runner.run(CASES_FILE, gate_id="v1")

    assert report.status == "pass"
    assert report.failures == ()
    assert report.metrics["failed_cases"] == 0
    assert report.metrics["passed_cases"] == report.metrics["executed_cases"]
    assert report.metrics["total_cases"] >= 62
    assert report.metrics["release_gate_cases"] == 60

    category_coverage = {item["category"]: item for item in report.metrics["category_coverage"]}
    assert category_coverage["symbol_lookup"]["actual_cases"] == 10
    assert category_coverage["workspace_navigation"]["actual_cases"] == 10
    assert all(item["ready"] for item in category_coverage.values())
    assert report.warnings == ()


def test_eval_runner_executes_quality_suite_and_reports_pass(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)
    runner = EvalRunner.from_config(resolved.model, cwd=tmp_path)

    report = runner.run(QUALITY_CASES_FILE, gate_id="quality")

    assert report.status == "pass"
    assert report.failures == ()
    quality_gate = report.metrics["quality_gate"]
    assert quality_gate["passed"] is True
    assert quality_gate["metrics"]["schema_compliance"] == 1.0
    assert quality_gate["metrics"]["blocked_security_contract"] == 1.0
    assert all(item["passed"] for item in quality_gate["checks"])


def test_eval_runner_fails_quality_gate_when_thresholds_regress(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)

    class BrokenQualityBenchmark(QualityBenchmark):
        def search(self, case):  # type: ignore[override]
            result = super().search(case)
            if case.case_id != "quality_api_sensor_open":
                return result
            return result.model_copy(update={"items": (), "evidence_refs": ()})

    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        quality_benchmark_factory=BrokenQualityBenchmark,
    )

    report = runner.run(QUALITY_CASES_FILE, gate_id="quality")

    assert report.status == "fail"
    assert any(failure["case_id"] == "quality_api_sensor_open" for failure in report.failures)
    failed_checks = [
        item for item in report.metrics["quality_gate"]["checks"] if not item["passed"]
    ]
    assert failed_checks
    assert any(
        item["metric"] in {"evidence_hit_rate", "top_5_recall", "mrr"} for item in failed_checks
    )


def test_eval_runner_executes_performance_suite_and_reports_pass(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)

    class PassingPerformanceBenchmark:
        def measure_suite(self, suite):
            del suite
            return _performance_observations()

        def environment(self):
            return {
                "platform": "test-platform",
                "python_version": "3.11.0",
                "cpu_count": 4,
                "transport": "streamable-http",
            }

        def dataset_scale(self):
            return {
                "workspace_files": 103,
                "source_docs_files": 3,
                "incremental_probe_files": 100,
            }

        def close(self):
            return None

    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        performance_benchmark_factory=PassingPerformanceBenchmark,
    )

    report = runner.run(PERFORMANCE_CASES_FILE, gate_id="performance")

    assert report.status == "pass"
    assert report.failures == ()
    performance_gate = report.metrics["performance_gate"]
    assert performance_gate["passed"] is True
    assert performance_gate["environment"]["transport"] == "streamable-http"
    assert performance_gate["sample_counts"]["serve_startup"] == 5


def test_eval_runner_supports_named_performance_suite_with_release_gate_id(
    tmp_path: Path,
) -> None:
    resolved = _resolved_config(tmp_path)

    class PassingPerformanceBenchmark:
        def measure_suite(self, suite):
            del suite
            return _performance_observations()

        def environment(self):
            return {
                "platform": "test-platform",
                "python_version": "3.11.0",
                "cpu_count": 4,
                "transport": "streamable-http",
            }

        def dataset_scale(self):
            return {
                "workspace_files": 103,
                "source_docs_files": 3,
                "incremental_probe_files": 100,
            }

        def close(self):
            return None

    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        performance_benchmark_factory=PassingPerformanceBenchmark,
    )

    report = runner.run(
        PERFORMANCE_CASES_FILE,
        gate_id="v1",
        suite_kind="performance",
    )

    assert report.gate_id == "v1"
    assert report.status == "pass"
    assert report.metrics["performance_gate"]["passed"] is True


def test_eval_runner_fails_performance_gate_when_p95_regresses(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)

    class FailingPerformanceBenchmark:
        def measure_suite(self, suite):
            del suite
            return _performance_observations(overrides={"serve_startup": 12.0})

        def environment(self):
            return {
                "platform": "test-platform",
                "python_version": "3.11.0",
                "cpu_count": 4,
                "transport": "streamable-http",
            }

        def dataset_scale(self):
            return {
                "workspace_files": 103,
                "source_docs_files": 3,
                "incremental_probe_files": 100,
            }

        def close(self):
            return None

    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        performance_benchmark_factory=FailingPerformanceBenchmark,
    )

    report = runner.run(PERFORMANCE_CASES_FILE, gate_id="performance")

    assert report.status == "fail"
    assert report.failures == ()
    assert report.warnings[0]["code"] == "eval.performance_gate_failed"
    failed_checks = [
        item for item in report.metrics["performance_gate"]["checks"] if not item["passed"]
    ]
    assert failed_checks
    assert failed_checks[0]["metric"] == "serve_startup"


def test_eval_runner_executes_real_performance_benchmark_smoke(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)
    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        performance_benchmark_factory=lambda: PerformanceBenchmark(
            sample_count=1,
            warmup_runs=0,
            incremental_file_count=5,
        ),
    )

    report = runner.run(PERFORMANCE_CASES_FILE, gate_id="performance")

    assert report.status == "pass"
    assert report.metrics["performance_gate"]["passed"] is True
    assert report.metrics["performance_gate"]["metrics"]["docs_search"]["p95"] >= 0.0
    assert report.metrics["performance_gate"]["metrics"]["serve_resident_memory"]["p95"] > 0


def test_eval_runner_executes_stability_suite_and_reports_pass(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)

    class PassingStabilityBenchmark:
        def measure_suite(self, suite):
            assert suite.suite_id == "stability-mixed-query-v1"
            return {
                "mixed_query": {
                    "configured_runs": 500,
                    "total_runs": 500,
                    "eligible_runs": 440,
                    "success_count": 436,
                    "success_rate": 0.990909,
                    "exception_count": 0,
                    "excluded_status_counts": {"zero_result": 60},
                    "failure_count": 0,
                    "failures": [],
                },
                "soak": {
                    "configured_seconds": 28800,
                    "actual_seconds": 28800.5,
                    "iterations": 240000,
                    "unhandled_exceptions": 0,
                },
                "index_recovery": {
                    "checkpoint_resume_available": True,
                    "failed_state_recorded": True,
                    "retryable": True,
                    "stale_lock_reacquired": True,
                    "lock_cleared": True,
                },
                "migration_idempotence": {
                    "schema_version": "1.0.1",
                    "applied_counts": [1, 0, 0],
                    "history_count": 1,
                },
                "partial_ready_query": {
                    "result_status": "partial_ready",
                    "warning_codes": ["index.partial_ready"],
                    "schema_compliant": True,
                    "item_count": 1,
                },
                "readonly_concurrency": {
                    "workers": 8,
                    "total_queries": 64,
                    "completed_queries": 64,
                    "timeout_count": 0,
                    "failure_count": 0,
                    "max_latency_seconds": 0.32,
                    "mean_latency_seconds": 0.08,
                    "failures": [],
                },
            }

        def environment(self):
            return {"platform": "test-platform", "transport": "streamable-http"}

        def dataset_scale(self):
            return {"workspace_files": 103, "source_docs_files": 3}

        def close(self):
            return None

    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        stability_benchmark_factory=PassingStabilityBenchmark,
    )

    report = runner.run(
        STABILITY_CASES_FILE,
        gate_id="stability",
        suite_kind="stability",
    )

    assert report.status == "pass"
    assert report.failures == ()
    stability_gate = report.metrics["stability_gate"]
    assert stability_gate["passed"] is True
    assert stability_gate["release_window"]["passed"] is True
    assert all(item["passed"] for item in stability_gate["checks"])


def test_eval_runner_marks_stability_suite_partial_ready_when_release_window_is_short(
    tmp_path: Path,
) -> None:
    resolved = _resolved_config(tmp_path)

    class ShortWindowStabilityBenchmark:
        def measure_suite(self, suite):
            del suite
            return {
                "mixed_query": {
                    "configured_runs": 500,
                    "total_runs": 500,
                    "eligible_runs": 440,
                    "success_count": 440,
                    "success_rate": 1.0,
                    "exception_count": 0,
                    "excluded_status_counts": {"zero_result": 60},
                    "failure_count": 0,
                    "failures": [],
                },
                "soak": {
                    "configured_seconds": 30,
                    "actual_seconds": 30.1,
                    "iterations": 240,
                    "unhandled_exceptions": 0,
                },
                "index_recovery": {
                    "checkpoint_resume_available": True,
                    "failed_state_recorded": True,
                    "retryable": True,
                    "stale_lock_reacquired": True,
                    "lock_cleared": True,
                },
                "migration_idempotence": {
                    "schema_version": "1.0.1",
                    "applied_counts": [1, 0, 0],
                    "history_count": 1,
                },
                "partial_ready_query": {
                    "result_status": "partial_ready",
                    "warning_codes": ["index.partial_ready"],
                    "schema_compliant": True,
                    "item_count": 1,
                },
                "readonly_concurrency": {
                    "workers": 8,
                    "total_queries": 64,
                    "completed_queries": 64,
                    "timeout_count": 0,
                    "failure_count": 0,
                    "max_latency_seconds": 0.25,
                    "mean_latency_seconds": 0.07,
                    "failures": [],
                },
            }

        def environment(self):
            return {}

        def dataset_scale(self):
            return {}

        def close(self):
            return None

    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        stability_benchmark_factory=ShortWindowStabilityBenchmark,
    )

    report = runner.run(
        STABILITY_CASES_FILE,
        gate_id="stability",
        suite_kind="stability",
    )

    assert report.status == "partial_ready"
    assert report.warnings[0]["code"] == "eval.stability_release_window_incomplete"
    assert report.metrics["stability_gate"]["passed"] is True
    assert report.metrics["stability_gate"]["release_window"]["passed"] is False


def test_eval_runner_fails_stability_gate_when_mixed_query_success_rate_regresses(
    tmp_path: Path,
) -> None:
    resolved = _resolved_config(tmp_path)

    class FailingStabilityBenchmark:
        def measure_suite(self, suite):
            del suite
            return {
                "mixed_query": {
                    "configured_runs": 500,
                    "total_runs": 500,
                    "eligible_runs": 440,
                    "success_count": 430,
                    "success_rate": 0.977273,
                    "exception_count": 3,
                    "excluded_status_counts": {"zero_result": 60},
                    "failure_count": 10,
                    "failures": [{"case_id": "stability_api_sensor_close", "error": "boom"}],
                },
                "soak": {
                    "configured_seconds": 28800,
                    "actual_seconds": 28801.0,
                    "iterations": 240000,
                    "unhandled_exceptions": 0,
                },
                "index_recovery": {
                    "checkpoint_resume_available": True,
                    "failed_state_recorded": True,
                    "retryable": True,
                    "stale_lock_reacquired": True,
                    "lock_cleared": True,
                },
                "migration_idempotence": {
                    "schema_version": "1.0.1",
                    "applied_counts": [1, 0, 0],
                    "history_count": 1,
                },
                "partial_ready_query": {
                    "result_status": "partial_ready",
                    "warning_codes": ["index.partial_ready"],
                    "schema_compliant": True,
                    "item_count": 1,
                },
                "readonly_concurrency": {
                    "workers": 8,
                    "total_queries": 64,
                    "completed_queries": 64,
                    "timeout_count": 0,
                    "failure_count": 0,
                    "max_latency_seconds": 0.32,
                    "mean_latency_seconds": 0.08,
                    "failures": [],
                },
            }

        def environment(self):
            return {}

        def dataset_scale(self):
            return {}

        def close(self):
            return None

    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        stability_benchmark_factory=FailingStabilityBenchmark,
    )

    report = runner.run(
        STABILITY_CASES_FILE,
        gate_id="stability",
        suite_kind="stability",
    )

    assert report.status == "fail"
    assert report.warnings[0]["code"] == "eval.stability_gate_failed"
    failed_checks = [
        item for item in report.metrics["stability_gate"]["checks"] if not item["passed"]
    ]
    assert failed_checks
    assert failed_checks[0]["metric"] == "mixed_query_success_rate"
    assert report.failures[0]["probe"] == "mixed_query"


def test_eval_runner_executes_real_stability_benchmark_smoke(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)
    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        stability_benchmark_factory=lambda: StabilityBenchmark(
            soak_seconds=1,
            mixed_query_count=12,
            readonly_workers=4,
            readonly_query_count=8,
            readonly_timeout_seconds=2.0,
        ),
    )

    report = runner.run(
        STABILITY_CASES_FILE,
        gate_id="stability",
        suite_kind="stability",
    )

    assert report.status == "partial_ready"
    assert report.metrics["stability_gate"]["passed"] is True
    assert report.metrics["stability_gate"]["metrics"]["mixed_query_success_rate"] >= 0.99


def test_eval_runner_executes_reproducibility_suite_and_reports_pass(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)

    class PassingReproducibilityBenchmark:
        def measure_suite(self, suite):
            assert suite.suite_id == "reproducibility-index-v1"
            stable_ids = ["stable:a", "stable:b"]
            return {
                "first_snapshot_id": "snapshot:stable",
                "second_snapshot_id": "snapshot:stable",
                "first_profile_ids": ["mhs003_watch"],
                "second_profile_ids": ["mhs003_watch"],
                "first_profile_record_ids": ["profile:stable"],
                "second_profile_record_ids": ["profile:stable"],
                "first_entity_ids": stable_ids,
                "second_entity_ids": stable_ids,
                "first_chunk_ids": stable_ids,
                "second_chunk_ids": stable_ids,
                "first_evidence_ids": stable_ids,
                "second_evidence_ids": stable_ids,
                "first_vector_ref_ids": stable_ids,
                "second_vector_ref_ids": stable_ids,
                "first_core_report_hash": "sha256:stable",
                "second_core_report_hash": "sha256:stable",
            }

        def environment(self):
            return {"platform": "test-platform"}

        def dataset_scale(self):
            return {"workspace_files": 3, "source_docs_files": 2}

        def close(self):
            return None

    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        reproducibility_benchmark_factory=PassingReproducibilityBenchmark,
    )

    report = runner.run(REPRODUCIBILITY_CASES_FILE, gate_id="reproducibility")

    assert report.status == "pass"
    assert report.failures == ()
    gate = report.metrics["reproducibility_gate"]
    assert gate["passed"] is True
    assert gate["metrics"]["passed_check_count"] == gate["metrics"]["stable_check_count"]


def test_eval_runner_fails_reproducibility_suite_when_core_hash_changes(
    tmp_path: Path,
) -> None:
    resolved = _resolved_config(tmp_path)

    class FailingReproducibilityBenchmark:
        def measure_suite(self, suite):
            del suite
            stable_ids = ["stable:a"]
            return {
                "first_snapshot_id": "snapshot:stable",
                "second_snapshot_id": "snapshot:stable",
                "first_profile_ids": ["mhs003_watch"],
                "second_profile_ids": ["mhs003_watch"],
                "first_profile_record_ids": ["profile:stable"],
                "second_profile_record_ids": ["profile:stable"],
                "first_entity_ids": stable_ids,
                "second_entity_ids": stable_ids,
                "first_chunk_ids": stable_ids,
                "second_chunk_ids": stable_ids,
                "first_evidence_ids": stable_ids,
                "second_evidence_ids": stable_ids,
                "first_vector_ref_ids": stable_ids,
                "second_vector_ref_ids": stable_ids,
                "first_core_report_hash": "sha256:first",
                "second_core_report_hash": "sha256:second",
            }

        def environment(self):
            return {}

        def dataset_scale(self):
            return {}

        def close(self):
            return None

    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        reproducibility_benchmark_factory=FailingReproducibilityBenchmark,
    )

    report = runner.run(REPRODUCIBILITY_CASES_FILE, gate_id="reproducibility")

    assert report.status == "fail"
    assert report.warnings[0]["code"] == "eval.reproducibility_gate_failed"
    assert any(item["check"] == "core_report_hash_stable" for item in report.failures)


def test_eval_runner_executes_real_reproducibility_benchmark_smoke(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)
    runner = EvalRunner.from_config(
        resolved.model,
        cwd=tmp_path,
        reproducibility_benchmark_factory=ReproducibilityBenchmark,
    )

    report = runner.run(REPRODUCIBILITY_CASES_FILE, gate_id="reproducibility")

    assert report.status == "pass"
    gate = report.metrics["reproducibility_gate"]
    assert gate["passed"] is True
    assert gate["metrics"]["entity_id_count"] > 0
    assert gate["metrics"]["chunk_id_count"] > 0
    assert gate["metrics"]["evidence_id_count"] > 0


def _performance_observations(
    *,
    overrides: dict[str, float] | None = None,
) -> tuple[PerformanceProbeObservation, ...]:
    values = overrides or {}
    observations = []
    for probe_id, (unit, threshold) in PERFORMANCE_GATE_THRESHOLDS.items():
        sample = values.get(probe_id, threshold / 2.0)
        observations.append(
            PerformanceProbeObservation.from_samples(
                probe_id=probe_id,
                display_name=probe_id,
                unit=unit,
                samples=(sample, sample, sample, sample, sample),
                metadata={},
            )
        )
    return tuple(observations)
