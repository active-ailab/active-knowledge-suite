#!/usr/bin/env python3
"""Opt-in resume smoke harness for a real local workspace."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from active_knowledge_server.config.loader import ConfigDict, resolve_config, set_nested


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = _repo_root()
    parser = argparse.ArgumentParser(
        description=(
            "Run one interrupted index attempt and one --resume auto retry, then "
            "verify resumed=true and tasks_skipped>0."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=repo_root / "examples" / "local-single-user.yaml",
        help="Config YAML path. Defaults to examples/local-single-user.yaml.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="Workspace root override. Defaults to project.workspace_root from config.",
    )
    parser.add_argument("--source-docs-root", type=Path, help="Optional source docs root override.")
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path("/tmp/active-kb-ar4-04-resume-smoke"),
        help="Isolated workdir for the smoke run.",
    )
    parser.add_argument(
        "--source",
        choices=("all", "code", "docs"),
        default="code",
        help="Source family to index. Defaults to code for a faster smoke run.",
    )
    parser.add_argument(
        "--job-id",
        default="index:manual-resume-smoke",
        help="Persisted job id used by the interrupted first run.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=30.0,
        help="How long the child process sleeps after the first applied checkpoint.",
    )
    parser.add_argument(
        "--checkpoint-timeout-seconds",
        type=float,
        default=120.0,
        help="How long to wait for the first applied checkpoint before failing.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove the smoke workdir before starting.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON report path. Prints to stdout when omitted.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.clean and args.workdir.exists():
        shutil.rmtree(args.workdir)
    args.workdir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = args.workdir / "resume-smoke-artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    resolved = resolve_config(config_path=args.config)
    workspace = args.workspace or resolved.model.project.workspace_root
    source_docs_root = args.source_docs_root or resolved.model.runtime.source_docs_root
    smoke_config_path = args.workdir / "resume-smoke.yaml"
    write_smoke_config(
        base_config_path=args.config,
        smoke_config_path=smoke_config_path,
        workspace=workspace,
        source_docs_root=source_docs_root,
        workdir=args.workdir,
    )
    jobs_path = args.workdir / "local" / "db" / "jobs.db"

    first_command = build_index_command(
        config=smoke_config_path,
        source=args.source,
        resume_args=("--no-resume", "--job-id", args.job_id),
    )
    resume_command = build_index_command(
        config=smoke_config_path,
        source=args.source,
        resume_args=("--resume", "auto"),
    )
    validate_command = build_validate_like_command(
        "validate",
        config=smoke_config_path,
        extra_args=("--strict", "--format", "json"),
    )
    status_command = build_validate_like_command(
        "status",
        config=smoke_config_path,
        extra_args=("--format", "json"),
    )

    interrupted = run_interrupted_index(
        command=first_command,
        job_id=args.job_id,
        jobs_path=jobs_path,
        support_root=artifacts_dir / "support",
        sleep_seconds=args.sleep_seconds,
        checkpoint_timeout_seconds=args.checkpoint_timeout_seconds,
        stdout_path=artifacts_dir / "first-run.stdout.json",
        stderr_path=artifacts_dir / "first-run.stderr.log",
    )
    resumed = run_json_command(
        resume_command,
        stdout_path=artifacts_dir / "resume.stdout.json",
        stderr_path=artifacts_dir / "resume.stderr.log",
    )
    validate = run_json_command(
        validate_command,
        stdout_path=artifacts_dir / "validate.stdout.json",
        stderr_path=artifacts_dir / "validate.stderr.log",
    )
    status = run_json_command(
        status_command,
        stdout_path=artifacts_dir / "status.stdout.json",
        stderr_path=artifacts_dir / "status.stderr.log",
    )

    resumed_job = _mapping(resumed["payload"].get("job"))
    validate_payload = validate["payload"]
    status_payload = status["payload"]
    interrupted_payload = interrupted["payload"]

    latest_job = _mapping(status_payload.get("jobs", {})).get("latest")
    latest_job_map = _mapping(latest_job)

    checks = {
        "interrupted_returncode": interrupted["returncode"] == 130,
        "interrupted_status": interrupted_payload.get("status") == "interrupted",
        "interrupted_job_id": _mapping(interrupted_payload.get("job")).get("job_id") == args.job_id,
        "resume_returncode": resumed["returncode"] == 0,
        "resume_status_ok": resumed["payload"].get("status") == "ok",
        "resume_job_id": resumed_job.get("job_id") == args.job_id,
        "resume_flag": resumed_job.get("resumed") is True,
        "resume_tasks_skipped": int(resumed_job.get("tasks_skipped") or 0) > 0,
        "validate_returncode": validate["returncode"] == 0,
        "validate_status_ok": validate_payload.get("status") == "ok",
        "status_returncode": status["returncode"] == 0,
        "status_status_ok": status_payload.get("status") == "ok",
        "latest_job_matches": latest_job_map.get("job_id") == args.job_id,
    }
    report = {
        "schema_version": "index_resume_smoke_report.v1",
        "status": "ok" if all(checks.values()) else "error",
        "scenario": {
            "config": str(args.config),
            "smoke_config": str(smoke_config_path),
            "workspace": str(workspace),
            "source_docs_root": str(source_docs_root),
            "workdir": str(args.workdir),
            "source": args.source,
            "job_id": args.job_id,
            "interrupt_kind": "sigterm_after_first_applied_checkpoint",
        },
        "commands": {
            "first_run": shlex.join(first_command),
            "resume_run": shlex.join(resume_command),
            "validate": shlex.join(validate_command),
            "status": shlex.join(status_command),
        },
        "checks": checks,
        "artifacts": {
            "dir": str(artifacts_dir),
            "first_run_stdout": str(artifacts_dir / "first-run.stdout.json"),
            "first_run_stderr": str(artifacts_dir / "first-run.stderr.log"),
            "resume_stdout": str(artifacts_dir / "resume.stdout.json"),
            "resume_stderr": str(artifacts_dir / "resume.stderr.log"),
            "validate_stdout": str(artifacts_dir / "validate.stdout.json"),
            "status_stdout": str(artifacts_dir / "status.stdout.json"),
        },
        "interrupted": summarize_command_result(interrupted),
        "resumed": summarize_command_result(resumed),
        "validate": summarize_command_result(validate),
        "status_result": summarize_command_result(status),
    }
    line = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(line + "\n", encoding="utf-8")
    else:
        print(line)
    return 0 if report["status"] == "ok" else 1


def build_index_command(
    *,
    config: Path,
    source: str,
    resume_args: tuple[str, ...],
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "active_knowledge_server.cli",
        "index",
        "--config",
        str(config),
        "--incremental",
        "--source",
        source,
        "--format",
        "json",
        *resume_args,
    ]
    return command


def build_validate_like_command(
    verb: str,
    *,
    config: Path,
    extra_args: tuple[str, ...],
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "active_knowledge_server.cli",
        verb,
        "--config",
        str(config),
        *extra_args,
    ]
    return command


def write_smoke_config(
    *,
    base_config_path: Path,
    smoke_config_path: Path,
    workspace: Path,
    source_docs_root: Path,
    workdir: Path,
) -> None:
    data = yaml.safe_load(base_config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"expected mapping config in {base_config_path}")

    overrides: ConfigDict = data
    baseline_dir = workdir / "baseline"
    local_dir = workdir / "local"
    set_nested(overrides, ("runtime", "workdir"), str(workdir))
    set_nested(overrides, ("runtime", "baseline_dir"), str(baseline_dir))
    set_nested(overrides, ("runtime", "local_dir"), str(local_dir))
    set_nested(overrides, ("runtime", "source_docs_root"), str(source_docs_root))
    set_nested(overrides, ("project", "workspace_root"), str(workspace))
    set_nested(overrides, ("storage", "baseline", "manifest"), str(baseline_dir / "manifest.json"))
    set_nested(overrides, ("storage", "metadata", "path"), str(baseline_dir / "db" / "metadata.db"))
    set_nested(overrides, ("storage", "overlay", "path"), str(local_dir / "db" / "overlay.db"))
    set_nested(overrides, ("storage", "jobs", "path"), str(local_dir / "db" / "jobs.db"))
    set_nested(overrides, ("storage", "vector", "path"), str(baseline_dir / "vectors" / "lancedb"))
    set_nested(
        overrides,
        ("storage", "vector_delta", "path"),
        str(local_dir / "vectors" / "lancedb-delta"),
    )
    set_nested(overrides, ("storage", "artifacts_root"), str(baseline_dir / "artifacts"))
    set_nested(overrides, ("storage", "local_artifacts_root"), str(local_dir / "artifacts"))
    set_nested(overrides, ("storage", "cache_root"), str(local_dir / "cache"))
    smoke_config_path.write_text(
        yaml.safe_dump(overrides, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def run_interrupted_index(
    *,
    command: list[str],
    job_id: str,
    jobs_path: Path,
    support_root: Path,
    sleep_seconds: float,
    checkpoint_timeout_seconds: float,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    env = {
        **os.environ,
        "ACTIVE_KB_TEST_SLEEP_AFTER_FIRST_CHECKPOINT": "1",
        "ACTIVE_KB_TEST_SLEEP_SECONDS": str(sleep_seconds),
        "PYTHONPATH": _pythonpath_with_sitecustomize(support_root),
    }
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=_project_root(),
        env=env,
    )
    try:
        wait_for_applied_checkpoint_or_exit(
            proc,
            jobs_path,
            job_id=job_id,
            timeout_seconds=checkpoint_timeout_seconds,
        )
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=max(sleep_seconds, 15.0))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    payload = _json_loads(stdout, source=stdout_path)
    return {
        "command": command,
        "returncode": proc.returncode,
        "payload": payload,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def run_json_command(
    command: list[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=_project_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    payload = _json_loads(result.stdout, source=stdout_path)
    return {
        "command": command,
        "returncode": result.returncode,
        "payload": payload,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def wait_for_applied_checkpoint_or_exit(
    proc: subprocess.Popen[str],
    jobs_path: Path,
    *,
    job_id: str,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        returncode = proc.poll()
        if returncode is not None:
            stdout, stderr = proc.communicate(timeout=5)
            raise RuntimeError(
                "index process exited before the first applied checkpoint "
                f"(returncode={returncode})\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
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
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for an applied checkpoint in {jobs_path}")


def _pythonpath_with_sitecustomize(support_root: Path) -> str:
    support_root.mkdir(parents=True, exist_ok=True)
    sitecustomize = support_root / "sitecustomize.py"
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
    parts = [str(support_root)]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def _json_loads(raw: str, *, source: Path) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"failed to parse JSON from {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object in {source}, got {type(payload).__name__}")
    return payload


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def summarize_command_result(result: dict[str, Any]) -> dict[str, Any]:
    payload = result["payload"]
    job = _mapping(payload.get("job"))
    return {
        "returncode": result["returncode"],
        "status": payload.get("status"),
        "job_id": job.get("job_id"),
        "resumed": job.get("resumed"),
        "tasks_skipped": job.get("tasks_skipped"),
        "stdout_path": result["stdout_path"],
        "stderr_path": result["stderr_path"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
