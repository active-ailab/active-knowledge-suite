from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from active_knowledge_server.cli import main


def test_subcommands_have_help() -> None:
    for command in ("init", "serve", "index", "status", "validate"):
        result = subprocess.run(
            [sys.executable, "-m", "active_knowledge_server.cli", command, "--help"],
            check=True,
            capture_output=True,
            text=True,
        )

        assert "usage: active-kb" in result.stdout
        assert command in result.stdout


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
