#!/usr/bin/env python3
"""Baseline benchmark for indexing progress and parallel tuning."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from active_knowledge_server.cli import IndexCommandInterrupted, _run_index_command, main as cli_main
from active_knowledge_server.config.loader import ConfigDict, resolve_config, set_nested
from active_knowledge_server.connectors.source_docs import SourceDocsConnector
from active_knowledge_server.connectors.workspace import WorkspaceConnector
from active_knowledge_server.eval.index_benchmark import (
    ProgressPhaseTimingCollector,
    parse_positive_int_csv,
    render_index_benchmark_markdown,
    summarize_index_benchmark_records,
)
from active_knowledge_server.storage.sqlite_store import (
    checkpoint_sqlite_database,
    configured_sqlite_paths,
    read_sqlite_runtime_settings,
    sqlite_pragma_profile_from_config,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark index runs with controlled workers and cache modes.",
    )
    parser.add_argument("--config", type=Path, help="Baseline/static config YAML path.")
    parser.add_argument("--workspace", type=Path, help="Workspace root override.")
    parser.add_argument("--source-docs-root", type=Path, help="Source docs root override.")
    parser.add_argument("--profile", help="Default profile override.")
    parser.add_argument(
        "--mode",
        choices=("incremental", "full"),
        default="incremental",
        help="Index mode to benchmark.",
    )
    parser.add_argument(
        "--target",
        choices=("local", "baseline"),
        default="local",
        help="Write target for the benchmark run.",
    )
    parser.add_argument(
        "--source",
        choices=("all", "code", "docs"),
        default="all",
        help="Source family to index.",
    )
    parser.add_argument(
        "--workers",
        default="1,2,4,8,auto",
        help="Comma-separated worker list, for example 1,2,4,8,auto.",
    )
    parser.add_argument(
        "--parallel-mode",
        choices=("thread", "process", "hybrid"),
        help="Optional collect executor mode override.",
    )
    parser.add_argument(
        "--writer-batch-sizes",
        help="Optional comma-separated writer batch sizes to sweep.",
    )
    parser.add_argument(
        "--writer-max-files-per-transaction",
        help="Optional comma-separated writer max-files-per-transaction values to sweep.",
    )
    parser.add_argument(
        "--writer-max-records-per-transaction",
        help="Optional comma-separated writer max-records-per-transaction values to sweep.",
    )
    parser.add_argument(
        "--writer-commit-intervals-ms",
        help="Optional comma-separated writer commit interval values to sweep.",
    )
    parser.add_argument(
        "--cache-mode",
        choices=("cold", "hot", "both"),
        default="cold",
        help="Cold creates a fresh workdir for every sample; hot reuses one workdir.",
    )
    parser.add_argument("--repeat", type=int, default=3, help="Sample count per scenario.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSONL output file. Prints to stdout when omitted.",
    )
    parser.add_argument(
        "--bench-root",
        type=Path,
        help="Optional root directory for generated benchmark workdirs.",
    )
    parser.add_argument(
        "--sqlite-journal-mode",
        choices=("delete", "wal"),
        help="Optional SQLite metadata journal mode override for benchmark samples.",
    )
    parser.add_argument(
        "--sqlite-synchronous",
        choices=("full", "normal"),
        help="Optional SQLite synchronous pragma override for benchmark samples.",
    )
    parser.add_argument(
        "--sqlite-wal-autocheckpoint-pages",
        type=int,
        help="Optional WAL autocheckpoint pages override for benchmark samples.",
    )
    parser.add_argument(
        "--sqlite-checkpoint-mode",
        choices=("none", "passive", "full", "restart", "truncate"),
        default="none",
        help="Optional explicit WAL checkpoint to run after each sample.",
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        help="Optional Markdown or JSON summary output derived from the raw records.",
    )
    parser.add_argument(
        "--readonly-probe",
        choices=("none", "sqlite_count"),
        default="none",
        help=(
            "Optional read-only probe to run concurrently with indexing. "
            "'sqlite_count' repeatedly opens the metadata DB read-only and measures "
            "simple count-query latency."
        ),
    )
    parser.add_argument(
        "--readonly-probe-interval-ms",
        type=int,
        default=50,
        help="Delay between read-only probe attempts when --readonly-probe is enabled.",
    )
    parser.add_argument(
        "--readonly-probe-timeout-ms",
        type=int,
        default=100,
        help="SQLite busy timeout per read-only probe attempt.",
    )
    parser.add_argument(
        "--summary-format",
        choices=("markdown", "json"),
        default="markdown",
        help="Summary output format when --summary-output is set.",
    )
    parser.add_argument(
        "--interrupt-after-task-percent",
        help=(
            "Optional comma-separated task completion percentages for controlled "
            "crash/resume samples, for example 30,70,90. When set, the benchmark "
            "runs one fresh baseline sample plus one crash/resume sample per "
            "percentage."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workers = parse_workers(args.workers)
    interrupt_after_task_percent = parse_interrupt_after_task_percent_csv(
        args.interrupt_after_task_percent
    )
    validate_interrupt_benchmark_args(
        args,
        interrupt_after_task_percent=interrupt_after_task_percent,
    )
    probe_resolved = resolve_benchmark_config(
        args=args,
        worker="auto",
        workdir=Path(".active-kb-benchmark-probe").resolve(),
        writer_batch_size=None,
        writer_max_files_per_transaction=None,
        writer_max_records_per_transaction=None,
        writer_commit_interval_ms=None,
    )
    writer_batch_sizes = parse_positive_int_csv(
        args.writer_batch_sizes,
        default=(probe_resolved.model.indexing.writer.batch_size,),
    )
    writer_max_files_per_transaction_values = parse_positive_int_csv(
        args.writer_max_files_per_transaction,
        default=(probe_resolved.model.indexing.writer.max_files_per_transaction,),
    )
    writer_max_records_per_transaction_values = parse_positive_int_csv(
        args.writer_max_records_per_transaction,
        default=(probe_resolved.model.indexing.writer.max_records_per_transaction,),
    )
    writer_commit_intervals_ms = parse_positive_int_csv(
        args.writer_commit_intervals_ms,
        default=(probe_resolved.model.indexing.writer.commit_interval_ms,),
    )
    cache_modes = ("cold", "hot") if args.cache_mode == "both" else (args.cache_mode,)
    dataset = collect_dataset_summary(args)
    git_commit = current_git_commit()
    records: list[dict[str, object]] = []

    with benchmark_root(args.bench_root) as bench_root_value:
        bench_root = Path(bench_root_value)
        for cache_mode in cache_modes:
            for writer_batch_size in writer_batch_sizes:
                for writer_max_files_per_transaction in writer_max_files_per_transaction_values:
                    for (
                        writer_max_records_per_transaction
                    ) in writer_max_records_per_transaction_values:
                        for writer_commit_interval_ms in writer_commit_intervals_ms:
                            for worker in workers:
                                scenario_workdir = bench_root / (
                                    f"{args.mode}-{args.target}-{cache_mode}"
                                    f"-w{worker}-b{writer_batch_size}"
                                    f"-mf{writer_max_files_per_transaction}"
                                    f"-mr{writer_max_records_per_transaction}"
                                    f"-c{writer_commit_interval_ms}"
                                )
                                hot_workdir = scenario_workdir / "hot"
                                for sample_index in range(max(args.repeat, 1)):
                                    if interrupt_after_task_percent:
                                        sample_root = scenario_workdir / f"sample-{sample_index}"
                                        fresh_record = run_sample(
                                            args=args,
                                            worker=worker,
                                            writer_batch_size=writer_batch_size,
                                            writer_max_files_per_transaction=(
                                                writer_max_files_per_transaction
                                            ),
                                            writer_max_records_per_transaction=(
                                                writer_max_records_per_transaction
                                            ),
                                            writer_commit_interval_ms=writer_commit_interval_ms,
                                            cache_mode=cache_mode,
                                            sample_index=sample_index,
                                            workdir=sample_root / "fresh",
                                            dataset=dataset,
                                            git_commit=git_commit,
                                        )
                                        records.append(fresh_record)
                                        emit_record(fresh_record, output_path=args.output)
                                        for task_percent in interrupt_after_task_percent:
                                            resumed_record = run_crash_resume_sample(
                                                args=args,
                                                worker=worker,
                                                writer_batch_size=writer_batch_size,
                                                writer_max_files_per_transaction=(
                                                    writer_max_files_per_transaction
                                                ),
                                                writer_max_records_per_transaction=(
                                                    writer_max_records_per_transaction
                                                ),
                                                writer_commit_interval_ms=(
                                                    writer_commit_interval_ms
                                                ),
                                                cache_mode=cache_mode,
                                                sample_index=sample_index,
                                                workdir=sample_root / f"resume-{task_percent}",
                                                dataset=dataset,
                                                git_commit=git_commit,
                                                interrupt_after_task_percent=task_percent,
                                                fresh_reference_wall_seconds=float(
                                                    fresh_record["wall_seconds"]
                                                ),
                                            )
                                            records.append(resumed_record)
                                            emit_record(resumed_record, output_path=args.output)
                                    else:
                                        if cache_mode == "cold":
                                            workdir = scenario_workdir / f"sample-{sample_index}"
                                        else:
                                            workdir = hot_workdir
                                        record = run_sample(
                                            args=args,
                                            worker=worker,
                                            writer_batch_size=writer_batch_size,
                                            writer_max_files_per_transaction=(
                                                writer_max_files_per_transaction
                                            ),
                                            writer_max_records_per_transaction=(
                                                writer_max_records_per_transaction
                                            ),
                                            writer_commit_interval_ms=writer_commit_interval_ms,
                                            cache_mode=cache_mode,
                                            sample_index=sample_index,
                                            workdir=workdir,
                                            dataset=dataset,
                                            git_commit=git_commit,
                                        )
                                        records.append(record)
                                        emit_record(record, output_path=args.output)

    if args.summary_output is not None:
        report = summarize_index_benchmark_records(records)
        summary_text = (
            json.dumps(report.to_dict(), indent=2, sort_keys=True)
            if args.summary_format == "json"
            else render_index_benchmark_markdown(report)
        )
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(summary_text, encoding="utf-8")

    if args.output is None:
        if args.summary_output is not None:
            print(
                json.dumps(
                    {
                        "schema_version": "index_benchmark_summary.v2",
                        "record_count": len(records),
                        "summary_output": str(args.summary_output),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
        return 0

    print(
        json.dumps(
            {
                "schema_version": "index_benchmark_summary.v1",
                "record_count": len(records),
                "output": str(args.output),
                "summary_output": None if args.summary_output is None else str(args.summary_output),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


def parse_workers(raw: str) -> tuple[int | str, ...]:
    values: list[int | str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        if candidate == "auto":
            values.append(candidate)
            continue
        count = int(candidate)
        if count <= 0:
            raise ValueError("workers must be positive integers or 'auto'")
        values.append(count)
    if not values:
        raise ValueError("at least one worker value is required")
    return tuple(values)


def benchmark_root(path: Path | None):
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        return _StaticBenchmarkRoot(path)
    return TemporaryDirectory(prefix="active-kb-index-benchmark-")


class _StaticBenchmarkRoot:
    def __init__(self, path: Path) -> None:
        self.name = str(path)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def run_sample(
    *,
    args: argparse.Namespace,
    worker: int | str,
    writer_batch_size: int,
    writer_max_files_per_transaction: int,
    writer_max_records_per_transaction: int,
    writer_commit_interval_ms: int,
    cache_mode: str,
    sample_index: int,
    workdir: Path,
    dataset: dict[str, object],
    git_commit: str | None,
) -> dict[str, object]:
    if cache_mode == "cold" and workdir.exists():
        remove_tree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    resolved = resolve_benchmark_config(
        args=args,
        worker=worker,
        workdir=workdir,
        writer_batch_size=writer_batch_size,
        writer_max_files_per_transaction=writer_max_files_per_transaction,
        writer_max_records_per_transaction=writer_max_records_per_transaction,
        writer_commit_interval_ms=writer_commit_interval_ms,
    )
    notes: list[str] = []
    if cache_mode == "hot" and args.mode == "incremental":
        notes.append(
            "Hot incremental samples may converge to near-noop runs "
            "when sources do not change."
        )

    measurement = measure_index_run(
        resolved=resolved,
        args=args,
        progress_callback=None,
        resume_policy=_disabled_resume_policy(),
    )
    counts = collect_storage_counts(resolved, args.target)
    return build_record(
        args=args,
        resolved=resolved,
        worker=worker,
        cache_mode=cache_mode,
        sample_index=sample_index,
        workdir=workdir,
        dataset=dataset,
        git_commit=git_commit,
        notes=notes,
        measurement=measurement,
        object_counts=counts,
        interrupt_summary=empty_interrupt_summary(),
        validate_summary=empty_validate_summary(),
    )


def run_crash_resume_sample(
    *,
    args: argparse.Namespace,
    worker: int | str,
    writer_batch_size: int,
    writer_max_files_per_transaction: int,
    writer_max_records_per_transaction: int,
    writer_commit_interval_ms: int,
    cache_mode: str,
    sample_index: int,
    workdir: Path,
    dataset: dict[str, object],
    git_commit: str | None,
    interrupt_after_task_percent: int,
    fresh_reference_wall_seconds: float,
) -> dict[str, object]:
    if workdir.exists():
        remove_tree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    resolved = resolve_benchmark_config(
        args=args,
        worker=worker,
        workdir=workdir,
        writer_batch_size=writer_batch_size,
        writer_max_files_per_transaction=writer_max_files_per_transaction,
        writer_max_records_per_transaction=writer_max_records_per_transaction,
        writer_commit_interval_ms=writer_commit_interval_ms,
    )
    job_id = (
        "bench-index-resume-"
        f"{sample_index}-w{worker}-b{writer_batch_size}"
        f"-mf{writer_max_files_per_transaction}"
        f"-mr{writer_max_records_per_transaction}"
        f"-c{writer_commit_interval_ms}"
        f"-p{interrupt_after_task_percent}"
    )
    interrupted = measure_index_run(
        resolved=resolved,
        args=args,
        progress_callback=None,
        resume_policy=_no_resume_policy(job_id),
        interrupt_after_task_percent=interrupt_after_task_percent,
    )
    if not interrupted.interrupted:
        raise RuntimeError(
            "crash/resume benchmark expected an interrupted first run, "
            f"but scenario completed normally at {interrupt_after_task_percent}%"
        )
    resumed = measure_index_run(
        resolved=resolved,
        args=args,
        progress_callback=None,
        resume_policy=_auto_resume_policy(),
    )
    validate_payload = run_validate_command(args=args, workdir=workdir)
    counts = collect_storage_counts(resolved, args.target)
    notes = [
        (
            f"Interrupted after approximately {interrupt_after_task_percent}% of applied tasks "
            "and resumed via --resume auto."
        )
    ]
    interrupt_summary = build_interrupt_summary(
        interrupt_after_task_percent=interrupt_after_task_percent,
        fresh_reference_wall_seconds=fresh_reference_wall_seconds,
        interrupted=interrupted,
        resumed=resumed,
    )
    return build_record(
        args=args,
        resolved=resolved,
        worker=worker,
        cache_mode=cache_mode,
        sample_index=sample_index,
        workdir=workdir,
        dataset=dataset,
        git_commit=git_commit,
        notes=notes,
        measurement=resumed,
        object_counts=counts,
        interrupt_summary=interrupt_summary,
        validate_summary=validate_payload,
    )


def resolve_benchmark_config(
    *,
    args: argparse.Namespace,
    worker: int | str,
    workdir: Path,
    writer_batch_size: int | None,
    writer_max_files_per_transaction: int | None,
    writer_max_records_per_transaction: int | None,
    writer_commit_interval_ms: int | None,
):
    overrides: ConfigDict = {}
    baseline_dir = workdir / "baseline"
    local_dir = workdir / "local"
    set_nested(overrides, ("runtime", "workdir"), str(workdir))
    set_nested(overrides, ("runtime", "baseline_dir"), str(baseline_dir))
    set_nested(overrides, ("runtime", "local_dir"), str(local_dir))
    set_nested(
        overrides,
        ("storage", "baseline", "manifest"),
        str(baseline_dir / "manifest.json"),
    )
    set_nested(
        overrides,
        ("storage", "metadata", "path"),
        str(baseline_dir / "db" / "metadata.db"),
    )
    set_nested(
        overrides,
        ("storage", "overlay", "path"),
        str(local_dir / "db" / "overlay.db"),
    )
    set_nested(
        overrides,
        ("storage", "jobs", "path"),
        str(local_dir / "db" / "jobs.db"),
    )
    set_nested(
        overrides,
        ("storage", "vector", "path"),
        str(baseline_dir / "vectors" / "lancedb"),
    )
    set_nested(
        overrides,
        ("storage", "vector_delta", "path"),
        str(local_dir / "vectors" / "lancedb-delta"),
    )
    set_nested(
        overrides,
        ("storage", "artifacts_root"),
        str(baseline_dir / "artifacts"),
    )
    set_nested(
        overrides,
        ("storage", "local_artifacts_root"),
        str(local_dir / "artifacts"),
    )
    set_nested(
        overrides,
        ("storage", "cache_root"),
        str(local_dir / "cache"),
    )
    if args.workspace is not None:
        set_nested(overrides, ("project", "workspace_root"), str(args.workspace))
    if args.source_docs_root is not None:
        set_nested(overrides, ("runtime", "source_docs_root"), str(args.source_docs_root))
    if args.profile is not None:
        set_nested(overrides, ("project", "default_profile"), args.profile)
    set_nested(overrides, ("indexing", "workers"), worker)
    if args.parallel_mode is not None:
        set_nested(overrides, ("indexing", "parallel", "mode"), args.parallel_mode)
    set_nested(overrides, ("indexing", "incremental"), args.mode == "incremental")
    set_nested(
        overrides,
        ("indexing", "write_target"),
        "baseline" if args.target == "baseline" else "local_overlay",
    )
    if writer_batch_size is not None:
        set_nested(overrides, ("indexing", "writer", "batch_size"), writer_batch_size)
    if writer_max_files_per_transaction is not None:
        set_nested(
            overrides,
            ("indexing", "writer", "max_files_per_transaction"),
            writer_max_files_per_transaction,
        )
    if writer_max_records_per_transaction is not None:
        set_nested(
            overrides,
            ("indexing", "writer", "max_records_per_transaction"),
            writer_max_records_per_transaction,
        )
    if writer_commit_interval_ms is not None:
        set_nested(
            overrides,
            ("indexing", "writer", "commit_interval_ms"),
            writer_commit_interval_ms,
        )
    if args.sqlite_journal_mode is not None:
        set_nested(overrides, ("storage", "sqlite", "journal_mode"), args.sqlite_journal_mode)
        if args.sqlite_journal_mode == "wal":
            set_nested(overrides, ("storage", "sqlite", "assume_local_filesystem"), True)
    if args.sqlite_synchronous is not None:
        set_nested(overrides, ("storage", "sqlite", "synchronous"), args.sqlite_synchronous)
    if args.sqlite_wal_autocheckpoint_pages is not None:
        set_nested(
            overrides,
            ("storage", "sqlite", "wal_autocheckpoint_pages"),
            args.sqlite_wal_autocheckpoint_pages,
        )
    return resolve_config(config_path=args.config, cli_overrides=overrides, cwd=Path.cwd())


def execute_index_run(
    *,
    resolved: Any,
    args: argparse.Namespace,
    progress_callback: Any | None = None,
    resume_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    command_payload = _run_index_command(
        resolved,
        summary={},
        mode=args.mode,
        target=args.target,
        source=args.source,
        resume_policy=_disabled_resume_policy() if resume_policy is None else resume_policy,
        progress_callback=progress_callback,
    )
    return dict(command_payload)


def collect_dataset_summary(args: argparse.Namespace) -> dict[str, object]:
    resolved = resolve_benchmark_config(
        args=args,
        worker="auto",
        workdir=Path(".active-kb-benchmark-probe").resolve(),
        writer_batch_size=None,
        writer_max_files_per_transaction=None,
        writer_max_records_per_transaction=None,
        writer_commit_interval_ms=None,
    )
    workspace_inventory = WorkspaceConnector.from_config(resolved.model, cwd=Path.cwd()).scan()
    source_docs_manifest = SourceDocsConnector.from_config(resolved.model, cwd=Path.cwd()).scan()
    return {
        "workspace_file_count": len(workspace_inventory.files),
        "workspace_repository_count": len(workspace_inventory.repositories),
        "source_doc_file_count": len(source_docs_manifest.files),
        "source_doc_category_count": len(source_docs_manifest.categories),
        "dataset_tier": dataset_tier(len(workspace_inventory.files)),
    }


def collect_storage_counts(resolved: Any, target: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    metadata_path = metadata_path_for_target(resolved, target)
    paths = configured_sqlite_paths(resolved.model, cwd=Path.cwd())
    jobs_path = paths["jobs"]
    counts.update(query_table_counts(metadata_path))
    if jobs_path.exists():
        counts["job"] = query_table_count(jobs_path, "job")
    return counts


def metadata_path_for_target(resolved: Any, target: str) -> Path:
    paths = configured_sqlite_paths(resolved.model, cwd=Path.cwd())
    return paths["baseline_metadata"] if target == "baseline" else paths["overlay_metadata"]


def maybe_checkpoint_sqlite_database(
    path: Path,
    *,
    checkpoint_mode: str,
) -> dict[str, object] | None:
    if checkpoint_mode == "none":
        return None
    result = checkpoint_sqlite_database(path, mode=checkpoint_mode)
    if result is None:
        return None
    return {
        "mode": result.mode,
        "busy": result.busy,
        "log_frames": result.log_frames,
        "checkpointed_frames": result.checkpointed_frames,
    }


def _probe_percentile(samples: list[float], quantile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(float(value) for value in samples)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


class SQLiteReadonlyProbe:
    """Sample one lightweight read-only query against the metadata DB."""

    def __init__(
        self,
        *,
        metadata_path: Path,
        interval_ms: int,
        timeout_ms: int,
    ) -> None:
        self._metadata_path = metadata_path
        self._interval_seconds = max(int(interval_ms), 1) / 1000.0
        self._timeout_seconds = max(int(timeout_ms), 1) / 1000.0
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sqlite-readonly-probe", daemon=True)
        self._latencies_ms: list[float] = []
        self._success_count = 0
        self._busy_count = 0
        self._error_count = 0
        self._startup_wait_count = 0
        self._last_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, object]:
        self._stop_event.set()
        self._thread.join(timeout=max(self._interval_seconds * 4.0, 1.0))
        latencies_ms = list(self._latencies_ms)
        return {
            "enabled": True,
            "mode": "sqlite_count",
            "success_count": self._success_count,
            "busy_count": self._busy_count,
            "error_count": self._error_count,
            "startup_wait_count": self._startup_wait_count,
            "latency_ms_p50": round(_probe_percentile(latencies_ms, 0.50), 3),
            "latency_ms_p95": round(_probe_percentile(latencies_ms, 0.95), 3),
            "latency_ms_max": (0.0 if not latencies_ms else round(max(latencies_ms), 3)),
            "last_error": self._last_error,
        }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            started_at = time.perf_counter()
            if not self._metadata_path.exists():
                self._startup_wait_count += 1
                self._sleep_remaining(started_at)
                continue
            try:
                self._probe_once()
            except sqlite3.OperationalError as exc:
                message = str(exc)
                self._last_error = message
                lowered = message.lower()
                if "locked" in lowered or "busy" in lowered:
                    self._busy_count += 1
                else:
                    self._error_count += 1
            except Exception as exc:  # noqa: BLE001 - benchmark probes must stay alive.
                self._last_error = str(exc)
                self._error_count += 1
            self._sleep_remaining(started_at)

    def _probe_once(self) -> None:
        start = time.perf_counter()
        connection = sqlite3.connect(
            f"file:{self._metadata_path}?mode=ro",
            uri=True,
            timeout=self._timeout_seconds,
        )
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='chunk'"
            ).fetchone()
            chunk_table_present = bool(row and int(row[0]) > 0)
            if chunk_table_present:
                connection.execute("SELECT COUNT(*) FROM chunk").fetchone()
            else:
                connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        finally:
            connection.close()
        self._latencies_ms.append((time.perf_counter() - start) * 1000.0)
        self._success_count += 1

    def _sleep_remaining(self, started_at: float) -> None:
        remaining = self._interval_seconds - (time.perf_counter() - started_at)
        if remaining > 0:
            self._stop_event.wait(remaining)


def collect_warning_code_counts(result_payload: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    warnings = result_payload.get("warnings", [])
    if not isinstance(warnings, list):
        return counts
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        code = warning.get("code")
        if not isinstance(code, str) or not code:
            continue
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def extract_result_metadata_mapping(
    result_payload: dict[str, object],
    key: str,
) -> dict[str, object]:
    metadata = result_payload.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    value = metadata.get(key)
    return dict(value) if isinstance(value, dict) else {}


def extract_result_job_mapping(result_payload: dict[str, object]) -> dict[str, object]:
    value = result_payload.get("job")
    return dict(value) if isinstance(value, dict) else {}


def extract_result_task_stats(result_payload: dict[str, object]) -> dict[str, object]:
    task_stats = extract_result_metadata_mapping(result_payload, "tasks")
    if task_stats:
        return task_stats
    job = extract_result_job_mapping(result_payload)
    keys = ("tasks_total", "tasks_applied", "tasks_skipped", "tasks_failed")
    derived = {
        key.replace("tasks_", ""): job.get(key)
        for key in keys
        if isinstance(job.get(key), int)
    }
    if "replayed" not in derived:
        derived["replayed"] = 0
    return derived


def collect_storage_file_sizes(metadata_path: Path) -> dict[str, int]:
    wal_path = Path(f"{metadata_path}-wal")
    shm_path = Path(f"{metadata_path}-shm")
    return {
        "metadata_db_bytes": metadata_path.stat().st_size if metadata_path.exists() else 0,
        "metadata_wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "metadata_shm_bytes": shm_path.stat().st_size if shm_path.exists() else 0,
    }


def query_table_counts(path: Path) -> dict[str, int]:
    tables = (
        "source",
        "snapshot",
        "profile",
        "file",
        "chunk",
        "entity",
        "relation",
        "evidence",
        "vector_ref",
        "tombstone",
        "replacement",
    )
    return {table: query_table_count(path, table) for table in tables}


def query_table_count(path: Path, table: str) -> int:
    if not path.exists():
        return 0
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None or int(row[0]) == 0:
            return 0
        count_row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return 0 if count_row is None else int(count_row[0])


def emit_record(record: dict[str, object], *, output_path: Path | None) -> None:
    line = json.dumps(record, ensure_ascii=True, sort_keys=True)
    if output_path is None:
        print(line)
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def dataset_tier(workspace_file_count: int) -> str:
    if workspace_file_count < 5_000:
        return "small"
    if workspace_file_count <= 30_000:
        return "medium"
    return "large"


def current_rss_bytes() -> int:
    try:
        import resource
    except ImportError:
        return 0
    usage = resource.getrusage(resource.RUSAGE_SELF)
    multiplier = 1 if sys.platform == "darwin" else 1024
    return int(usage.ru_maxrss) * multiplier


def current_git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _disabled_resume_policy() -> dict[str, object]:
    return {
        "schema_version": "index_resume_policy.v1",
        "mode": "disabled",
        "resume": None,
        "resume_job_id": None,
        "planned_job_id": None,
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


def _no_resume_policy(job_id: str) -> dict[str, object]:
    return {
        **_disabled_resume_policy(),
        "planned_job_id": job_id,
    }


def parse_interrupt_after_task_percent_csv(raw: str | None) -> tuple[int, ...]:
    if raw is None or not raw.strip():
        return ()
    values: list[int] = []
    seen: set[int] = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        value = int(candidate)
        if value <= 0 or value >= 100:
            raise ValueError("interrupt-after-task-percent values must be between 1 and 99")
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    if not values:
        raise ValueError("at least one interrupt-after-task-percent value is required")
    return tuple(values)


def validate_interrupt_benchmark_args(
    args: argparse.Namespace,
    *,
    interrupt_after_task_percent: tuple[int, ...],
) -> None:
    if not interrupt_after_task_percent:
        return
    if args.mode != "incremental":
        raise ValueError("crash/resume benchmark only supports --mode incremental")
    if args.target != "local":
        raise ValueError("crash/resume benchmark only supports --target local")
    if args.cache_mode != "cold":
        raise ValueError("crash/resume benchmark requires --cache-mode cold")


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
            raise KeyboardInterrupt(
                f"benchmark interrupt after {percent}% of applied tasks"
            )
        return result

    pipeline_module.record_task_applied_checkpoint = _wrapped
    try:
        yield state
    finally:
        pipeline_module.record_task_applied_checkpoint = original


def measure_index_run(
    *,
    resolved: Any,
    args: argparse.Namespace,
    progress_callback: Any | None,
    resume_policy: dict[str, object],
    interrupt_after_task_percent: int | None = None,
) -> IndexRunMeasurement:
    progress = ProgressPhaseTimingCollector()
    metadata_path = metadata_path_for_target(resolved, args.target)
    readonly_probe: SQLiteReadonlyProbe | None = None
    readonly_probe_summary = empty_readonly_probe_summary()
    if args.readonly_probe == "sqlite_count":
        readonly_probe = SQLiteReadonlyProbe(
            metadata_path=metadata_path,
            interval_ms=args.readonly_probe_interval_ms,
            timeout_ms=args.readonly_probe_timeout_ms,
        )
        readonly_probe.start()
    if progress_callback is None:
        callback = progress.observe
    else:
        def callback(event) -> None:
            progress.observe(event)
            progress_callback(event)
    rss_before = current_rss_bytes()
    cpu_before = time.process_time()
    wall_before = time.perf_counter()
    interrupted = False
    try:
        try:
            if interrupt_after_task_percent is None:
                command_payload = execute_index_run(
                    resolved=resolved,
                    args=args,
                    progress_callback=callback,
                    resume_policy=resume_policy,
                )
            else:
                with interrupt_after_task_percent_context(interrupt_after_task_percent):
                    command_payload = execute_index_run(
                        resolved=resolved,
                        args=args,
                        progress_callback=callback,
                        resume_policy=resume_policy,
                    )
        except IndexCommandInterrupted as exc:
            command_payload = dict(exc.payload)
            interrupted = True
        wall_seconds = time.perf_counter() - wall_before
        cpu_seconds = time.process_time() - cpu_before
        progress_snapshot = progress.finish()
        rss_after = current_rss_bytes()
        sqlite_checkpoint = maybe_checkpoint_sqlite_database(
            metadata_path,
            checkpoint_mode=args.sqlite_checkpoint_mode,
        )
    finally:
        if readonly_probe is not None:
            readonly_probe_summary = readonly_probe.stop()
    return IndexRunMeasurement(
        command_payload=command_payload,
        interrupted=interrupted,
        wall_seconds=wall_seconds,
        cpu_seconds=cpu_seconds,
        rss_before_bytes=rss_before,
        rss_after_bytes=rss_after,
        progress_snapshot=progress_snapshot,
        sqlite_checkpoint=sqlite_checkpoint,
        readonly_probe=readonly_probe_summary,
    )


def build_record(
    *,
    args: argparse.Namespace,
    resolved: Any,
    worker: int | str,
    cache_mode: str,
    sample_index: int,
    workdir: Path,
    dataset: dict[str, object],
    git_commit: str | None,
    notes: list[str],
    measurement: IndexRunMeasurement,
    object_counts: dict[str, int],
    interrupt_summary: dict[str, object],
    validate_summary: dict[str, object],
) -> dict[str, object]:
    result_payload = extract_result_mapping(measurement.command_payload)
    warning_code_counts = collect_warning_code_counts(result_payload)
    writer_metadata = extract_result_metadata_mapping(result_payload, "writer")
    task_stats = extract_result_task_stats(result_payload)
    job_payload = extract_result_job_mapping(result_payload)
    metadata_path = metadata_path_for_target(resolved, args.target)
    return {
        "schema_version": "index_benchmark_record.v4",
        "executed_at": utc_now(),
        "config_path": None if args.config is None else str(args.config),
        "mode": args.mode,
        "target": args.target,
        "source": args.source,
        "cache_mode": cache_mode,
        "sample_index": sample_index,
        "workers_requested": worker,
        "writer": {
            "batch_size": int(
                writer_metadata.get("batch_size", resolved.model.indexing.writer.batch_size)
            ),
            "max_files_per_transaction": int(
                writer_metadata.get(
                    "max_files_per_transaction",
                    resolved.model.indexing.writer.max_files_per_transaction,
                )
            ),
            "max_records_per_transaction": int(
                writer_metadata.get(
                    "max_records_per_transaction",
                    resolved.model.indexing.writer.max_records_per_transaction,
                )
            ),
            "commit_interval_ms": int(
                writer_metadata.get(
                    "commit_interval_ms",
                    resolved.model.indexing.writer.commit_interval_ms,
                )
            ),
        },
        "parallel": {
            "mode": resolved.model.indexing.parallel.mode,
        },
        "sqlite": {
            "configured": {
                "journal_mode": resolved.model.storage.sqlite.journal_mode,
                "synchronous": resolved.model.storage.sqlite.synchronous,
                "wal_autocheckpoint_pages": (
                    resolved.model.storage.sqlite.wal_autocheckpoint_pages
                ),
                "assume_local_filesystem": resolved.model.storage.sqlite.assume_local_filesystem,
            },
            "actual": read_sqlite_runtime_settings(
                metadata_path,
                pragma_profile=sqlite_pragma_profile_from_config(resolved.model),
            ),
            "checkpoint": measurement.sqlite_checkpoint,
        },
        "workdir": str(workdir),
        "result_status": str(
            result_payload.get("result_status", measurement.command_payload.get("status", "unknown"))
        ),
        "wall_seconds": round(measurement.wall_seconds, 6),
        "cpu_seconds": round(measurement.cpu_seconds, 6),
        "rss_before_bytes": measurement.rss_before_bytes,
        "rss_after_bytes": measurement.rss_after_bytes,
        "rss_delta_bytes": max(measurement.rss_after_bytes - measurement.rss_before_bytes, 0),
        "warning_count": len(result_payload.get("warnings", [])),
        "warning_codes": sorted(warning_code_counts),
        "warning_code_counts": warning_code_counts,
        "job": job_payload,
        "plan_summary": result_payload.get("plan"),
        "timings": extract_result_metadata_mapping(result_payload, "timings"),
        "phase_timings": measurement.progress_snapshot.phase_timings,
        "phase_event_counts": measurement.progress_snapshot.phase_event_counts,
        "progress_event_count": measurement.progress_snapshot.event_count,
        "observed_phases": list(measurement.progress_snapshot.observed_phases),
        "diagnostics": extract_result_metadata_mapping(result_payload, "diagnostics"),
        "apply_batches": extract_result_metadata_mapping(result_payload, "apply_batches"),
        "task_stats": task_stats,
        "interrupt": interrupt_summary,
        "validate": validate_summary,
        "readonly_probe": measurement.readonly_probe,
        "object_counts": object_counts,
        "storage_files": collect_storage_file_sizes(metadata_path),
        "dataset": dataset,
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count() or 1,
        },
        "git_commit": git_commit,
        "notes": notes,
    }


def extract_result_mapping(command_payload: dict[str, object]) -> dict[str, object]:
    result = command_payload.get("result")
    if not isinstance(result, dict):
        return {}
    result_payload = dict(result)
    job = command_payload.get("job")
    if isinstance(job, dict):
        result_payload["job"] = dict(job)
    return result_payload


def empty_interrupt_summary() -> dict[str, object]:
    return {
        "after_task_percent": None,
        "interrupted_wall_seconds": None,
        "resume_wall_seconds": None,
        "total_wall_seconds": None,
        "expected_remaining_fraction": None,
        "expected_remaining_wall_seconds": None,
        "resume_vs_expected_remaining_ratio": None,
        "interrupted_status": None,
    }


def build_interrupt_summary(
    *,
    interrupt_after_task_percent: int,
    fresh_reference_wall_seconds: float,
    interrupted: IndexRunMeasurement,
    resumed: IndexRunMeasurement,
) -> dict[str, object]:
    remaining_fraction = max(0.0, (100.0 - float(interrupt_after_task_percent)) / 100.0)
    expected_remaining_wall_seconds = fresh_reference_wall_seconds * remaining_fraction
    ratio = None
    if expected_remaining_wall_seconds > 0:
        ratio = resumed.wall_seconds / expected_remaining_wall_seconds
    return {
        "after_task_percent": interrupt_after_task_percent,
        "interrupted_wall_seconds": round(interrupted.wall_seconds, 6),
        "resume_wall_seconds": round(resumed.wall_seconds, 6),
        "total_wall_seconds": round(interrupted.wall_seconds + resumed.wall_seconds, 6),
        "expected_remaining_fraction": round(remaining_fraction, 4),
        "expected_remaining_wall_seconds": round(expected_remaining_wall_seconds, 6),
        "resume_vs_expected_remaining_ratio": (
            None if ratio is None else round(ratio, 4)
        ),
        "interrupted_status": str(interrupted.command_payload.get("status", "unknown")),
    }


def empty_validate_summary() -> dict[str, object]:
    return {
        "ran": False,
        "exit_code": None,
        "status": "not_run",
        "warning_count": 0,
        "blocked_warning_count": 0,
        "error_check_count": 0,
        "storage_status": None,
        "index_result_status": None,
    }


def empty_readonly_probe_summary() -> dict[str, object]:
    return {
        "enabled": False,
        "mode": "none",
        "success_count": 0,
        "busy_count": 0,
        "error_count": 0,
        "startup_wait_count": 0,
        "latency_ms_p50": 0.0,
        "latency_ms_p95": 0.0,
        "latency_ms_max": 0.0,
        "last_error": None,
    }


def run_validate_command(
    *,
    args: argparse.Namespace,
    workdir: Path,
) -> dict[str, object]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = cli_main(
            [
                "validate",
                *build_cli_common_args(args=args, workdir=workdir),
                "--strict",
                "--format",
                "json",
            ]
        )
    payload = json.loads(stdout.getvalue())
    warnings = payload.get("warnings", [])
    checks = payload.get("checks", [])
    storage_report = payload.get("storage_report", {})
    index_status = payload.get("index", {})
    return {
        "ran": True,
        "exit_code": exit_code,
        "status": str(payload.get("status", "unknown")),
        "warning_count": len(warnings) if isinstance(warnings, list) else 0,
        "blocked_warning_count": (
            sum(
                1
                for item in warnings
                if isinstance(item, dict) and str(item.get("level")) == "blocked"
            )
            if isinstance(warnings, list)
            else 0
        ),
        "error_check_count": (
            sum(
                1
                for item in checks
                if isinstance(item, dict) and str(item.get("level")) == "error"
            )
            if isinstance(checks, list)
            else 0
        ),
        "storage_status": (
            str(storage_report.get("status", "unknown"))
            if isinstance(storage_report, dict)
            else "unknown"
        ),
        "index_result_status": (
            str(index_status.get("result_status", "unknown"))
            if isinstance(index_status, dict)
            else "unknown"
        ),
    }


def build_cli_common_args(*, args: argparse.Namespace, workdir: Path) -> list[str]:
    argv = ["--workdir", str(workdir)]
    if args.config is not None:
        argv.extend(["--config", str(args.config)])
    if args.workspace is not None:
        argv.extend(["--workspace", str(args.workspace)])
    if args.source_docs_root is not None:
        argv.extend(["--source-docs-root", str(args.source_docs_root)])
    if args.profile is not None:
        argv.extend(["--profile", args.profile])
    return argv


class IndexRunMeasurement:
    def __init__(
        self,
        *,
        command_payload: dict[str, object],
        interrupted: bool,
        wall_seconds: float,
        cpu_seconds: float,
        rss_before_bytes: int,
        rss_after_bytes: int,
        progress_snapshot,
        sqlite_checkpoint: dict[str, object] | None,
        readonly_probe: dict[str, object],
    ) -> None:
        self.command_payload = command_payload
        self.interrupted = interrupted
        self.wall_seconds = wall_seconds
        self.cpu_seconds = cpu_seconds
        self.rss_before_bytes = rss_before_bytes
        self.rss_after_bytes = rss_after_bytes
        self.progress_snapshot = progress_snapshot
        self.sqlite_checkpoint = sqlite_checkpoint
        self.readonly_probe = dict(readonly_probe)


if __name__ == "__main__":
    raise SystemExit(main())
