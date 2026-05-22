from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.eval import EvalRunner

CASES_FILE = Path(__file__).resolve().parents[2] / "eval" / "cases.yaml"


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