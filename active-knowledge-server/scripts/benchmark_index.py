#!/usr/bin/env python3
"""Baseline benchmark for indexing progress and parallel tuning."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from active_knowledge_server.cli import run_full_index
from active_knowledge_server.config.loader import ConfigDict, resolve_config, set_nested
from active_knowledge_server.connectors.source_docs import SourceDocsConnector
from active_knowledge_server.connectors.workspace import WorkspaceConnector
from active_knowledge_server.eval.index_benchmark import (
    parse_positive_int_csv,
    render_index_benchmark_markdown,
    summarize_index_benchmark_records,
)
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID, IncrementalIndexPipeline
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
        "--summary-format",
        choices=("markdown", "json"),
        default="markdown",
        help="Summary output format when --summary-output is set.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workers = parse_workers(args.workers)
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

    rss_before = current_rss_bytes()
    cpu_before = time.process_time()
    wall_before = time.perf_counter()
    result_payload = execute_index_run(resolved=resolved, args=args)
    wall_seconds = time.perf_counter() - wall_before
    cpu_seconds = time.process_time() - cpu_before
    rss_after = current_rss_bytes()
    metadata_path = metadata_path_for_target(resolved, args.target)
    checkpoint = maybe_checkpoint_sqlite_database(
        metadata_path,
        checkpoint_mode=args.sqlite_checkpoint_mode,
    )
    counts = collect_storage_counts(resolved, args.target)
    warning_code_counts = collect_warning_code_counts(result_payload)
    writer_metadata = extract_result_metadata_mapping(result_payload, "writer")

    return {
        "schema_version": "index_benchmark_record.v1",
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
            "checkpoint": checkpoint,
        },
        "workdir": str(workdir),
        "result_status": str(result_payload.get("result_status", "unknown")),
        "wall_seconds": round(wall_seconds, 6),
        "cpu_seconds": round(cpu_seconds, 6),
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "rss_delta_bytes": max(rss_after - rss_before, 0),
        "warning_count": len(result_payload.get("warnings", [])),
        "warning_codes": sorted(warning_code_counts),
        "warning_code_counts": warning_code_counts,
        "plan_summary": result_payload.get("plan"),
        "timings": extract_result_metadata_mapping(result_payload, "timings"),
        "diagnostics": extract_result_metadata_mapping(result_payload, "diagnostics"),
        "apply_batches": extract_result_metadata_mapping(result_payload, "apply_batches"),
        "object_counts": counts,
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
    set_nested(overrides, ("runtime", "workdir"), str(workdir))
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


def execute_index_run(*, resolved: Any, args: argparse.Namespace) -> dict[str, object]:
    if args.mode == "incremental" and args.target == "local":
        pipeline = IncrementalIndexPipeline(resolved.model, cwd=Path.cwd())
        return pipeline.run(snapshot_id=CURRENT_SNAPSHOT_ID, source=args.source).to_dict()
    operation_mode = "baseline_publish" if args.target == "baseline" else "normal"
    return run_full_index(
        resolved,
        target=args.target,
        source=args.source,
        operation_mode=operation_mode,
    )


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


if __name__ == "__main__":
    raise SystemExit(main())
