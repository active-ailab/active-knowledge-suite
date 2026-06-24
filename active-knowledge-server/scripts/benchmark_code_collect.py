#!/usr/bin/env python3
"""Benchmark code collect executor modes without running full index writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from active_knowledge_server.config.loader import resolve_config, set_nested
from active_knowledge_server.connectors.workspace import WorkspaceConnector
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID
from active_knowledge_server.indexing.code_indexer import (
    _CodeCollectTask,
    _CollectedCodeEntry,
    _WORKSPACE_LANGUAGE_SET,
    _collect_code_entry_task,
)
from active_knowledge_server.indexing.parallel import parallel_map_ordered, resolve_indexing_workers


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark code collect thread/process/hybrid modes on one workspace slice.",
    )
    parser.add_argument("--config", type=Path, required=True, help="Baseline config YAML path.")
    parser.add_argument("--workspace", type=Path, help="Optional workspace root override.")
    parser.add_argument(
        "--paths-include",
        help="Optional comma-separated include roots to override config paths.include.",
    )
    parser.add_argument(
        "--parallel-modes",
        default="thread,process,hybrid",
        help="Comma-separated mode list, for example thread,process,hybrid.",
    )
    parser.add_argument(
        "--workers",
        default="4",
        help="Worker count or auto. One value is applied to every mode in this run.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Sample count per mode.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        help="Optional cap on collected code files after stable path ordering.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path. Prints JSON to stdout when omitted.",
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        help="Optional Markdown summary output path.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    modes = _parse_modes(args.parallel_modes)
    workers = _parse_workers(args.workers)
    sample_count = max(args.repeat, 1)

    inventory_config = _resolve_runtime_config(
        config_path=args.config,
        workspace=args.workspace,
        include_paths=_parse_optional_csv(args.paths_include),
        workers=workers,
        parallel_mode="thread",
    )
    connector = WorkspaceConnector.from_config(inventory_config.model, cwd=Path.cwd())
    inventory = connector.scan()
    selected_entries = _select_code_entries(
        inventory=inventory,
        max_files=args.max_files,
    )
    if not selected_entries:
        raise SystemExit("No code files matched the configured workspace slice.")

    records: list[dict[str, Any]] = []
    baseline_signature: str | None = None
    for mode in modes:
        scenario_config = _resolve_runtime_config(
            config_path=args.config,
            workspace=args.workspace,
            include_paths=_parse_optional_csv(args.paths_include),
            workers=workers,
            parallel_mode=mode,
        )
        for sample_index in range(sample_count):
            record = _run_collect_sample(
                config=scenario_config.model,
                selected_entries=selected_entries,
                sample_index=sample_index,
                total_workspace_files=len(inventory.files),
                include_paths=_parse_optional_csv(args.paths_include),
            )
            if baseline_signature is None and mode == "thread":
                baseline_signature = str(record["output_signature"])
            record["equivalent_to_thread_baseline"] = (
                True
                if baseline_signature is None
                else str(record["output_signature"]) == baseline_signature
            )
            records.append(record)

    payload = {
        "schema_version": "code_collect_benchmark.v1",
        "workspace_root": inventory.workspace_root,
        "include_paths": list(_parse_optional_csv(args.paths_include) or ()),
        "selected_file_count": len(selected_entries),
        "total_workspace_files": len(inventory.files),
        "max_files": args.max_files,
        "workers_requested": workers,
        "parallel_modes": list(modes),
        "records": records,
    }

    if args.output is None:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(str(args.output))

    if args.summary_output is not None:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(
            _render_summary_markdown(payload),
            encoding="utf-8",
        )

    return 0


def _resolve_runtime_config(
    *,
    config_path: Path,
    workspace: Path | None,
    include_paths: tuple[str, ...] | None,
    workers: int | str,
    parallel_mode: str,
):
    overrides: dict[str, object] = {}
    if workspace is not None:
        set_nested(overrides, ("project", "workspace_root"), str(workspace))
    if include_paths is not None:
        set_nested(overrides, ("paths", "include"), list(include_paths))
    set_nested(overrides, ("indexing", "workers"), workers)
    set_nested(overrides, ("indexing", "parallel", "mode"), parallel_mode)
    return resolve_config(config_path=config_path, cli_overrides=overrides, env={}, cwd=Path.cwd())


def _select_code_entries(*, inventory, max_files: int | None):
    selected = tuple(
        entry
        for entry in sorted(inventory.files, key=lambda item: item.relative_path)
        if entry.language in _WORKSPACE_LANGUAGE_SET
    )
    if max_files is None or max_files <= 0:
        return selected
    return selected[:max_files]


def _run_collect_sample(
    *,
    config,
    selected_entries,
    sample_index: int,
    total_workspace_files: int,
    include_paths: tuple[str, ...] | None,
) -> dict[str, Any]:
    workspace_root = Path(config.project.workspace_root)
    tasks = tuple(
        _CodeCollectTask(
            snapshot_id=CURRENT_SNAPSHOT_ID,
            workspace_root=str(workspace_root),
            relative_path=entry.relative_path,
            content_hash=entry.content_hash,
            area=entry.area,
            language=entry.language,
            is_symlink=entry.is_symlink,
            prefer_ctags=config.indexing.code.enable_ctags,
        )
        for entry in selected_entries
    )
    resolved_workers = resolve_indexing_workers(
        config.indexing.workers,
        configured_mode=config.indexing.parallel.mode,
        task_count=len(tasks),
        phase="code",
        allow_process=True,
    )
    wall_started_at = time.perf_counter()
    cpu_started_at = time.process_time()
    results = parallel_map_ordered(
        tasks,
        key=lambda task: task.relative_path,
        mapper=_collect_code_entry_task,
        workers=resolved_workers,
    )
    wall_seconds = time.perf_counter() - wall_started_at
    parent_cpu_seconds = time.process_time() - cpu_started_at

    successful_results = tuple(result.value for result in results if result.value is not None)
    warning_count = sum(len(item.warnings) for item in successful_results)
    error_count = sum(1 for result in results if result.error is not None)
    parser_seconds = sum(item.elapsed_seconds for item in successful_results)
    output_signature = _collect_output_signature(successful_results)

    return {
        "sample_index": sample_index,
        "parallel_mode": config.indexing.parallel.mode,
        "workers_requested": config.indexing.workers,
        "effective_workers": resolved_workers.to_dict(),
        "workspace_root": str(workspace_root),
        "include_paths": list(include_paths or ()),
        "selected_file_count": len(selected_entries),
        "total_workspace_files": total_workspace_files,
        "wall_seconds": round(wall_seconds, 6),
        "parent_cpu_seconds": round(parent_cpu_seconds, 6),
        "parser_seconds": round(parser_seconds, 6),
        "warning_count": warning_count,
        "error_count": error_count,
        "output_signature": output_signature,
    }


def _collect_output_signature(results: tuple[_CollectedCodeEntry | None, ...]) -> str:
    hasher = hashlib.sha256()
    for item in results:
        if item is None:
            continue
        payload = {
            "relative_path": item.relative_path,
            "file_record": asdict(item.file_record),
            "parsed_code": None if item.parsed_code is None else item.parsed_code.to_dict(),
            "parsed_makefile": (
                None if item.parsed_makefile is None else item.parsed_makefile.to_dict()
            ),
            "warnings": [warning.to_dict() for warning in item.warnings],
        }
        hasher.update(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8"))
    return f"sha256:{hasher.hexdigest()}"


def _render_summary_markdown(payload: dict[str, Any]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in payload["records"]:
        grouped.setdefault(str(record["parallel_mode"]), []).append(record)

    reference = grouped.get("thread", [])
    reference_p50 = _p50([float(item["wall_seconds"]) for item in reference]) if reference else None

    lines = [
        "# Code Collect Benchmark",
        "",
        f"- Workspace root: `{payload['workspace_root']}`",
        f"- Include paths: `{', '.join(payload['include_paths']) or '(config default)'}`",
        f"- Selected code files: `{payload['selected_file_count']}` / `{payload['total_workspace_files']}`",
        f"- Workers requested: `{payload['workers_requested']}`",
        "",
        "| Mode | Samples | p50 wall (s) | p50 parent cpu (s) | p50 parser (s) | Speedup vs thread | Equivalent |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for mode in ("thread", "process", "hybrid"):
        records = grouped.get(mode)
        if not records:
            continue
        wall_p50 = _p50([float(item["wall_seconds"]) for item in records])
        cpu_p50 = _p50([float(item["parent_cpu_seconds"]) for item in records])
        parser_p50 = _p50([float(item["parser_seconds"]) for item in records])
        speedup = (
            f"{reference_p50 / wall_p50:.2f}x"
            if reference_p50 is not None and wall_p50 > 0
            else "n/a"
        )
        equivalent = "yes" if all(bool(item["equivalent_to_thread_baseline"]) for item in records) else "no"
        lines.append(
            f"| {mode} | {len(records)} | {wall_p50:.3f} | {cpu_p50:.3f} | "
            f"{parser_p50:.3f} | {speedup} | {equivalent} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def _parse_optional_csv(raw: str | None) -> tuple[str, ...] | None:
    if raw is None or not raw.strip():
        return None
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(values)


def _parse_modes(raw: str) -> tuple[str, ...]:
    allowed = {"thread", "process", "hybrid"}
    values = _parse_optional_csv(raw)
    if not values:
        raise ValueError("at least one parallel mode is required")
    invalid = [value for value in values if value not in allowed]
    if invalid:
        raise ValueError(f"unsupported parallel mode(s): {', '.join(invalid)}")
    return values


def _parse_workers(raw: str) -> int | str:
    candidate = raw.strip().lower()
    if candidate == "auto":
        return "auto"
    value = int(candidate)
    if value <= 0:
        raise ValueError("workers must be a positive integer or auto")
    return value


def _p50(values: list[float]) -> float:
    if not values:
        return math.nan
    return float(statistics.median(sorted(values)))


if __name__ == "__main__":
    raise SystemExit(main())
