from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from active_knowledge_server.cli import (
    main,
    parse_performance_exemptions,
    resolve_index_output_mode,
)
from active_knowledge_server.config.loader import ConfigError
from active_knowledge_server.eval.baseline import create_baseline_snapshot
from active_knowledge_server.eval.metrics import PERFORMANCE_GATE_THRESHOLDS
from active_knowledge_server.eval.runner import EvalRunReport
from active_knowledge_server.indexing.pipeline import IncrementalIndexPlan, IncrementalIndexResult
from active_knowledge_server.indexing.progress import IndexProgressEvent
from active_knowledge_server.mcp.schemas import ALL_RESOURCE_URIS, ALL_TOOL_NAMES


def test_subcommands_have_help() -> None:
    for command in (
        "init",
        "serve",
        "index",
        "rebuild",
        "baseline",
        "status",
        "validate",
        "clean",
        "eval",
        "perf",
        "stability",
        "eval-baseline",
    ):
        result = subprocess.run(
            [sys.executable, "-m", "active_knowledge_server.cli", command, "--help"],
            check=True,
            capture_output=True,
            text=True,
        )

        assert "usage: active-kb" in result.stdout
        assert command in result.stdout


def test_eval_run_subcommand_has_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "eval", "run", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb eval run" in result.stdout
    assert "--cases" in result.stdout


def test_perf_run_subcommand_has_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "perf", "run", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb perf run" in result.stdout
    assert "--cases" in result.stdout


def test_rebuild_subcommand_has_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "rebuild", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb rebuild" in result.stdout
    assert "--vectors" in result.stdout


def test_baseline_validate_subcommand_has_help() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "active_knowledge_server.cli",
            "baseline",
            "validate",
            "--help",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb baseline validate" in result.stdout


def test_baseline_publish_subcommand_has_help() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "active_knowledge_server.cli",
            "baseline",
            "publish",
            "--help",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb baseline publish" in result.stdout
    assert "--publish-mode" in result.stdout


def test_stability_run_subcommand_has_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "stability", "run", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb stability run" in result.stdout
    assert "--soak-seconds" in result.stdout


def test_eval_baseline_save_subcommand_has_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "eval-baseline", "save", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb eval-baseline save" in result.stdout
    assert "--quality-report" in result.stdout


def test_eval_baseline_compare_subcommand_has_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "eval-baseline", "compare", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb eval-baseline compare" in result.stdout
    assert "--baseline" in result.stdout
    assert "--performance-exemption" in result.stdout


def test_parse_performance_exemptions_requires_known_probe_and_reason() -> None:
    assert parse_performance_exemptions(["kb_search=accepted for large workspace"]) == {
        "kb_search": "accepted for large workspace"
    }

    try:
        parse_performance_exemptions(["kb_search="])
    except ConfigError as exc:
        assert "PROBE_ID=REASON" in str(exc)
    else:
        raise AssertionError("empty performance exemption reason should fail")

    try:
        parse_performance_exemptions(["unknown=accepted"])
    except ConfigError as exc:
        assert "unknown performance exemption" in str(exc)
    else:
        raise AssertionError("unknown performance exemption probe should fail")


def test_status_json_is_machine_readable(capsys) -> None:
    exit_code = main(["status", "--format", "json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["command"] == "status"
    assert payload["status"] == "ok"
    assert "baseline_reuse" in payload
    assert "profile" in payload
    assert "index" in payload
    assert "warnings" in payload


def test_init_creates_workdir_and_local_config(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    write_profile_fixture(
        workspace,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
    )
    write_baseline_manifest(
        workdir / "baseline" / "manifest.json",
        baseline_id="baseline-unit",
        default_profile="mhs003_watch",
    )

    exit_code = main(
        [
            "init",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--reuse-baseline",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    local_config = workdir / "local" / "config" / "active-kb.local.yaml"

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert local_config.exists()
    assert (workdir / "local" / "db").is_dir()
    assert (workdir / "baseline" / "config").is_dir()
    assert payload["baseline_reuse"]["enabled"] is True
    assert payload["baseline_reuse"]["status"] == "missing"
    assert payload["profile"]["status"] == "resolved"
    assert payload["profile"]["resolved_profile_id"] == "mhs003_watch"
    assert payload["index"]["result_status"] == "missing"
    assert {warning["code"] for warning in payload["warnings"]} == {
        "compile_db.missing",
        "storage.schema_missing",
    }


def test_status_json_reports_baseline_profile_index_and_warnings(
    tmp_path: Path,
    capsys,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    write_profile_fixture(
        workspace,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
    )
    write_baseline_manifest(
        workdir / "baseline" / "manifest.json",
        baseline_id="baseline-unit",
        default_profile="mhs003_watch",
    )
    main(
        [
            "init",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--reuse-baseline",
            "--format",
            "json",
        ]
    )
    capsys.readouterr()

    exit_code = main(
        [
            "status",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["baseline_reuse"]["status"] == "missing"
    assert payload["profile"]["status"] == "resolved"
    assert payload["profile"]["resolved_profile_id"] == "mhs003_watch"
    assert payload["index"]["result_status"] == "missing"
    assert {warning["code"] for warning in payload["warnings"]} == {
        "compile_db.missing",
        "storage.schema_missing",
    }


def test_validate_json_reports_baseline_profile_index_and_warnings(
    tmp_path: Path,
    capsys,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    write_profile_fixture(
        workspace,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
    )
    write_baseline_manifest(
        workdir / "baseline" / "manifest.json",
        baseline_id="baseline-unit",
        default_profile="mhs003_watch",
    )
    main(
        [
            "init",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--reuse-baseline",
            "--format",
            "json",
        ]
    )
    capsys.readouterr()

    exit_code = main(
        [
            "validate",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["baseline_reuse"]["status"] == "missing"
    assert payload["profile"]["status"] == "resolved"
    assert payload["profile"]["resolved_profile_id"] == "mhs003_watch"
    assert payload["index"]["result_status"] == "missing"
    assert {warning["code"] for warning in payload["warnings"]} == {
        "compile_db.missing",
        "storage.schema_missing",
    }


def test_validate_strict_reports_missing_workdir(tmp_path: Path, capsys) -> None:
    missing_workdir = tmp_path / "missing-kb"

    exit_code = main(
        [
            "validate",
            "--workdir",
            str(missing_workdir),
            "--strict",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 1
    assert payload["status"] == "error"
    assert any(check["name"] == "workdir" for check in payload["checks"])


def test_serve_returns_blocked_json_for_insecure_local_http(capsys) -> None:
    exit_code = main(
        [
            "serve",
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["result_status"] == "blocked"
    assert payload["warnings"][0]["code"] == "security.remote_insecure_config"


def test_serve_json_reports_registered_mcp_components(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()

    exit_code = main(
        [
            "serve",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--transport",
            "http",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == "ready"
    assert payload["mcp"]["components"]["tools"] == list(ALL_TOOL_NAMES)
    assert payload["mcp"]["components"]["resources"] == list(ALL_RESOURCE_URIS)
    assert payload["mcp"]["http_endpoint"] == "http://127.0.0.1:8765/mcp"


def test_eval_run_json_reports_seed_suite_summary(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    cases = Path(__file__).resolve().parents[2] / "eval" / "cases.yaml"
    workspace.mkdir()
    source_docs.mkdir()

    exit_code = main(
        [
            "eval",
            "run",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--cases",
            str(cases),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "eval run"
    assert payload["status"] == "pass"
    assert payload["suite_id"] == "v1-routing-v1"
    assert payload["metrics"]["failed_cases"] == 0
    assert payload["metrics"]["release_gate_cases"] == 60
    assert payload["warnings"] == []


def test_eval_run_quality_json_uses_quality_suite_and_reports_gate_summary(
    tmp_path: Path,
    capsys,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()

    exit_code = main(
        [
            "eval",
            "run",
            "--gate",
            "quality",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "eval run"
    assert payload["gate_id"] == "quality"
    assert payload["suite_id"] == "quality-benchmark-v1"
    assert payload["status"] == "pass"
    assert payload["metrics"]["quality_gate"]["passed"] is True
    assert payload["metrics"]["blocked_security_probe"]["ok"] is True


def test_eval_run_performance_json_uses_performance_suite_and_reports_gate_summary(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    performance_cases = Path("eval") / "performance_cases.yaml"

    class DummyRunner:
        def run(self, cases_file: Path, *, gate_id: str) -> EvalRunReport:
            assert cases_file == performance_cases
            assert gate_id == "performance"
            return EvalRunReport(
                gate_id="performance",
                suite_id="performance-benchmark-v1",
                status="pass",
                started_at="2026-05-06T00:00:00Z",
                finished_at="2026-05-06T00:00:01Z",
                cases_file=str(cases_file),
                metrics={
                    "executed_cases": 5,
                    "passed_cases": 5,
                    "failed_cases": 0,
                    "performance_gate": {
                        "passed": True,
                        "sample_counts": {"serve_startup": 5},
                    },
                },
                failures=(),
                warnings=(),
            )

    monkeypatch.setattr(
        "active_knowledge_server.cli.EvalRunner.from_config",
        lambda *args, **kwargs: DummyRunner(),
    )

    exit_code = main(
        [
            "eval",
            "run",
            "--gate",
            "performance",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "eval run"
    assert payload["gate_id"] == "performance"
    assert payload["suite_id"] == "performance-benchmark-v1"
    assert payload["status"] == "pass"
    assert payload["metrics"]["performance_gate"]["passed"] is True


def test_perf_run_json_uses_performance_suite_and_reports_gate_summary(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    performance_cases = Path("eval") / "performance_cases.yaml"

    class DummyRunner:
        def run(
            self,
            cases_file: Path,
            *,
            gate_id: str,
            suite_kind: str | None = None,
        ) -> EvalRunReport:
            assert cases_file == performance_cases
            assert gate_id == "v1"
            assert suite_kind == "performance"
            return EvalRunReport(
                gate_id="v1",
                suite_id="performance-benchmark-v1",
                status="pass",
                started_at="2026-05-06T00:00:00Z",
                finished_at="2026-05-06T00:00:01Z",
                cases_file=str(cases_file),
                metrics={
                    "executed_cases": 5,
                    "passed_cases": 5,
                    "failed_cases": 0,
                    "performance_gate": {
                        "passed": True,
                        "sample_counts": {"serve_startup": 5},
                    },
                },
                failures=(),
                warnings=(),
            )

    monkeypatch.setattr(
        "active_knowledge_server.cli.EvalRunner.from_config",
        lambda *args, **kwargs: DummyRunner(),
    )

    exit_code = main(
        [
            "perf",
            "run",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "perf run"
    assert payload["gate_id"] == "v1"
    assert payload["suite_id"] == "performance-benchmark-v1"
    assert payload["status"] == "pass"
    assert payload["metrics"]["performance_gate"]["passed"] is True


def test_eval_run_stability_json_uses_stability_suite_and_reports_gate_summary(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    stability_cases = Path("eval") / "stability_cases.yaml"

    class DummyRunner:
        def run(
            self,
            cases_file: Path,
            *,
            gate_id: str,
            suite_kind: str | None = None,
        ) -> EvalRunReport:
            assert cases_file == stability_cases
            assert gate_id == "stability"
            assert suite_kind is None
            return EvalRunReport(
                gate_id="stability",
                suite_id="stability-mixed-query-v1",
                status="partial_ready",
                started_at="2026-05-06T00:00:00Z",
                finished_at="2026-05-06T00:00:01Z",
                cases_file=str(cases_file),
                metrics={
                    "executed_cases": 8,
                    "passed_cases": 8,
                    "failed_cases": 0,
                    "stability_gate": {
                        "passed": True,
                        "release_window": {"passed": False},
                    },
                },
                failures=(),
                warnings=(
                    {
                        "code": "eval.stability_release_window_incomplete",
                        "message": "short soak",
                    },
                ),
            )

    monkeypatch.setattr(
        "active_knowledge_server.cli.EvalRunner.from_config",
        lambda *args, **kwargs: DummyRunner(),
    )

    exit_code = main(
        [
            "eval",
            "run",
            "--gate",
            "stability",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "eval run"
    assert payload["gate_id"] == "stability"
    assert payload["suite_id"] == "stability-mixed-query-v1"
    assert payload["status"] == "partial_ready"
    assert payload["metrics"]["stability_gate"]["passed"] is True


def test_stability_run_json_uses_stability_suite_and_reports_gate_summary(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    stability_cases = Path("eval") / "stability_cases.yaml"

    class DummyRunner:
        def run(
            self,
            cases_file: Path,
            *,
            gate_id: str,
            suite_kind: str | None = None,
        ) -> EvalRunReport:
            assert cases_file == stability_cases
            assert gate_id == "v1"
            assert suite_kind == "stability"
            return EvalRunReport(
                gate_id="v1",
                suite_id="stability-mixed-query-v1",
                status="partial_ready",
                started_at="2026-05-06T00:00:00Z",
                finished_at="2026-05-06T00:00:01Z",
                cases_file=str(cases_file),
                metrics={
                    "executed_cases": 8,
                    "passed_cases": 8,
                    "failed_cases": 0,
                    "stability_gate": {
                        "passed": True,
                        "release_window": {
                            "passed": False,
                            "actual_soak_seconds": 60.0,
                            "actual_mixed_query_count": 500,
                        },
                    },
                },
                failures=(),
                warnings=(
                    {
                        "code": "eval.stability_release_window_incomplete",
                        "message": "short soak",
                    },
                ),
            )

    monkeypatch.setattr(
        "active_knowledge_server.cli.EvalRunner.from_config",
        lambda *args, **kwargs: DummyRunner(),
    )

    exit_code = main(
        [
            "stability",
            "run",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "stability run"
    assert payload["gate_id"] == "v1"
    assert payload["suite_id"] == "stability-mixed-query-v1"
    assert payload["status"] == "partial_ready"
    assert payload["metrics"]["stability_gate"]["passed"] is True


def test_eval_baseline_save_json_persists_snapshot(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    quality_path = tmp_path / "quality.json"
    performance_path = tmp_path / "performance.json"
    quality_path.write_text(json.dumps(_quality_report().to_dict()), encoding="utf-8")
    performance_path.write_text(json.dumps(_performance_report().to_dict()), encoding="utf-8")

    exit_code = main(
        [
            "eval-baseline",
            "save",
            "--baseline-id",
            "release-20260522",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--quality-report",
            str(quality_path),
            "--performance-report",
            str(performance_path),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "eval-baseline save"
    assert payload["baseline_id"] == "release-20260522"
    assert Path(payload["output"]).exists()
    assert Path(payload["latest"]).exists()


def test_eval_baseline_compare_json_reports_partial_ready_for_exempted_perf_regression(
    tmp_path: Path,
    capsys,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    baseline_dir = workdir / "baseline" / "artifacts" / "eval-baseline"
    baseline_dir.mkdir(parents=True)
    baseline_path = baseline_dir / "latest.json"
    snapshot = create_baseline_snapshot(
        baseline_id="release-20260522",
        quality_report=_quality_report(),
        performance_report=_performance_report(),
    )
    baseline_path.write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")
    current_quality_path = tmp_path / "current-quality.json"
    current_performance_path = tmp_path / "current-performance.json"
    current_quality_path.write_text(json.dumps(_quality_report().to_dict()), encoding="utf-8")
    current_performance_path.write_text(
        json.dumps(_performance_report(p95_overrides={"kb_search": 2.0}).to_dict()),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "eval-baseline",
            "compare",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--quality-report",
            str(current_quality_path),
            "--performance-report",
            str(current_performance_path),
            "--performance-exemption",
            "kb_search=accepted for larger workspace",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "eval-baseline compare"
    assert payload["status"] == "partial_ready"
    assert payload["warnings"][0]["check"] == "performance_regression_exempted"
    assert payload["warnings"][0]["exemption_reason"] == "accepted for larger workspace"


def test_eval_baseline_compare_json_fails_on_quality_regression(
    tmp_path: Path,
    capsys,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    baseline_dir = workdir / "baseline" / "artifacts" / "eval-baseline"
    baseline_dir.mkdir(parents=True)
    baseline_path = baseline_dir / "latest.json"
    snapshot = create_baseline_snapshot(
        baseline_id="release-20260522",
        quality_report=_quality_report(),
        performance_report=_performance_report(),
    )
    baseline_path.write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")
    current_quality_path = tmp_path / "current-quality.json"
    current_performance_path = tmp_path / "current-performance.json"
    current_quality_path.write_text(
        json.dumps(_quality_report(evidence_hit_rate=0.87, schema_compliance=0.99).to_dict()),
        encoding="utf-8",
    )
    current_performance_path.write_text(
        json.dumps(_performance_report().to_dict()), encoding="utf-8"
    )

    exit_code = main(
        [
            "eval-baseline",
            "compare",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--quality-report",
            str(current_quality_path),
            "--performance-report",
            str(current_performance_path),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 1
    assert payload["command"] == "eval-baseline compare"
    assert payload["status"] == "fail"
    assert any(item["check"] == "quality_metric_regression" for item in payload["failures"])


def test_serve_returns_blocked_json_for_invalid_deployment_mode(
    tmp_path: Path,
    capsys,
) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("deployment_mode: public_internet\n", encoding="utf-8")

    exit_code = main(["serve", "--config", str(config), "--format", "json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["result_status"] == "blocked"
    assert payload["warnings"][0]["code"] == "schema.invalid_request"
    assert "deployment_mode" in payload["warnings"][0]["message"]


def test_serve_without_json_runs_server(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    called = {"run": False}

    class DummyRuntime:
        def describe(self) -> dict[str, object]:
            return {"components": {"tools": [], "resources": []}}

        def run(self) -> None:
            called["run"] = True

    monkeypatch.setattr(
        "active_knowledge_server.cli.build_server_app", lambda resolved: DummyRuntime()
    )

    exit_code = main(
        [
            "serve",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
        ]
    )

    assert exit_code == 0
    assert called["run"] is True


def test_index_baseline_requires_publish_mode(capsys) -> None:
    exit_code = main(
        [
            "index",
            "--full",
            "--target",
            "baseline",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["result_status"] == "blocked"
    assert payload["warnings"][0]["code"] == "baseline.publish_mode_required"


def test_index_incremental_json_is_machine_readable(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()

    class DummyPipeline:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def run(
            self,
            *,
            snapshot_id: str,
            source: str,
            progress_callback,
        ) -> IncrementalIndexResult:
            assert snapshot_id == "current"
            assert source == "all"
            assert progress_callback is not None
            return IncrementalIndexResult(
                schema_version="incremental_index_result.v1",
                snapshot_id=snapshot_id,
                result_status="ready",
                plan=IncrementalIndexPlan(
                    snapshot_id=snapshot_id,
                    source="all",
                    previous_state=None,
                    current_state=object(),
                    workspace_inventory=object(),
                    source_docs_manifest=object(),
                    collected_profiles=object(),
                ),
            )

    monkeypatch.setattr("active_knowledge_server.cli.IncrementalIndexPipeline", DummyPipeline)

    exit_code = main(
        [
            "index",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--incremental",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "index"
    assert payload["status"] == "ok"
    assert payload["result"]["result_status"] == "ready"


def test_resolve_index_output_mode_contract() -> None:
    class DummyStream:
        def __init__(self, *, is_tty: bool) -> None:
            self._is_tty = is_tty

        def isatty(self) -> bool:
            return self._is_tty

    assert resolve_index_output_mode(output_format="json") == "json_final"
    assert (
        resolve_index_output_mode(output_format="text", stream=DummyStream(is_tty=True))
        == "text_dynamic"
    )
    assert (
        resolve_index_output_mode(output_format="text", stream=DummyStream(is_tty=False))
        == "text_plain"
    )
    assert (
        resolve_index_output_mode(
            output_format="text",
            stream=DummyStream(is_tty=True),
            rich_available=False,
        )
        == "text_plain"
    )


def test_baseline_publish_json_writes_manifest(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()

    monkeypatch.setattr(
        "active_knowledge_server.cli.run_full_index",
        lambda resolved, target, source, operation_mode, progress_callback=None: {
            "schema_version": "index_full_result.v1",
            "result_status": "ready",
            "snapshot_id": "current",
            "code_indexer_schema_version": "code_indexer.v1",
            "doc_indexer_schema_version": "doc_indexer.v1",
            "profile_collector_schema_version": "profile_collector.v1",
            "relation_schema_version": "profile_relations.v1",
        },
    )

    exit_code = main(
        [
            "baseline",
            "publish",
            "--publish-mode",
            "publish",
            "--baseline-id",
            "baseline-o8-02",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    manifest_path = workdir / "baseline" / "manifest.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["command"] == "baseline publish"
    assert payload["baseline_id"] == "baseline-o8-02"
    assert manifest_payload["baseline_id"] == "baseline-o8-02"


def test_index_interrupt_prints_snapshot_without_traceback(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()

    class DummyPipeline:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def run(
            self,
            *,
            snapshot_id: str,
            source: str,
            progress_callback,
        ) -> IncrementalIndexResult:
            progress_callback(
                IndexProgressEvent(
                    phase="doc_collect",
                    stage_total=4,
                    stage_done=2,
                    global_total=8,
                    global_done=3,
                    current_path="knowledge-sources/api/sensor.md",
                    message="Collecting source documents",
                )
            )
            raise KeyboardInterrupt

    monkeypatch.setattr("active_knowledge_server.cli.IncrementalIndexPipeline", DummyPipeline)
    monkeypatch.setattr(
        "active_knowledge_server.cli.resolve_index_output_mode",
        lambda output_format: "text_plain",
    )

    exit_code = main(
        [
            "index",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--incremental",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 130
    assert "Index interrupted." in captured.out
    assert "Traceback" not in captured.out


def test_baseline_validate_json_reports_missing_manifest(
    tmp_path: Path,
    capsys,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()

    exit_code = main(
        [
            "baseline",
            "validate",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 1
    assert payload["command"] == "baseline validate"
    assert payload["status"] == "fail"
    assert payload["manifest"]["exists"] is False


def write_profile_fixture(
    workspace_root: Path,
    *,
    defconfig_rel: str,
    dotconfig_rel: str,
    app: str,
    board: str,
) -> None:
    defconfig_path = workspace_root / defconfig_rel
    dotconfig_path = workspace_root / dotconfig_rel
    defconfig_path.parent.mkdir(parents=True, exist_ok=True)
    dotconfig_path.parent.mkdir(parents=True, exist_ok=True)
    defconfig_path.write_text(
        f'CONFIG_APP="{app}"\nCONFIG_BOARD="{board}"\nCONFIG_FEATURE_{app.upper()}=y\n',
        encoding="utf-8",
    )
    dotconfig_path.write_text(
        f'CONFIG_APP="{app}"\nCONFIG_BOARD="{board}"\nCONFIG_RUNTIME_READY=y\n',
        encoding="utf-8",
    )


def write_baseline_manifest(
    path: Path,
    *,
    baseline_id: str,
    default_profile: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "active_kb_baseline_manifest.v1",
                "baseline_id": baseline_id,
                "default_profile": default_profile,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _quality_report(
    *,
    evidence_hit_rate: float = 0.90,
    top_5_recall: float = 0.93,
    symbol_top_3_recall: float = 0.97,
    mrr: float = 0.80,
    profile_correctness: float = 0.94,
    warning_quality: float = 0.90,
    schema_compliance: float = 1.0,
    blocked_security_contract: float = 1.0,
) -> EvalRunReport:
    observations = []
    for category in (
        "symbol_lookup",
        "api_documentation",
        "widget_usage",
        "workspace_navigation",
        "profile_impact",
        "feature_domain_cross_layer",
    ):
        observations.append(
            {
                "case_id": f"{category}:1",
                "category": category,
                "result_status": "ok",
                "schema_compliant": True,
                "warning_quality_ok": True,
                "profile_correct": True,
                "evidence_hit": True,
                "top_5_hit": True,
                "symbol_top_3_hit": True,
                "reciprocal_rank": 1.0,
            }
        )
    return EvalRunReport(
        gate_id="quality",
        suite_id="quality-benchmark-v1",
        status="pass",
        started_at="2026-05-22T00:00:00Z",
        finished_at="2026-05-22T00:00:01Z",
        cases_file="eval/quality_cases.yaml",
        metrics={
            "quality_gate": {
                "metrics": {
                    "schema_compliance": schema_compliance,
                    "evidence_hit_rate": evidence_hit_rate,
                    "top_5_recall": top_5_recall,
                    "symbol_top_3_recall": symbol_top_3_recall,
                    "mrr": mrr,
                    "profile_correctness": profile_correctness,
                    "warning_quality": warning_quality,
                    "blocked_security_contract": blocked_security_contract,
                },
                "case_observations": observations,
            }
        },
    )


def _performance_report(
    *,
    p95_overrides: dict[str, float] | None = None,
) -> EvalRunReport:
    metrics = {}
    for probe_id, (unit, threshold) in PERFORMANCE_GATE_THRESHOLDS.items():
        p95 = (p95_overrides or {}).get(probe_id, threshold / 2.0)
        metrics[probe_id] = {
            "unit": unit,
            "p50": p95 / 2.0,
            "p95": p95,
            "mean": p95 / 2.0,
            "max": p95,
        }
    return EvalRunReport(
        gate_id="performance",
        suite_id="performance-benchmark-v1",
        status="pass",
        started_at="2026-05-22T00:00:00Z",
        finished_at="2026-05-22T00:00:01Z",
        cases_file="eval/performance_cases.yaml",
        metrics={"performance_gate": {"metrics": metrics}},
    )
