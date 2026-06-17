#!/usr/bin/env python3
"""AR4-03 acceptance harness for fresh vs crash/resume logical equivalence."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

from active_knowledge_server.cli import (
    IndexCommandInterrupted,
    _run_index_command,
    main as cli_main,
)
from active_knowledge_server.config.loader import ConfigDict, resolve_config, set_nested
from active_knowledge_server.eval.index_consistency import (
    compare_live_index_collections,
    load_live_index_collections,
    summarize_live_index_collections,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare one fresh index run with one crash/resume run.",
    )
    parser.add_argument("--config", type=Path, required=True, help="Baseline config YAML path.")
    parser.add_argument("--workspace", type=Path, required=True, help="Workspace root to index.")
    parser.add_argument("--source-docs-root", type=Path, help="Optional source docs override.")
    parser.add_argument("--profile", help="Optional default profile override.")
    parser.add_argument(
        "--source",
        choices=("all", "code", "docs"),
        default="all",
        help="Source family to compare.",
    )
    parser.add_argument(
        "--fresh-workers",
        default="1",
        help="Worker count for the one-shot fresh run. Defaults to 1.",
    )
    parser.add_argument(
        "--resume-workers",
        default="auto",
        help="Worker count for the crash/resume scenario. Defaults to auto.",
    )
    parser.add_argument(
        "--parallel-mode",
        choices=("thread", "process", "hybrid"),
        help="Optional collect executor mode override.",
    )
    parser.add_argument(
        "--writer-batch-size",
        type=int,
        help="Optional writer batch size override.",
    )
    parser.add_argument(
        "--writer-max-files-per-transaction",
        type=int,
        help="Optional writer max-files-per-transaction override.",
    )
    parser.add_argument(
        "--writer-max-records-per-transaction",
        type=int,
        help="Optional writer max-records-per-transaction override.",
    )
    parser.add_argument(
        "--writer-commit-interval-ms",
        type=int,
        help="Optional writer commit interval override.",
    )
    parser.add_argument(
        "--interrupt-after-task-percent",
        type=int,
        default=70,
        help="Interrupt the first resume scenario after this applied-task percentage.",
    )
    parser.add_argument(
        "--bench-root",
        type=Path,
        help="Optional root dir for the fresh/resume workdirs and report artifacts.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path. Prints to stdout when omitted.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.interrupt_after_task_percent <= 0 or args.interrupt_after_task_percent >= 100:
        raise ValueError("--interrupt-after-task-percent must be between 1 and 99")
    with benchmark_root(args.bench_root) as root:
        bench_root = Path(root)
        fresh_workdir = bench_root / "fresh"
        resumed_workdir = bench_root / "resume"
        if fresh_workdir.exists():
            shutil.rmtree(fresh_workdir)
        if resumed_workdir.exists():
            shutil.rmtree(resumed_workdir)
        fresh_workdir.mkdir(parents=True, exist_ok=True)
        resumed_workdir.mkdir(parents=True, exist_ok=True)

        fresh_resolved = resolve_acceptance_config(
            args=args,
            workdir=fresh_workdir,
            workers=args.fresh_workers,
        )
        fresh_run = run_index_once(
            resolved=fresh_resolved,
            args=args,
            workdir=fresh_workdir,
            resume_policy=_disabled_resume_policy(planned_job_id="ar4-03-fresh"),
        )
        fresh_validate = run_cli_json(
            "validate",
            args=args,
            workdir=fresh_workdir,
            extra_args=["--strict", "--format", "json"],
        )
        fresh_status = run_cli_json(
            "status",
            args=args,
            workdir=fresh_workdir,
            extra_args=["--format", "json"],
        )
        fresh_collections = load_live_index_collections(
            fresh_resolved.model,
            cwd=Path.cwd(),
        )

        resumed_resolved = resolve_acceptance_config(
            args=args,
            workdir=resumed_workdir,
            workers=args.resume_workers,
        )
        interrupted_run = run_index_once(
            resolved=resumed_resolved,
            args=args,
            workdir=resumed_workdir,
            resume_policy=_disabled_resume_policy(planned_job_id="ar4-03-resume"),
            interrupt_after_task_percent=args.interrupt_after_task_percent,
        )
        if not interrupted_run["interrupted"]:
            raise RuntimeError("resume scenario completed without an interruption")
        resumed_run = run_index_once(
            resolved=resumed_resolved,
            args=args,
            workdir=resumed_workdir,
            resume_policy=_auto_resume_policy(),
        )
        resumed_validate = run_cli_json(
            "validate",
            args=args,
            workdir=resumed_workdir,
            extra_args=["--strict", "--format", "json"],
        )
        resumed_status = run_cli_json(
            "status",
            args=args,
            workdir=resumed_workdir,
            extra_args=["--format", "json"],
        )
        resumed_collections = load_live_index_collections(
            resumed_resolved.model,
            cwd=Path.cwd(),
        )

        comparison = compare_live_index_collections(fresh_collections, resumed_collections)
        resume_job_check = build_resume_job_check(
            interrupted_run=interrupted_run,
            resumed_run=resumed_run,
            resumed_status=resumed_status,
        )
        report = {
            "schema_version": "index_resume_consistency_report.v1",
            "status": report_status(
                comparison=comparison,
                fresh_validate=fresh_validate,
                resumed_validate=resumed_validate,
                resume_job_check=resume_job_check,
            ),
            "scenario": {
                "config": str(args.config),
                "workspace": str(args.workspace),
                "source_docs_root": (
                    None if args.source_docs_root is None else str(args.source_docs_root)
                ),
                "profile": args.profile,
                "source": args.source,
                "fresh_workers": args.fresh_workers,
                "resume_workers": args.resume_workers,
                "parallel_mode": args.parallel_mode,
                "interrupt_after_task_percent": args.interrupt_after_task_percent,
                "bench_root": str(bench_root),
            },
            "fresh": {
                "workdir": str(fresh_workdir),
                "run": fresh_run,
                "validate": summarize_validate_payload(fresh_validate),
                "status": summarize_status_payload(fresh_status),
                "collections": summarize_live_index_collections(fresh_collections),
            },
            "resumed": {
                "workdir": str(resumed_workdir),
                "interrupted_run": interrupted_run,
                "run": resumed_run,
                "validate": summarize_validate_payload(resumed_validate),
                "status": summarize_status_payload(resumed_status),
                "collections": summarize_live_index_collections(resumed_collections),
                "resume_job_check": resume_job_check,
            },
            "comparison": comparison,
        }
        line = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(line + "\n", encoding="utf-8")
        else:
            print(line)
        return 0 if report["status"] == "ok" else 1


def resolve_acceptance_config(
    *,
    args: argparse.Namespace,
    workdir: Path,
    workers: str,
):
    overrides: ConfigDict = {}
    baseline_dir = workdir / "baseline"
    local_dir = workdir / "local"
    set_nested(overrides, ("runtime", "workdir"), str(workdir))
    set_nested(overrides, ("runtime", "baseline_dir"), str(baseline_dir))
    set_nested(overrides, ("runtime", "local_dir"), str(local_dir))
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
    set_nested(overrides, ("project", "workspace_root"), str(args.workspace))
    set_nested(overrides, ("indexing", "incremental"), True)
    set_nested(overrides, ("indexing", "write_target"), "local_overlay")
    set_nested(overrides, ("indexing", "workers"), parse_worker(workers))
    if args.source_docs_root is not None:
        set_nested(overrides, ("runtime", "source_docs_root"), str(args.source_docs_root))
    if args.profile is not None:
        set_nested(overrides, ("project", "default_profile"), args.profile)
    if args.parallel_mode is not None:
        set_nested(overrides, ("indexing", "parallel", "mode"), args.parallel_mode)
    if args.writer_batch_size is not None:
        set_nested(overrides, ("indexing", "writer", "batch_size"), args.writer_batch_size)
    if args.writer_max_files_per_transaction is not None:
        set_nested(
            overrides,
            ("indexing", "writer", "max_files_per_transaction"),
            args.writer_max_files_per_transaction,
        )
    if args.writer_max_records_per_transaction is not None:
        set_nested(
            overrides,
            ("indexing", "writer", "max_records_per_transaction"),
            args.writer_max_records_per_transaction,
        )
    if args.writer_commit_interval_ms is not None:
        set_nested(
            overrides,
            ("indexing", "writer", "commit_interval_ms"),
            args.writer_commit_interval_ms,
        )
    return resolve_config(config_path=args.config, cli_overrides=overrides, cwd=Path.cwd())


def run_index_once(
    *,
    resolved: Any,
    args: argparse.Namespace,
    workdir: Path,
    resume_policy: dict[str, object],
    interrupt_after_task_percent: int | None = None,
) -> dict[str, object]:
    command_payload: dict[str, object]
    interrupted = False
    try:
        if interrupt_after_task_percent is None:
            command_payload = _execute_index_run(
                resolved=resolved,
                args=args,
                resume_policy=resume_policy,
            )
        else:
            with interrupt_after_task_percent_context(interrupt_after_task_percent):
                command_payload = _execute_index_run(
                    resolved=resolved,
                    args=args,
                    resume_policy=resume_policy,
                )
    except IndexCommandInterrupted as exc:
        command_payload = dict(exc.payload)
        interrupted = True
    result = _extract_result_mapping(command_payload)
    return {
        "interrupted": interrupted,
        "workdir": str(workdir),
        "status": str(command_payload.get("status", "unknown")),
        "result_status": str(result.get("result_status", "unknown")),
        "job": _extract_job_summary(command_payload),
    }


def _execute_index_run(
    *,
    resolved: Any,
    args: argparse.Namespace,
    resume_policy: dict[str, object],
) -> dict[str, object]:
    return dict(
        _run_index_command(
            resolved,
            summary={},
            mode="incremental",
            target="local",
            source=args.source,
            resume_policy=resume_policy,
            progress_callback=lambda _event: None,
        )
    )


@contextlib.contextmanager
def interrupt_after_task_percent_context(percent: int):
    import active_knowledge_server.indexing.pipeline as pipeline_module

    original = pipeline_module.record_task_applied_checkpoint
    state: dict[str, object] = {
        "applied_count": 0,
        "threshold": None,
        "triggered": False,
    }

    def _wrapped(store, job_id, task, *, metadata=None):
        result = original(store, job_id, task, metadata=metadata)
        if bool(state["triggered"]):
            return result
        applied_count = int(state["applied_count"]) + 1
        state["applied_count"] = applied_count
        threshold = state.get("threshold")
        if threshold is None:
            job = store.get_job(job_id)
            tasks_total = 0 if job is None else int(job.metadata.get("tasks_total", 0))
            threshold = max(1, math.ceil(tasks_total * (percent / 100.0)))
            state["threshold"] = threshold
        if applied_count >= int(threshold):
            state["triggered"] = True
            raise KeyboardInterrupt(f"consistency interrupt after {percent}% of applied tasks")
        return result

    pipeline_module.record_task_applied_checkpoint = _wrapped
    try:
        yield state
    finally:
        pipeline_module.record_task_applied_checkpoint = original


def run_cli_json(
    command: str,
    *,
    args: argparse.Namespace,
    workdir: Path,
    extra_args: list[str],
) -> dict[str, object]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = cli_main([command, *build_cli_common_args(args=args, workdir=workdir), *extra_args])
    payload = json.loads(stdout.getvalue())
    if isinstance(payload, dict):
        payload["exit_code"] = exit_code
    return payload


def summarize_validate_payload(payload: dict[str, object]) -> dict[str, object]:
    storage_report = payload.get("storage_report", {})
    return {
        "exit_code": payload.get("exit_code"),
        "status": payload.get("status"),
        "warning_count": len(payload.get("warnings", []))
        if isinstance(payload.get("warnings"), list)
        else 0,
        "error_check_count": sum(
            1
            for item in payload.get("checks", [])
            if isinstance(item, dict) and item.get("level") == "error"
        )
        if isinstance(payload.get("checks"), list)
        else 0,
        "storage_status": storage_report.get("status") if isinstance(storage_report, dict) else None,
    }


def summarize_status_payload(payload: dict[str, object]) -> dict[str, object]:
    index = payload.get("index", {})
    latest_job = None
    if isinstance(index, dict):
        recent_jobs = index.get("recent_jobs", [])
        if isinstance(recent_jobs, list) and recent_jobs:
            first = recent_jobs[0]
            latest_job = first if isinstance(first, dict) else None
    latest_job_summary = None
    if latest_job is not None:
        metadata = latest_job.get("metadata", {})
        latest_job_summary = {
            "job_id": latest_job.get("job_id"),
            "status": latest_job.get("status"),
            "resume_count": metadata.get("resume_count") if isinstance(metadata, dict) else None,
            "tasks_total": metadata.get("tasks_total") if isinstance(metadata, dict) else None,
            "tasks_applied": metadata.get("tasks_applied") if isinstance(metadata, dict) else None,
            "tasks_skipped": metadata.get("tasks_skipped") if isinstance(metadata, dict) else None,
            "tasks_failed": metadata.get("tasks_failed") if isinstance(metadata, dict) else None,
            "resume_policy_mode": (
                metadata.get("resume_policy", {}).get("mode")
                if isinstance(metadata, dict) and isinstance(metadata.get("resume_policy"), dict)
                else None
            ),
        }
    return {
        "exit_code": payload.get("exit_code"),
        "status": payload.get("status"),
        "index_result_status": index.get("result_status") if isinstance(index, dict) else None,
        "latest_job": latest_job_summary,
    }


def build_resume_job_check(
    *,
    interrupted_run: dict[str, object],
    resumed_run: dict[str, object],
    resumed_status: dict[str, object],
) -> dict[str, object]:
    latest_job = summarize_status_payload(resumed_status).get("latest_job")
    latest_job = latest_job if isinstance(latest_job, dict) else {}
    resumed_job = resumed_run.get("job")
    resumed_job = resumed_job if isinstance(resumed_job, dict) else {}
    interrupted_job = interrupted_run.get("job")
    interrupted_job = interrupted_job if isinstance(interrupted_job, dict) else {}
    resumed_flag = bool(resumed_job.get("resumed"))
    latest_status_ok = latest_job.get("status") in {"ready", "partial_ready"}
    skipped_positive = _safe_int(latest_job.get("tasks_skipped")) > 0 or _safe_int(
        resumed_job.get("tasks_skipped")
    ) > 0
    same_job_id = (
        bool(interrupted_job.get("job_id"))
        and interrupted_job.get("job_id") == resumed_job.get("job_id")
    )
    return {
        "ok": resumed_flag and latest_status_ok and skipped_positive and same_job_id,
        "same_job_id": same_job_id,
        "resumed_flag": resumed_flag,
        "latest_status_ok": latest_status_ok,
        "skipped_positive": skipped_positive,
        "interrupted_job_id": interrupted_job.get("job_id"),
        "resumed_job_id": resumed_job.get("job_id"),
        "latest_job": latest_job,
    }


def report_status(
    *,
    comparison: dict[str, object],
    fresh_validate: dict[str, object],
    resumed_validate: dict[str, object],
    resume_job_check: dict[str, object],
) -> str:
    if not bool(comparison.get("all_equal")):
        return "mismatch"
    if fresh_validate.get("status") != "ok" or resumed_validate.get("status") != "ok":
        return "validate_failed"
    if not bool(resume_job_check.get("ok")):
        return "resume_job_check_failed"
    return "ok"


def build_cli_common_args(*, args: argparse.Namespace, workdir: Path) -> list[str]:
    argv = ["--workdir", str(workdir), "--config", str(args.config), "--workspace", str(args.workspace)]
    if args.source_docs_root is not None:
        argv.extend(["--source-docs-root", str(args.source_docs_root)])
    if args.profile is not None:
        argv.extend(["--profile", args.profile])
    return argv


def benchmark_root(path: Path | None):
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        return _StaticBenchmarkRoot(path)
    return tempfile.TemporaryDirectory(prefix="active-kb-resume-consistency-")


class _StaticBenchmarkRoot:
    def __init__(self, path: Path) -> None:
        self.name = str(path)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def parse_worker(raw: str) -> int | str:
    candidate = raw.strip()
    if candidate == "auto":
        return "auto"
    value = int(candidate)
    if value <= 0:
        raise ValueError("--workers must be a positive integer or 'auto'")
    return value


def _extract_result_mapping(command_payload: dict[str, object]) -> dict[str, object]:
    result = command_payload.get("result")
    if not isinstance(result, dict):
        return {}
    result_payload = dict(result)
    job = command_payload.get("job")
    if isinstance(job, dict):
        result_payload["job"] = dict(job)
    return result_payload


def _extract_job_summary(command_payload: dict[str, object]) -> dict[str, object]:
    result = _extract_result_mapping(command_payload)
    job = result.get("job")
    if not isinstance(job, dict):
        return {}
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "resumed": job.get("resumed"),
        "plan_signature": job.get("plan_signature"),
        "tasks_total": job.get("tasks_total"),
        "tasks_applied": job.get("tasks_applied"),
        "tasks_skipped": job.get("tasks_skipped"),
        "tasks_failed": job.get("tasks_failed"),
        "resume_policy": job.get("resume_policy"),
    }


def _disabled_resume_policy(*, planned_job_id: str) -> dict[str, object]:
    return {
        "schema_version": "index_resume_policy.v1",
        "mode": "disabled",
        "resume": None,
        "resume_job_id": None,
        "planned_job_id": planned_job_id,
        "resume_enabled": False,
        "restart_requested": False,
    }


def _auto_resume_policy() -> dict[str, object]:
    return {
        "schema_version": "index_resume_policy.v1",
        "mode": "auto",
        "resume": "auto",
        "resume_job_id": None,
        "planned_job_id": None,
        "resume_enabled": True,
        "restart_requested": False,
    }


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
