from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from active_knowledge_server.cli import main
from active_knowledge_server.eval.runner import EvalRunReport
from active_knowledge_server.mcp.schemas import ALL_RESOURCE_URIS, ALL_TOOL_NAMES


def test_subcommands_have_help() -> None:
    for command in (
        "init",
        "serve",
        "index",
        "status",
        "validate",
        "clean",
        "eval",
        "perf",
        "stability",
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


def test_stability_run_subcommand_has_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "stability", "run", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb stability run" in result.stdout
    assert "--soak-seconds" in result.stdout


def test_status_json_is_machine_readable(capsys) -> None:
    exit_code = main(["status", "--format", "json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["command"] == "status"
    assert payload["status"] == "ok"
    assert payload["index"]["result_status"] == "partial_ready"


def test_init_creates_workdir_and_local_config(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()

    exit_code = main(
        [
            "init",
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
    local_config = workdir / "local" / "config" / "active-kb.local.yaml"

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert local_config.exists()
    assert (workdir / "local" / "db").is_dir()
    assert (workdir / "baseline" / "config").is_dir()


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

    monkeypatch.setattr("active_knowledge_server.cli.build_server_app", lambda resolved: DummyRuntime())

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
