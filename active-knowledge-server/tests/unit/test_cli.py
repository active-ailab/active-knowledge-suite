from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from active_knowledge_server.cli import (
    main,
    parse_performance_exemptions,
    resolve_index_output_mode,
    resolve_index_progress_output_mode,
    resolve_index_resume_policy,
    storage_write_target_for_cli_target,
)
from active_knowledge_server.config.loader import ConfigError
from active_knowledge_server.eval.baseline import create_baseline_snapshot
from active_knowledge_server.eval.metrics import PERFORMANCE_GATE_THRESHOLDS
from active_knowledge_server.eval.runner import EvalRunReport
from active_knowledge_server.indexing.pipeline import IncrementalIndexPlan, IncrementalIndexResult
from active_knowledge_server.indexing.progress import IndexProgressEvent
from active_knowledge_server.mcp.schemas import ALL_RESOURCE_URIS, ALL_TOOL_NAMES


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _pythonpath_with(*extra_paths: Path) -> str:
    parts = [str(path) for path in extra_paths]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def _write_cli_workspace_fixture(workspace: Path) -> None:
    component_dir = workspace / "components" / "health"
    component_dir.mkdir(parents=True, exist_ok=True)
    (component_dir / "module.mk").write_text(
        """NAME = health_core
MODULE = health.logic
HEALTH_SOURCES = main.c health.h
ifdef CONFIG_HEALTH_BT
HEALTH_SOURCES += bt.c
endif
""",
        encoding="utf-8",
    )
    (component_dir / "main.c").write_text(
        """#include "health.h"

#define HEALTH_BASELINE 1

int health_main(void)
{
    return HEALTH_BASELINE;
}
""",
        encoding="utf-8",
    )
    (component_dir / "bt.c").write_text(
        """#include "health.h"

#define HEALTH_BT_BASELINE 1

int health_bt(void)
{
    return HEALTH_BT_BASELINE;
}
""",
        encoding="utf-8",
    )
    (component_dir / "health.h").write_text(
        """#ifndef HEALTH_H
#define HEALTH_H

int health_main(void);
int health_bt(void);

#endif
""",
        encoding="utf-8",
    )


def _wait_for_applied_checkpoint(jobs_path: Path, job_id: str, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if jobs_path.exists():
            with sqlite3.connect(jobs_path) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM job_checkpoint
                    WHERE job_id = ?
                      AND checkpoint_key LIKE 'task:applied:%'
                    """,
                    (job_id,),
                ).fetchone()
            if row is not None and int(row[0]) > 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for applied checkpoint in {jobs_path}")


def test_subcommands_have_help() -> None:
    for command in (
        "init",
        "serve",
        "index",
        "rebuild",
        "baseline",
        "release",
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


def test_index_help_documents_resume_contract() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "index", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--resume auto|JOB_ID" in result.stdout
    assert "--restart" in result.stdout
    assert "--no-resume" in result.stdout
    assert "--job-id JOB_ID" in result.stdout
    assert "the default" in result.stdout
    assert "newest compatible job" in result.stdout


def test_index_resume_restart_flags_are_mutually_exclusive() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "active_knowledge_server.cli",
            "index",
            "--restart",
            "--no-resume",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr


def test_resolve_index_resume_policy_contract() -> None:
    args = argparse.Namespace(
        resume="auto",
        restart=False,
        no_resume=False,
        job_id=None,
    )

    assert resolve_index_resume_policy(args)["mode"] == "auto"

    explicit = argparse.Namespace(
        resume="index:abc",
        restart=False,
        no_resume=False,
        job_id=None,
    )
    explicit_policy = resolve_index_resume_policy(explicit)
    assert explicit_policy["mode"] == "job_id"
    assert explicit_policy["resume_job_id"] == "index:abc"

    restart = argparse.Namespace(
        resume="auto",
        restart=True,
        no_resume=False,
        job_id="index:new",
    )
    restart_policy = resolve_index_resume_policy(restart)
    assert restart_policy["mode"] == "restart"
    assert restart_policy["planned_job_id"] == "index:new"


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


def test_cli_local_target_maps_to_overlay_storage_target() -> None:
    assert storage_write_target_for_cli_target("local") == "overlay"
    assert storage_write_target_for_cli_target("baseline") == "baseline"


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


def test_release_checklist_subcommand_has_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "release", "checklist", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb release checklist" in result.stdout
    assert "--quality-report" in result.stdout
    assert "--remote-config" in result.stdout


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


def test_init_uses_quick_storage_validation(monkeypatch, tmp_path: Path, capsys) -> None:
    from active_knowledge_server import cli as cli_module

    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()

    captured_modes: list[str] = []
    original = cli_module.validate_storage_consistency

    def wrapped(*args, **kwargs):
        captured_modes.append(str(kwargs.get("mode", "full")))
        return original(*args, **kwargs)

    monkeypatch.setattr(cli_module, "validate_storage_consistency", wrapped)

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

    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert captured_modes == ["quick"]


def test_validate_uses_full_storage_validation(monkeypatch, tmp_path: Path, capsys) -> None:
    from active_knowledge_server import cli as cli_module

    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()

    captured_modes: list[str] = []
    original = cli_module.validate_storage_consistency

    def wrapped(*args, **kwargs):
        captured_modes.append(str(kwargs.get("mode", "full")))
        return original(*args, **kwargs)

    monkeypatch.setattr(cli_module, "validate_storage_consistency", wrapped)

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

    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert captured_modes == ["full"]


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


def _empty_incremental_plan(
    *,
    snapshot_id: str = "current",
    source: str = "all",
) -> IncrementalIndexPlan:
    return IncrementalIndexPlan(
        snapshot_id=snapshot_id,
        source=source,
        previous_state=None,
        current_state=object(),
        workspace_inventory=object(),
        source_docs_manifest=object(),
        collected_profiles=object(),
    )


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

        def plan(self, *, snapshot_id: str, source: str, progress_callback) -> IncrementalIndexPlan:
            return _empty_incremental_plan(snapshot_id=snapshot_id, source=source)

        def run(
            self,
            *,
            snapshot_id: str,
            source: str,
            progress_callback,
            plan: IncrementalIndexPlan | None = None,
            run_context: object | None = None,
        ) -> IncrementalIndexResult:
            assert snapshot_id == "current"
            assert source == "all"
            assert progress_callback is not None
            assert plan is not None
            assert run_context is not None
            return IncrementalIndexResult(
                schema_version="incremental_index_result.v1",
                snapshot_id=snapshot_id,
                result_status="ready",
                plan=plan,
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
    assert payload["job"]["schema_version"] == "index_job_contract.v1"
    assert payload["job"]["resume_policy"]["mode"] == "auto"
    assert payload["job"]["resumed"] is False
    assert payload["job"]["job_id"].startswith("index:")
    assert payload["job"]["plan_signature"]["digest"].startswith("sha256:")
    assert payload["job"]["tasks_total"] == 0
    assert payload["job"]["tasks_applied"] == 0

    with sqlite3.connect(workdir / "local" / "db" / "jobs.db") as connection:
        row = connection.execute(
            "SELECT status, metadata_json FROM job WHERE job_id = ?",
            (payload["job"]["job_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "ready"
    assert json.loads(row[1])["plan_signature"].startswith("sha256:")


def test_index_incremental_json_accepts_explicit_job_id_policy(
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

        def plan(self, *, snapshot_id: str, source: str, progress_callback) -> IncrementalIndexPlan:
            return _empty_incremental_plan(snapshot_id=snapshot_id, source=source)

        def run(
            self,
            *,
            snapshot_id: str,
            source: str,
            progress_callback,
            plan: IncrementalIndexPlan | None = None,
            run_context: object | None = None,
        ) -> IncrementalIndexResult:
            return IncrementalIndexResult(
                schema_version="incremental_index_result.v1",
                snapshot_id=snapshot_id,
                result_status="ready",
                plan=plan or _empty_incremental_plan(snapshot_id=snapshot_id, source=source),
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
            "--no-resume",
            "--job-id",
            "index:ci-123",
            "--format",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["job"]["job_id"] == "index:ci-123"
    assert payload["job"]["resume_policy"]["mode"] == "disabled"
    assert payload["job"]["resume_policy"]["resume_enabled"] is False

    with sqlite3.connect(workdir / "local" / "db" / "jobs.db") as connection:
        status = connection.execute(
            "SELECT status FROM job WHERE job_id = 'index:ci-123'"
        ).fetchone()
    assert status == ("ready",)


def test_index_incremental_json_auto_resumes_interrupted_job(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    calls = {"count": 0}

    class DummyPipeline:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def plan(self, *, snapshot_id: str, source: str, progress_callback) -> IncrementalIndexPlan:
            return _empty_incremental_plan(snapshot_id=snapshot_id, source=source)

        def run(
            self,
            *,
            snapshot_id: str,
            source: str,
            progress_callback,
            plan: IncrementalIndexPlan | None = None,
            run_context: object | None = None,
        ) -> IncrementalIndexResult:
            calls["count"] += 1
            if calls["count"] == 1:
                raise KeyboardInterrupt
            return IncrementalIndexResult(
                schema_version="incremental_index_result.v1",
                snapshot_id=snapshot_id,
                result_status="ready",
                plan=plan or _empty_incremental_plan(snapshot_id=snapshot_id, source=source),
            )

    monkeypatch.setattr("active_knowledge_server.cli.IncrementalIndexPipeline", DummyPipeline)

    first_exit = main(
        [
            "index",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--incremental",
            "--no-resume",
            "--job-id",
            "index:auto-resume",
            "--format",
            "json",
        ]
    )
    first_payload = json.loads(capsys.readouterr().out)

    second_exit = main(
        [
            "index",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--incremental",
            "--resume",
            "auto",
            "--format",
            "json",
        ]
    )
    second_payload = json.loads(capsys.readouterr().out)

    assert first_exit == 130
    assert first_payload["job"]["job_id"] == "index:auto-resume"
    assert second_exit == 0
    assert second_payload["job"]["job_id"] == "index:auto-resume"
    assert second_payload["job"]["resumed"] is True

    with sqlite3.connect(workdir / "local" / "db" / "jobs.db") as connection:
        row = connection.execute(
            "SELECT status, metadata_json FROM job WHERE job_id = 'index:auto-resume'"
        ).fetchone()
    assert row is not None
    assert row[0] == "ready"
    assert json.loads(row[1])["resume_count"] == 1


def test_index_incremental_restart_supersedes_compatible_job(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    calls = {"count": 0}

    class DummyPipeline:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def plan(self, *, snapshot_id: str, source: str, progress_callback) -> IncrementalIndexPlan:
            return _empty_incremental_plan(snapshot_id=snapshot_id, source=source)

        def run(
            self,
            *,
            snapshot_id: str,
            source: str,
            progress_callback,
            plan: IncrementalIndexPlan | None = None,
            run_context: object | None = None,
        ) -> IncrementalIndexResult:
            calls["count"] += 1
            if calls["count"] == 1:
                raise KeyboardInterrupt
            return IncrementalIndexResult(
                schema_version="incremental_index_result.v1",
                snapshot_id=snapshot_id,
                result_status="ready",
                plan=plan or _empty_incremental_plan(snapshot_id=snapshot_id, source=source),
            )

    monkeypatch.setattr("active_knowledge_server.cli.IncrementalIndexPipeline", DummyPipeline)

    assert (
        main(
            [
                "index",
                "--workdir",
                str(workdir),
                "--workspace",
                str(workspace),
                "--source-docs-root",
                str(source_docs),
                "--incremental",
                "--no-resume",
                "--job-id",
                "index:restart-old",
                "--format",
                "json",
            ]
        )
        == 130
    )
    capsys.readouterr()

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
            "--restart",
            "--job-id",
            "index:restart-new",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["job"]["job_id"] == "index:restart-new"
    assert payload["job"]["resumed"] is False

    with sqlite3.connect(workdir / "local" / "db" / "jobs.db") as connection:
        old_row = connection.execute(
            "SELECT status, metadata_json FROM job WHERE job_id = 'index:restart-old'"
        ).fetchone()
        new_row = connection.execute(
            "SELECT status FROM job WHERE job_id = 'index:restart-new'"
        ).fetchone()
    assert old_row is not None
    assert old_row[0] == "failed"
    old_metadata = json.loads(old_row[1])
    assert old_metadata["execution_state"] == "superseded"
    assert old_metadata["superseded_by_job_id"] == "index:restart-new"
    assert new_row == ("ready",)


def test_index_incremental_sigterm_resume_reuses_checkpointed_work_and_validates(
    tmp_path: Path,
) -> None:
    project_root = _project_root()
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    support = tmp_path / "support"
    sitecustomize = support / "sitecustomize.py"
    workspace.mkdir()
    source_docs.mkdir()
    support.mkdir()
    _write_cli_workspace_fixture(workspace)
    sitecustomize.write_text(
        """import os
import time

if os.environ.get("ACTIVE_KB_TEST_SLEEP_AFTER_FIRST_CHECKPOINT") == "1":
    import active_knowledge_server.indexing.pipeline as pipeline_module

    _original = pipeline_module.record_task_applied_checkpoint
    _state = {"count": 0}

    def _wrapped(*args, **kwargs):
        result = _original(*args, **kwargs)
        _state["count"] += 1
        if _state["count"] == 1:
            time.sleep(float(os.environ.get("ACTIVE_KB_TEST_SLEEP_SECONDS", "30")))
        return result

    pipeline_module.record_task_applied_checkpoint = _wrapped
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": _pythonpath_with(support, project_root / "src"),
    }

    (workspace / "components" / "health" / "main.c").write_text(
        """#include "health.h"

#define HEALTH_SIGTERM_MAIN 7

int health_main(void)
{
    return HEALTH_SIGTERM_MAIN;
}
""",
        encoding="utf-8",
    )
    (workspace / "components" / "health" / "bt.c").write_text(
        """#include "health.h"

#define HEALTH_SIGTERM_BT 9

int health_bt(void)
{
    return HEALTH_SIGTERM_BT;
}
""",
        encoding="utf-8",
    )
    first_env = {
        **env,
        "ACTIVE_KB_TEST_SLEEP_AFTER_FIRST_CHECKPOINT": "1",
        "ACTIVE_KB_TEST_SLEEP_SECONDS": "30",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "active_knowledge_server.cli",
            "index",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--incremental",
            "--source",
            "code",
            "--no-resume",
            "--job-id",
            "index:sigterm-resume",
            "--format",
            "json",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=project_root,
        env=first_env,
    )
    jobs_path = workdir / "local" / "db" / "jobs.db"
    try:
        _wait_for_applied_checkpoint(
            jobs_path,
            "index:sigterm-resume",
            timeout_seconds=15,
        )
        proc.send_signal(signal.SIGTERM)
        interrupted_stdout, interrupted_stderr = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    interrupted_payload = json.loads(interrupted_stdout)
    assert proc.returncode == 130
    assert interrupted_payload["status"] == "interrupted"
    assert interrupted_payload["job"]["job_id"] == "index:sigterm-resume"
    assert interrupted_payload["job"]["status"] == "interrupted"

    resumed = subprocess.run(
        [
            sys.executable,
            "-m",
            "active_knowledge_server.cli",
            "index",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--incremental",
            "--source",
            "code",
            "--resume",
            "auto",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=project_root,
        env=env,
    )
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["status"] == "ok"
    assert resumed_payload["job"]["job_id"] == "index:sigterm-resume"
    assert resumed_payload["job"]["resumed"] is True
    assert resumed_payload["job"]["tasks_skipped"] is not None
    assert resumed_payload["job"]["tasks_skipped"] > 0

    validate = subprocess.run(
        [
            sys.executable,
            "-m",
            "active_knowledge_server.cli",
            "validate",
            "--workdir",
            str(workdir),
            "--workspace",
            str(workspace),
            "--source-docs-root",
            str(source_docs),
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=project_root,
        env=env,
    )
    validate_payload = json.loads(validate.stdout)
    assert validate_payload["status"] == "ok"
    assert validate_payload["storage_report"]["status"] != "blocked"


def test_index_incremental_json_can_render_progress_to_stderr(
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

        def plan(self, *, snapshot_id: str, source: str, progress_callback) -> IncrementalIndexPlan:
            return _empty_incremental_plan(snapshot_id=snapshot_id, source=source)

        def run(
            self,
            *,
            snapshot_id: str,
            source: str,
            progress_callback,
            plan: IncrementalIndexPlan | None = None,
            run_context: object | None = None,
        ) -> IncrementalIndexResult:
            progress_callback(
                IndexProgressEvent(
                    phase="done",
                    stage_total=1,
                    stage_done=1,
                    global_total=1,
                    global_done=1,
                    message="Incremental indexing finished",
                )
            )
            return IncrementalIndexResult(
                schema_version="incremental_index_result.v1",
                snapshot_id=snapshot_id,
                result_status="ready",
                plan=plan or _empty_incremental_plan(snapshot_id=snapshot_id, source=source),
            )

    monkeypatch.setattr("active_knowledge_server.cli.IncrementalIndexPipeline", DummyPipeline)
    monkeypatch.setattr(
        "active_knowledge_server.cli.resolve_index_progress_output_mode",
        lambda *, output_format: "text_plain",
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
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "index"
    assert "Incremental indexing finished" in captured.err
    assert payload["job"]["resume_policy"]["mode"] == "auto"


def test_index_incremental_json_interrupt_keeps_stdout_payload(
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

        def plan(self, *, snapshot_id: str, source: str, progress_callback) -> IncrementalIndexPlan:
            return _empty_incremental_plan(snapshot_id=snapshot_id, source=source)

        def run(
            self,
            *,
            snapshot_id: str,
            source: str,
            progress_callback,
            plan: IncrementalIndexPlan | None = None,
            run_context: object | None = None,
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
        "active_knowledge_server.cli.resolve_index_progress_output_mode",
        lambda *, output_format: "text_plain",
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
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 130
    assert payload["status"] == "interrupted"
    assert payload["job"]["status"] == "interrupted"
    assert payload["job"]["resume_policy"]["mode"] == "auto"
    assert "Index interrupted." in captured.err


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


def test_resolve_index_progress_output_mode_contract() -> None:
    class DummyStream:
        def __init__(self, *, is_tty: bool) -> None:
            self._is_tty = is_tty

        def isatty(self) -> bool:
            return self._is_tty

    assert (
        resolve_index_progress_output_mode(
            output_format="json",
            progress_stream=DummyStream(is_tty=True),
        )
        == "text_dynamic"
    )
    assert (
        resolve_index_progress_output_mode(
            output_format="json",
            progress_stream=DummyStream(is_tty=True),
            rich_available=False,
        )
        == "text_plain"
    )
    assert (
        resolve_index_progress_output_mode(
            output_format="json",
            progress_stream=DummyStream(is_tty=False),
        )
        == "none"
    )
    assert (
        resolve_index_progress_output_mode(
            output_format="text",
            output_stream=DummyStream(is_tty=False),
        )
        == "text_plain"
    )


def test_full_local_index_writes_overlay_when_empty_baseline_db_exists(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    baseline_db = workdir / "baseline" / "db" / "metadata.db"
    overlay_db = workdir / "local" / "db" / "overlay.db"
    workspace.mkdir()
    source_docs.mkdir()
    baseline_db.parent.mkdir(parents=True)
    sqlite3.connect(baseline_db).close()
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "index",
            "--full",
            "--target",
            "local",
            "--source",
            "docs",
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

    payload = json.loads(capsys.readouterr().out)
    with sqlite3.connect(overlay_db) as connection:
        overlay_snapshot_count = connection.execute("SELECT COUNT(*) FROM snapshot").fetchone()
    with sqlite3.connect(baseline_db) as connection:
        baseline_snapshot_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'snapshot'"
        ).fetchone()

    assert exit_code == 0
    assert payload["target"] == "local"
    assert payload["result"]["target"] == "local"
    assert overlay_snapshot_count is not None
    assert int(overlay_snapshot_count[0]) > 0
    assert baseline_snapshot_table is None


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
    assert manifest_payload["source_docs_hash"].startswith("sha256:")
    assert manifest_payload["versions"]["mcp_schema_version"] == "mcp_interface.v1"
    assert manifest_payload["versions"]["parser_versions"]["doc"] == "doc_parser.v1"
    assert manifest_payload["versions"]["extractor_versions"]["code_indexer"] == "code_indexer.v1"
    assert manifest_payload["source_docs"]["manifest_hash"] == manifest_payload["source_docs_hash"]


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

        def plan(self, *, snapshot_id: str, source: str, progress_callback) -> IncrementalIndexPlan:
            return _empty_incremental_plan(snapshot_id=snapshot_id, source=source)

        def run(
            self,
            *,
            snapshot_id: str,
            source: str,
            progress_callback,
            plan: IncrementalIndexPlan | None = None,
            run_context: object | None = None,
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
        "active_knowledge_server.cli.resolve_index_progress_output_mode",
        lambda *, output_format: "text_plain",
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


def test_release_checklist_json_reports_pass_with_complete_artifacts(
    tmp_path: Path,
    capsys,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    readme_path = tmp_path / "README.md"
    remote_config_path = tmp_path / "remote-shared.yaml"
    quality_path = tmp_path / "quality.json"
    performance_path = tmp_path / "performance.json"
    stability_path = tmp_path / "stability.json"

    readme_path.write_text(
        "\n".join(
            (
                "active-kb init",
                "active-kb index",
                "active-kb serve",
                "active-kb validate",
                "active-kb clean",
                "active-kb migrate",
            )
        ),
        encoding="utf-8",
    )
    remote_config_path.write_text(
        _remote_shared_config_fixture(),
        encoding="utf-8",
    )
    quality_path.write_text(json.dumps(_quality_gate_report().to_dict()), encoding="utf-8")
    performance_path.write_text(json.dumps(_performance_gate_report().to_dict()), encoding="utf-8")
    stability_path.write_text(json.dumps(_stability_gate_report().to_dict()), encoding="utf-8")

    manifest_path = workdir / "baseline" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(_complete_release_manifest(source_docs_root=source_docs), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "release",
            "checklist",
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
            "--stability-report",
            str(stability_path),
            "--readme",
            str(readme_path),
            "--remote-config",
            str(remote_config_path),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["command"] == "release checklist"
    assert payload["status"] == "pass"
    assert all(check["status"] == "pass" for check in payload["checks"])


def test_release_checklist_json_blocks_on_tracked_local_files(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    readme_path = tmp_path / "README.md"
    remote_config_path = tmp_path / "remote-shared.yaml"
    quality_path = tmp_path / "quality.json"
    performance_path = tmp_path / "performance.json"
    stability_path = tmp_path / "stability.json"

    readme_path.write_text(
        "\n".join(
            (
                "active-kb init",
                "active-kb index",
                "active-kb serve",
                "active-kb validate",
                "active-kb clean",
                "active-kb migrate",
            )
        ),
        encoding="utf-8",
    )
    remote_config_path.write_text(
        _remote_shared_config_fixture(),
        encoding="utf-8",
    )
    quality_path.write_text(json.dumps(_quality_gate_report().to_dict()), encoding="utf-8")
    performance_path.write_text(json.dumps(_performance_gate_report().to_dict()), encoding="utf-8")
    stability_path.write_text(json.dumps(_stability_gate_report().to_dict()), encoding="utf-8")

    manifest_path = workdir / "baseline" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(_complete_release_manifest(source_docs_root=source_docs), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "active_knowledge_server.cli.inspect_tracked_local_files",
        lambda *_args, **_kwargs: type(
            "DummyWarning",
            (),
            {"details": ("artifacts/stability/hybrid-query-500.json",)},
        )(),
    )

    exit_code = main(
        [
            "release",
            "checklist",
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
            "--stability-report",
            str(stability_path),
            "--readme",
            str(readme_path),
            "--remote-config",
            str(remote_config_path),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    local_check = next(
        check for check in payload["checks"] if check["check_id"] == "local_artifacts_excluded"
    )

    assert exit_code == 1
    assert payload["status"] == "fail"
    assert local_check["status"] == "fail"


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


def _quality_gate_report() -> EvalRunReport:
    report = _quality_report()
    metrics = dict(report.metrics)
    metrics["quality_gate"] = {
        **metrics["quality_gate"],
        "passed": True,
    }
    return report.model_copy(update={"metrics": metrics})


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


def _performance_gate_report() -> EvalRunReport:
    report = _performance_report()
    metrics = dict(report.metrics)
    metrics["performance_gate"] = {
        **metrics["performance_gate"],
        "passed": True,
    }
    return report.model_copy(update={"metrics": metrics})


def _stability_gate_report() -> EvalRunReport:
    return EvalRunReport(
        gate_id="stability",
        suite_id="stability-mixed-query-v1",
        status="pass",
        started_at="2026-05-22T00:00:00Z",
        finished_at="2026-05-22T08:00:01Z",
        cases_file="eval/stability_cases.yaml",
        metrics={
            "stability_gate": {
                "passed": True,
                "release_window": {
                    "passed": True,
                    "actual_soak_seconds": 28800,
                    "actual_mixed_query_count": 500,
                },
            }
        },
    )


def _complete_release_manifest(*, source_docs_root: Path) -> dict[str, object]:
    return {
        "schema_version": "active_kb_baseline_manifest.v1",
        "baseline_id": "release-20260526",
        "default_profile": "auto",
        "published_at": "2026-05-26T00:00:00Z",
        "snapshot_id": "current",
        "source": "all",
        "publish_mode": "publish",
        "snapshots": ["current"],
        "profiles": ["watch"],
        "source_docs_hash": "sha256:release",
        "parser_version": "c_family_parser.v1+doc_parser.v1+kconfig_parser.v1+makefile_parser.v1",
        "extractor_version": (
            "snapshot_collector.v1+profile_collector.v1+code_indexer.v1+doc_indexer.v1+"
            "profile_conditioned_relations.v1+workspace_map.v1"
        ),
        "embedding_model": "bge-m3",
        "embedding_model_version": "bge-m3",
        "artifacts": {
            "metadata": "db/metadata.db",
            "vectors": "vectors/lancedb",
            "workspace_map": "artifacts/workspace-maps/current.json",
        },
        "versions": {
            "config_schema_version": "0.1",
            "query_result_schema_version": "query_result.v1",
            "mcp_schema_version": "mcp_interface.v1",
            "embedding_model_version": "bge-m3",
            "parser_versions": {
                "c_family": "c_family_parser.v1",
                "doc": "doc_parser.v1",
                "kconfig": "kconfig_parser.v1",
                "makefile": "makefile_parser.v1",
            },
            "extractor_versions": {
                "snapshot_collector": "snapshot_collector.v1",
                "profile_collector": "profile_collector.v1",
                "code_indexer": "code_indexer.v1",
                "doc_indexer": "doc_indexer.v1",
                "profile_conditioned_relations": "profile_conditioned_relations.v1",
                "workspace_map": "workspace_map.v1",
            },
        },
        "source_docs": {
            "schema_version": "source_docs_manifest.v1",
            "manifest_hash": "sha256:release",
            "root": str(source_docs_root),
            "file_count": 0,
            "supported_categories": [],
            "present_categories": [],
            "file_count_by_category": {},
        },
    }


def _remote_shared_config_fixture() -> str:
    return "\n".join(
        (
            "deployment_mode: remote_shared",
            "server:",
            "  transport: streamable-http",
            "  expose_ops_tools: false",
            "  http:",
            "    host: 0.0.0.0",
            "    port: 8765",
            "    require_auth: true",
            "    auth_provider: token",
            "    token:",
            "      env: ACTIVE_KB_AUTH_TOKEN",
            "    allowed_origins:",
            "      - https://chatgpt.com",
            "runtime:",
            "  workdir: .active-kb",
            "  baseline_dir: .active-kb/baseline",
            "  local_dir: .active-kb/local",
            "  source_root: .",
            "  source_docs_root: knowledge-sources",
            "project:",
            "  id: active",
            "  display_name: Active",
            "  workspace_root: workspace",
            "storage:",
            "  baseline:",
            "    manifest: .active-kb/baseline/manifest.json",
            "  metadata:",
            "    backend: sqlite",
            "    path: .active-kb/baseline/db/metadata.db",
            "    mode: readonly",
            "  overlay:",
            "    backend: sqlite",
            "    path: .active-kb/local/db/overlay.db",
            "  jobs:",
            "    backend: sqlite",
            "    path: .active-kb/local/db/jobs.db",
            "  vector:",
            "    backend: lancedb",
            "    path: .active-kb/baseline/vectors/lancedb",
            "    mode: readonly",
            "  vector_delta:",
            "    backend: lancedb",
            "    path: .active-kb/local/vectors/lancedb-delta",
            "  artifacts_root: .active-kb/baseline/artifacts",
            "  local_artifacts_root: .active-kb/local/artifacts",
            "  cache_root: .active-kb/local/cache",
            "indexing:",
            "  embeddings:",
            "    enabled: true",
            "    model: bge-m3",
            "query:",
            "  hybrid:",
            "    rerank: lightweight",
            "security:",
            "  audit:",
            "    enabled: true",
        )
    )
