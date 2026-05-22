from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from active_knowledge_server.cli import main
from active_knowledge_server.mcp.schemas import ALL_RESOURCE_URIS, ALL_TOOL_NAMES


def test_subcommands_have_help() -> None:
    for command in ("init", "serve", "index", "status", "validate", "clean", "eval"):
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
