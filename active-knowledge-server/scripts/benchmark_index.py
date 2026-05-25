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
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID, IncrementalIndexPipeline
from active_knowledge_server.storage.sqlite_store import configured_sqlite_paths


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workers = parse_workers(args.workers)
    cache_modes = ("cold", "hot") if args.cache_mode == "both" else (args.cache_mode,)
    dataset = collect_dataset_summary(args)
    git_commit = current_git_commit()
    records: list[dict[str, object]] = []

    with benchmark_root(args.bench_root) as bench_root_value:
        bench_root = Path(bench_root_value)
        for cache_mode in cache_modes:
            for worker in workers:
                scenario_workdir = bench_root / f"{args.mode}-{args.target}-{cache_mode}-{worker}"
                hot_workdir = scenario_workdir / "hot"
                for sample_index in range(max(args.repeat, 1)):
                    if cache_mode == "cold":
                        workdir = scenario_workdir / f"sample-{sample_index}"
                    else:
                        workdir = hot_workdir
                    record = run_sample(
                        args=args,
                        worker=worker,
                        cache_mode=cache_mode,
                        sample_index=sample_index,
                        workdir=workdir,
                        dataset=dataset,
                        git_commit=git_commit,
                    )
                    records.append(record)
                    emit_record(record, output_path=args.output)

    if args.output is None:
        return 0

    print(
        json.dumps(
            {
                "schema_version": "index_benchmark_summary.v1",
                "record_count": len(records),
                "output": str(args.output),
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
    cache_mode: str,
    sample_index: int,
    workdir: Path,
    dataset: dict[str, object],
    git_commit: str | None,
) -> dict[str, object]:
    if cache_mode == "cold" and workdir.exists():
        remove_tree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    resolved = resolve_benchmark_config(args=args, worker=worker, workdir=workdir)
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
    counts = collect_storage_counts(resolved, args.target)

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
        "workdir": str(workdir),
        "result_status": str(result_payload.get("result_status", "unknown")),
        "wall_seconds": round(wall_seconds, 6),
        "cpu_seconds": round(cpu_seconds, 6),
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "rss_delta_bytes": max(rss_after - rss_before, 0),
        "warning_count": len(result_payload.get("warnings", [])),
        "plan_summary": result_payload.get("plan"),
        "object_counts": counts,
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
    set_nested(overrides, ("indexing", "incremental"), args.mode == "incremental")
    set_nested(
        overrides,
        ("indexing", "write_target"),
        "baseline" if args.target == "baseline" else "local_overlay",
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
    paths = configured_sqlite_paths(resolved.model, cwd=Path.cwd())
    if target == "baseline":
        metadata_path = paths["baseline_metadata"]
    else:
        metadata_path = paths["overlay_metadata"]
    jobs_path = paths["jobs"]
    counts.update(query_table_counts(metadata_path))
    if jobs_path.exists():
        counts["job"] = query_table_count(jobs_path, "job")
    return counts


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
