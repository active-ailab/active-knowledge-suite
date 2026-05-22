from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.eval import EvalRunner
from active_knowledge_server.eval.benchmark import QualityBenchmark

CASES_FILE = Path(__file__).resolve().parents[2] / "eval" / "cases.yaml"
QUALITY_CASES_FILE = Path(__file__).resolve().parents[2] / "eval" / "quality_cases.yaml"


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

    category_coverage = {
        item["category"]: item for item in report.metrics["category_coverage"]
    }
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
    failed_checks = [item for item in report.metrics["quality_gate"]["checks"] if not item["passed"]]
    assert failed_checks
    assert any(item["metric"] in {"evidence_hit_rate", "top_5_recall", "mrr"} for item in failed_checks)