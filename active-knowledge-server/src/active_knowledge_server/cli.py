"""Command-line entry point for Active Knowledge Server."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from active_knowledge_server import __version__
from active_knowledge_server.config.loader import (
    ConfigDict,
    ConfigError,
    ResolvedConfig,
    normalize_transport,
    resolve_config,
    resolve_runtime_path,
    set_nested,
)
from active_knowledge_server.config.schema import summarize_config
from active_knowledge_server.config.workdir import (
    WorkdirLayout,
    initialize_workdir,
    layout_from_config,
)
from active_knowledge_server.eval import EvalRunner
from active_knowledge_server.eval.baseline import (
    compare_against_baseline,
    create_baseline_snapshot,
    load_baseline_snapshot,
    load_eval_report_payload,
    save_baseline_snapshot,
)
from active_knowledge_server.eval.stability import StabilityBenchmark
from active_knowledge_server.security.config import (
    SecurityBlockedWarning,
    SecurityConfigError,
    SecurityValidationResult,
    validate_startup_security,
)
from active_knowledge_server.server import build_server_app, server_name
from active_knowledge_server.storage.maintenance import clean_local_state
from active_knowledge_server.storage.validation import validate_storage_consistency

_TRANSPORT_CHOICES = ("stdio", "streamable-http", "http")
_FORMAT_CHOICES = ("text", "json")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser."""

    parser = argparse.ArgumentParser(
        prog="active-kb",
        description="Active Knowledge Server CLI.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print active-knowledge-server version and exit.",
    )

    common = argparse.ArgumentParser(add_help=False)
    add_common_config_options(common)

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    init_parser = subparsers.add_parser(
        "init",
        parents=[common],
        help="Initialize a local Active Knowledge workdir.",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the generated local config if it already exists.",
    )
    init_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    init_parser.set_defaults(handler=handle_init)

    serve_parser = subparsers.add_parser(
        "serve",
        parents=[common],
        help="Run the MCP server or emit a machine-readable launch plan with --format json.",
    )
    serve_parser.add_argument(
        "--transport",
        choices=_TRANSPORT_CHOICES,
        help="MCP transport to use. 'http' is accepted as streamable-http alias.",
    )
    serve_parser.add_argument("--host", help="HTTP host for streamable-http transport.")
    serve_parser.add_argument("--port", type=int, help="HTTP port for streamable-http transport.")
    serve_parser.add_argument(
        "--expose-ops-tools",
        action="store_true",
        help="Expose operational tools when server policy allows it.",
    )
    serve_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    serve_parser.set_defaults(handler=handle_serve)

    index_parser = subparsers.add_parser(
        "index",
        parents=[common],
        help="Resolve config and prepare an indexing job plan.",
    )
    mode = index_parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="Plan a full local overlay rebuild.")
    mode.add_argument(
        "--incremental",
        action="store_true",
        help="Plan an incremental index update.",
    )
    index_parser.add_argument(
        "--source",
        choices=("all", "code", "docs"),
        default="all",
        help="Source family to index.",
    )
    index_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    index_parser.set_defaults(handler=handle_index)

    status_parser = subparsers.add_parser(
        "status",
        parents=[common],
        help="Show Active Knowledge local state and config summary.",
    )
    status_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    status_parser.set_defaults(handler=handle_status)

    validate_parser = subparsers.add_parser(
        "validate",
        parents=[common],
        help="Validate basic CLI, config, and local path readiness.",
    )
    validate_parser.add_argument(
        "--strict",
        action="store_true",
        help="Return failure when workspace, source docs, or workdir paths are missing.",
    )
    validate_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    validate_parser.set_defaults(handler=handle_validate)

    clean_parser = subparsers.add_parser(
        "clean",
        parents=[common],
        help="Clean local runtime cache, tmp, jobs, snapshots, or compact overlay metadata.",
    )
    clean_parser.add_argument("--cache", action="store_true", help="Clean local cache files.")
    clean_parser.add_argument("--tmp", action="store_true", help="Clean local tmp files.")
    clean_parser.add_argument(
        "--old-jobs",
        action="store_true",
        help="Delete old terminal jobs while preserving active jobs.",
    )
    clean_parser.add_argument(
        "--old-snapshots",
        action="store_true",
        help="Delete old local overlay snapshots.",
    )
    clean_parser.add_argument(
        "--compact-overlay",
        action="store_true",
        help="Compact local overlay control rows and rebuild overlay FTS.",
    )
    clean_parser.add_argument(
        "--keep",
        type=int,
        default=3,
        help="Number of old jobs or snapshots to keep.",
    )
    clean_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    clean_parser.set_defaults(handler=handle_clean)

    eval_parser = subparsers.add_parser(
        "eval",
        help="Run eval suites and emit machine-readable gate summaries.",
    )
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", metavar="EVAL_COMMAND")

    eval_run_parser = eval_subparsers.add_parser(
        "run",
        parents=[common],
        help="Load and execute one eval case suite.",
    )
    eval_run_parser.add_argument(
        "--gate",
        default="v1",
        help="Gate identifier recorded in the eval report.",
    )
    eval_run_parser.add_argument(
        "--cases",
        type=Path,
        default=Path("eval") / "cases.yaml",
        help="Eval case YAML file.",
    )
    eval_run_parser.add_argument(
        "--report",
        type=Path,
        help="Optional output path for the eval report JSON.",
    )
    eval_run_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    eval_run_parser.set_defaults(handler=handle_eval_run)

    perf_parser = subparsers.add_parser(
        "perf",
        help="Run performance gate benchmarks and emit machine-readable reports.",
    )
    perf_subparsers = perf_parser.add_subparsers(dest="perf_command", metavar="PERF_COMMAND")

    perf_run_parser = perf_subparsers.add_parser(
        "run",
        parents=[common],
        help="Load and execute the E7-03 performance suite.",
    )
    perf_run_parser.add_argument(
        "--gate",
        default="v1",
        help="Gate identifier recorded in the performance report.",
    )
    perf_run_parser.add_argument(
        "--cases",
        type=Path,
        default=Path("eval") / "performance_cases.yaml",
        help="Performance case YAML file.",
    )
    perf_run_parser.add_argument(
        "--report",
        type=Path,
        help="Optional output path for the performance report JSON.",
    )
    perf_run_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    perf_run_parser.set_defaults(handler=handle_perf_run)

    stability_parser = subparsers.add_parser(
        "stability",
        help="Run stability gate benchmarks and emit machine-readable reports.",
    )
    stability_subparsers = stability_parser.add_subparsers(
        dest="stability_command",
        metavar="STABILITY_COMMAND",
    )

    stability_run_parser = stability_subparsers.add_parser(
        "run",
        parents=[common],
        help="Load and execute the E7-04 stability suite.",
    )
    stability_run_parser.add_argument(
        "--gate",
        default="v1",
        help="Gate identifier recorded in the stability report.",
    )
    stability_run_parser.add_argument(
        "--cases",
        type=Path,
        default=Path("eval") / "stability_cases.yaml",
        help="Stability case YAML file.",
    )
    stability_run_parser.add_argument(
        "--report",
        type=Path,
        help="Optional output path for the stability report JSON.",
    )
    stability_run_parser.add_argument(
        "--soak-seconds",
        type=int,
        default=60,
        help="Timed soak window. Use 28800 for the 8-hour release gate.",
    )
    stability_run_parser.add_argument(
        "--mixed-query-count",
        type=int,
        default=500,
        help="Number of mixed queries to execute for the success-rate probe.",
    )
    stability_run_parser.add_argument(
        "--readonly-workers",
        type=int,
        default=8,
        help="Concurrent readonly worker count for the non-blocking probe.",
    )
    stability_run_parser.add_argument(
        "--readonly-queries",
        type=int,
        default=64,
        help="Total readonly queries to issue in the concurrency probe.",
    )
    stability_run_parser.add_argument(
        "--readonly-timeout-seconds",
        type=float,
        default=5.0,
        help="Per-query timeout for readonly concurrency validation.",
    )
    stability_run_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    stability_run_parser.set_defaults(handler=handle_stability_run)

    baseline_parser = subparsers.add_parser(
        "eval-baseline",
        help="Save and compare release baseline snapshots for E7-05 regression gating.",
    )
    baseline_subparsers = baseline_parser.add_subparsers(
        dest="baseline_command",
        metavar="BASELINE_COMMAND",
    )

    baseline_save_parser = baseline_subparsers.add_parser(
        "save",
        parents=[common],
        help="Save the current release baseline snapshot.",
    )
    baseline_save_parser.add_argument(
        "--baseline-id",
        help="Identifier to embed into the saved baseline snapshot.",
    )
    baseline_save_parser.add_argument(
        "--quality-report",
        type=Path,
        help="Existing quality report JSON. When omitted, the command runs the quality gate.",
    )
    baseline_save_parser.add_argument(
        "--performance-report",
        type=Path,
        help="Existing performance report JSON. When omitted, the command runs the performance gate.",
    )
    baseline_save_parser.add_argument(
        "--stability-report",
        type=Path,
        help="Optional stability report JSON to attach to the baseline snapshot.",
    )
    baseline_save_parser.add_argument(
        "--output",
        type=Path,
        help="Optional explicit baseline snapshot path. Defaults to baseline artifacts/eval-baseline/<baseline-id>.json.",
    )
    baseline_save_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    baseline_save_parser.set_defaults(handler=handle_eval_baseline_save)

    baseline_compare_parser = baseline_subparsers.add_parser(
        "compare",
        parents=[common],
        help="Compare current gate reports with the previous release baseline.",
    )
    baseline_compare_parser.add_argument(
        "--baseline",
        type=Path,
        help="Saved baseline snapshot path. Defaults to baseline artifacts/eval-baseline/latest.json.",
    )
    baseline_compare_parser.add_argument(
        "--quality-report",
        type=Path,
        help="Existing current quality report JSON. When omitted, the command runs the quality gate.",
    )
    baseline_compare_parser.add_argument(
        "--performance-report",
        type=Path,
        help="Existing current performance report JSON. When omitted, the command runs the performance gate.",
    )
    baseline_compare_parser.add_argument(
        "--stability-report",
        type=Path,
        help="Optional current stability report JSON to include in the regression report context.",
    )
    baseline_compare_parser.add_argument(
        "--report",
        type=Path,
        help="Optional output path for the regression comparison report JSON.",
    )
    baseline_compare_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    baseline_compare_parser.set_defaults(handler=handle_eval_baseline_compare)

    return parser


def add_common_config_options(parser: argparse.ArgumentParser) -> None:
    """Add config-related options shared by subcommands."""

    parser.add_argument("--config", type=Path, help="Baseline/static config YAML path.")
    parser.add_argument("--local-config", type=Path, help="User-local config YAML path.")
    parser.add_argument("--workdir", type=Path, help="Active Knowledge workdir.")
    parser.add_argument("--workspace", type=Path, help="Active project workspace root.")
    parser.add_argument("--source-docs-root", type=Path, help="Knowledge source docs root.")
    parser.add_argument("--profile", help="Default profile id or 'auto'.")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"active-knowledge-server {__version__}")
        return 0

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return cast(Callable[[argparse.Namespace], int], handler)(args)
    except SecurityConfigError as exc:
        return emit_blocked_result(args, exc.result)
    except ConfigError as exc:
        result = SecurityValidationResult(
            (
                SecurityBlockedWarning(
                    code="schema.invalid_request",
                    message=str(exc),
                    suggested_action="Fix the configuration and rerun the command.",
                ),
            )
        )
        return emit_blocked_result(args, result)


def handle_init(args: argparse.Namespace) -> int:
    """Initialize the local workdir skeleton."""

    resolved = resolve_from_args(args)
    result = initialize_workdir(resolved, force=bool(args.force))
    layout = result.layout

    summary = config_summary(resolved)
    payload = {
        "command": "init",
        "status": "ok",
        "created": [str(path) for path in result.created],
        "warnings": [warning.to_dict() for warning in result.warnings],
        "baseline_manifest": result.baseline_manifest.to_dict(),
        "config": summary,
    }
    if args.format == "json":
        print_json(payload)
    else:
        print(f"Initialized Active Knowledge workdir: {layout.workdir}")
        print(f"Local config: {layout.local_config_path}")
        print(f"Workspace: {summary['workspace_root']}")
        for warning in result.warnings:
            print(f"Warning [{warning.code}]: {warning.message}")
    return 0


def handle_serve(args: argparse.Namespace) -> int:
    """Resolve a server launch plan."""

    resolved = resolve_from_args(args, command_overrides=serve_overrides(args))
    security_result = validate_startup_security(resolved.model)
    if security_result.blocked:
        return emit_blocked_result(args, security_result)

    runtime = build_server_app(resolved)
    summary = config_summary(resolved)
    payload = {
        "command": "serve",
        "status": "ready",
        "server": server_name(),
        "config": summary,
        "mcp": runtime.describe(),
    }
    if args.format == "json":
        print_json(payload)
        return 0

    runtime.run()
    return 0


def handle_index(args: argparse.Namespace) -> int:
    """Resolve an index job plan."""

    resolved = resolve_from_args(args, command_overrides=index_overrides(args))
    summary = config_summary(resolved)
    payload = {
        "command": "index",
        "status": "ready",
        "source": args.source,
        "mode": "full" if args.full else "incremental",
        "config": summary,
        "note": "Index pipeline execution is scheduled for Phase 4; C1-02 validates job planning.",
    }
    if args.format == "json":
        print_json(payload)
    else:
        print(f"Index plan ready: {payload['mode']} ({payload['source']})")
        print(f"Workdir: {summary['workdir']}")
        print("Index pipeline execution is not started by the C1-02 skeleton.")
    return 0


def handle_status(args: argparse.Namespace) -> int:
    """Show local state and config summary."""

    resolved = resolve_from_args(args)
    layout = workdir_layout(resolved)
    summary = config_summary(resolved)
    payload = {
        "command": "status",
        "status": "ok",
        "config": summary,
        "paths": path_status(layout, resolved),
        "index": {
            "result_status": "partial_ready",
            "message": "Storage and indexing backends are not implemented yet.",
        },
    }
    if args.format == "json":
        print_json(payload)
    else:
        print("Active Knowledge status")
        print(f"Workdir: {layout.workdir} ({exists_label(layout.workdir)})")
        print(
            f"Local config: {layout.local_config_path} ({exists_label(layout.local_config_path)})"
        )
        print(f"Transport: {summary['transport']}")
        print("Index: partial_ready (storage/indexing backends pending)")
    return 0


def handle_validate(args: argparse.Namespace) -> int:
    """Validate basic setup readiness."""

    resolved = resolve_from_args(args)
    layout = workdir_layout(resolved)
    checks = validation_checks(layout, resolved, strict=bool(args.strict))
    storage_report = validate_storage_consistency(resolved.model, cwd=Path.cwd())
    errors = [check for check in checks if check["level"] == "error"]
    payload = {
        "schema_version": "active_kb_validate.v1",
        "command": "validate",
        "status": "error" if errors or storage_report.status == "blocked" else "ok",
        "checks": checks,
        "storage_report": storage_report.to_dict(),
        "config": config_summary(resolved),
    }

    if args.format == "json":
        print_json(payload)
    else:
        print("Validation checks")
        for check in checks:
            print(f"- {check['level']}: {check['name']} - {check['message']}")
        print(f"Storage consistency: {storage_report.status}")
        for storage_check in storage_report.checks:
            print(
                f"- {storage_check.severity}: "
                f"{storage_check.check_code} - {storage_check.message}"
            )
    return 1 if errors or storage_report.status == "blocked" else 0


def handle_clean(args: argparse.Namespace) -> int:
    """Clean local runtime state without touching baseline assets."""

    resolved = resolve_from_args(args)
    report = clean_local_state(
        resolved.model,
        cwd=Path.cwd(),
        clean_cache=bool(args.cache),
        clean_tmp=bool(args.tmp),
        old_jobs_keep=int(args.keep) if args.old_jobs else None,
        old_snapshots_keep=int(args.keep) if args.old_snapshots else None,
        compact_overlay=bool(args.compact_overlay),
    )
    payload = {
        "command": "clean",
        "status": "ok",
        "clean_report": report.to_dict(),
    }
    if args.format == "json":
        print_json(payload)
    else:
        print("Clean completed")
        print(f"Deleted files: {report.deleted_files}")
        print(f"Deleted dirs: {report.deleted_dirs}")
        print(f"Deleted jobs: {report.deleted_jobs}")
        print(f"Deleted snapshots: {report.deleted_snapshots}")
        if report.compact:
            print(f"Compact: {report.compact}")
    return 0


def handle_eval_run(args: argparse.Namespace) -> int:
    """Load and execute the configured eval suite."""

    resolved = resolve_from_args(args)
    runner = EvalRunner.from_config(resolved.model, cwd=Path.cwd())
    report = runner.run(
        resolve_eval_cases_path(args),
        gate_id=str(args.gate),
    )
    if args.report is not None:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = report.model_copy(update={"artifacts": report.artifacts + (str(report_path),)})
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    payload = {
        "command": "eval run",
        **report.to_dict(),
        "config": config_summary(resolved),
    }
    if args.format == "json":
        print_json(payload)
    else:
        print(f"Eval suite: {report.suite_id}")
        print(f"Gate: {report.gate_id}")
        print(f"Status: {report.status}")
        print(
            f"Cases: {report.metrics['passed_cases']}/{report.metrics['executed_cases']} passed"
        )
        for warning in report.warnings:
            print(f"Warning [{warning['code']}]: {warning['message']}")
    return 1 if report.status == "fail" else 0


def handle_perf_run(args: argparse.Namespace) -> int:
    """Load and execute the configured performance suite."""

    resolved = resolve_from_args(args)
    runner = EvalRunner.from_config(resolved.model, cwd=Path.cwd())
    report = runner.run(
        resolve_performance_cases_path(args),
        gate_id=str(args.gate),
        suite_kind="performance",
    )
    if args.report is not None:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = report.model_copy(update={"artifacts": report.artifacts + (str(report_path),)})
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    payload = {
        "command": "perf run",
        **report.to_dict(),
        "config": config_summary(resolved),
    }
    if args.format == "json":
        print_json(payload)
    else:
        print(f"Performance suite: {report.suite_id}")
        print(f"Gate: {report.gate_id}")
        print(f"Status: {report.status}")
        performance_gate = report.metrics.get("performance_gate", {})
        sample_counts = performance_gate.get("sample_counts", {})
        if sample_counts:
            print(f"Probes: {len(sample_counts)}")
        for warning in report.warnings:
            print(f"Warning [{warning['code']}]: {warning['message']}")
    return 1 if report.status == "fail" else 0


def handle_stability_run(args: argparse.Namespace) -> int:
    """Load and execute the configured stability suite."""

    resolved = resolve_from_args(args)
    runner = EvalRunner.from_config(
        resolved.model,
        cwd=Path.cwd(),
        stability_benchmark_factory=lambda: StabilityBenchmark(
            soak_seconds=int(args.soak_seconds),
            mixed_query_count=int(args.mixed_query_count),
            readonly_workers=int(args.readonly_workers),
            readonly_query_count=int(args.readonly_queries),
            readonly_timeout_seconds=float(args.readonly_timeout_seconds),
        ),
    )
    report = runner.run(
        resolve_stability_cases_path(args),
        gate_id=str(args.gate),
        suite_kind="stability",
    )
    if args.report is not None:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = report.model_copy(update={"artifacts": report.artifacts + (str(report_path),)})
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    payload = {
        "command": "stability run",
        **report.to_dict(),
        "config": config_summary(resolved),
    }
    if args.format == "json":
        print_json(payload)
    else:
        print(f"Stability suite: {report.suite_id}")
        print(f"Gate: {report.gate_id}")
        print(f"Status: {report.status}")
        stability_gate = report.metrics.get("stability_gate", {})
        release_window = stability_gate.get("release_window", {})
        if release_window:
            print(
                "Release window: "
                f"{release_window.get('actual_soak_seconds')}s soak / "
                f"{release_window.get('actual_mixed_query_count')} mixed queries"
            )
        for warning in report.warnings:
            print(f"Warning [{warning['code']}]: {warning['message']}")
    return 1 if report.status == "fail" else 0


def handle_eval_baseline_save(args: argparse.Namespace) -> int:
    """Save one release baseline snapshot."""

    resolved = resolve_from_args(args)
    quality_report = (
        load_eval_report_payload(Path(args.quality_report))
        if args.quality_report is not None
        else _run_quality_gate_report(resolved)
    )
    performance_report = (
        load_eval_report_payload(Path(args.performance_report))
        if args.performance_report is not None
        else _run_performance_gate_report(resolved)
    )
    stability_report = (
        None
        if args.stability_report is None
        else load_eval_report_payload(Path(args.stability_report))
    )
    baseline_id = str(args.baseline_id or _default_baseline_id())
    output_path = (
        Path(args.output)
        if args.output is not None
        else _baseline_snapshot_dir(resolved) / f"{baseline_id}.json"
    )
    latest_path = output_path.parent / "latest.json"
    snapshot = create_baseline_snapshot(
        baseline_id=baseline_id,
        quality_report=quality_report,
        performance_report=performance_report,
        stability_report=stability_report,
        source_artifacts=tuple(
            str(path)
            for path in (
                Path(args.quality_report) if args.quality_report is not None else None,
                Path(args.performance_report) if args.performance_report is not None else None,
                Path(args.stability_report) if args.stability_report is not None else None,
            )
            if path is not None
        ),
    )
    save_baseline_snapshot(snapshot, output_path=output_path, latest_path=latest_path)
    payload = {
        "command": "eval-baseline save",
        "status": "ok",
        "baseline_id": snapshot.baseline_id,
        "output": str(output_path),
        "latest": str(latest_path),
        "baseline": snapshot.to_dict(),
        "config": config_summary(resolved),
    }
    if args.format == "json":
        print_json(payload)
    else:
        print(f"Baseline saved: {snapshot.baseline_id}")
        print(f"Output: {output_path}")
        print(f"Latest: {latest_path}")
    return 0


def handle_eval_baseline_compare(args: argparse.Namespace) -> int:
    """Compare current gate reports with the saved release baseline."""

    resolved = resolve_from_args(args)
    baseline_path = (
        Path(args.baseline)
        if args.baseline is not None
        else _baseline_snapshot_dir(resolved) / "latest.json"
    )
    baseline = load_baseline_snapshot(baseline_path)
    current_quality_report = (
        load_eval_report_payload(Path(args.quality_report))
        if args.quality_report is not None
        else _run_quality_gate_report(resolved)
    )
    current_performance_report = (
        load_eval_report_payload(Path(args.performance_report))
        if args.performance_report is not None
        else _run_performance_gate_report(resolved)
    )
    current_stability_report = (
        None
        if args.stability_report is None
        else load_eval_report_payload(Path(args.stability_report))
    )
    report = compare_against_baseline(
        baseline=baseline,
        baseline_path=baseline_path,
        current_quality_report=current_quality_report,
        current_performance_report=current_performance_report,
        current_stability_report=current_stability_report,
    )
    if args.report is not None:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = report.model_copy(update={"artifacts": report.artifacts + (str(report_path),)})
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    payload = {
        "command": "eval-baseline compare",
        **report.to_dict(),
        "config": config_summary(resolved),
    }
    if args.format == "json":
        print_json(payload)
    else:
        print(f"Regression baseline: {report.baseline_id}")
        print(f"Baseline path: {baseline_path}")
        print(f"Status: {report.status}")
        for failure in report.failures:
            print(f"Failure [{failure['check']}]: {failure}")
        for warning in report.warnings:
            print(f"Warning [{warning['check']}]: {warning}")
    return 1 if report.status == "fail" else 0


def resolve_eval_cases_path(args: argparse.Namespace) -> Path:
    """Resolve the default eval suite path for the selected gate."""

    cases_path = getattr(args, "cases", None)
    if cases_path is not None:
        candidate = Path(cases_path)
        if candidate != Path("eval") / "cases.yaml":
            return candidate
    if str(args.gate) == "quality":
        return Path("eval") / "quality_cases.yaml"
    if str(args.gate) == "performance":
        return Path("eval") / "performance_cases.yaml"
    if str(args.gate) == "stability":
        return Path("eval") / "stability_cases.yaml"
    return Path("eval") / "cases.yaml"


def resolve_performance_cases_path(args: argparse.Namespace) -> Path:
    """Resolve the default performance suite path."""

    cases_path = getattr(args, "cases", None)
    if cases_path is not None:
        return Path(cases_path)
    return Path("eval") / "performance_cases.yaml"


def resolve_stability_cases_path(args: argparse.Namespace) -> Path:
    """Resolve the default stability suite path."""

    cases_path = getattr(args, "cases", None)
    if cases_path is not None:
        return Path(cases_path)
    return Path("eval") / "stability_cases.yaml"


def _run_quality_gate_report(resolved: ResolvedConfig) -> EvalRunReport:
    runner = EvalRunner.from_config(resolved.model, cwd=Path.cwd())
    return runner.run(
        Path("eval") / "quality_cases.yaml",
        gate_id="quality",
        suite_kind="quality",
    )


def _run_performance_gate_report(resolved: ResolvedConfig) -> EvalRunReport:
    runner = EvalRunner.from_config(resolved.model, cwd=Path.cwd())
    return runner.run(
        Path("eval") / "performance_cases.yaml",
        gate_id="performance",
        suite_kind="performance",
    )


def _baseline_snapshot_dir(resolved: ResolvedConfig) -> Path:
    return resolve_runtime_path(resolved.model.storage.artifacts_root, Path.cwd()) / "eval-baseline"


def _default_baseline_id() -> str:
    return "release-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def resolve_from_args(
    args: argparse.Namespace,
    *,
    command_overrides: ConfigDict | None = None,
) -> ResolvedConfig:
    """Resolve config for a parsed command."""

    overrides = merge_cli_overrides(common_overrides(args), command_overrides or {})
    return resolve_config(
        config_path=getattr(args, "config", None),
        local_config_path=getattr(args, "local_config", None),
        cli_overrides=overrides,
    )


def common_overrides(args: argparse.Namespace) -> ConfigDict:
    """Build config overrides from common CLI flags."""

    overrides: ConfigDict = {}
    optional_paths: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("workdir", ("runtime", "workdir")),
        ("workspace", ("project", "workspace_root")),
        ("source_docs_root", ("runtime", "source_docs_root")),
    )
    for attr, path in optional_paths:
        value = getattr(args, attr, None)
        if value is not None:
            set_nested(overrides, path, str(value))

    profile = getattr(args, "profile", None)
    if profile is not None:
        set_nested(overrides, ("project", "default_profile"), profile)
    return overrides


def serve_overrides(args: argparse.Namespace) -> ConfigDict:
    """Build serve-specific config overrides."""

    overrides: ConfigDict = {}
    if args.transport is not None:
        set_nested(overrides, ("server", "transport"), normalize_transport(args.transport))
    if args.host is not None:
        set_nested(overrides, ("server", "http", "host"), args.host)
    if args.port is not None:
        set_nested(overrides, ("server", "http", "port"), args.port)
    if args.expose_ops_tools:
        set_nested(overrides, ("server", "expose_ops_tools"), True)
    return overrides


def index_overrides(args: argparse.Namespace) -> ConfigDict:
    """Build index-specific config overrides."""

    overrides: ConfigDict = {}
    if args.full:
        set_nested(overrides, ("indexing", "incremental"), False)
    elif args.incremental:
        set_nested(overrides, ("indexing", "incremental"), True)
    return overrides


def merge_cli_overrides(low: ConfigDict, high: ConfigDict) -> ConfigDict:
    """Merge command override dictionaries without importing loader internals."""

    merged: ConfigDict = dict(low)
    for key, value in high.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_cli_overrides(current, value)
        else:
            merged[key] = value
    return merged


def workdir_layout(resolved: ResolvedConfig) -> WorkdirLayout:
    """Return resolved workdir paths."""

    return layout_from_config(resolved)


def config_summary(
    resolved: ResolvedConfig,
) -> dict[str, str | int | bool | list[str] | dict[str, Any]]:
    """Return a non-sensitive config summary for CLI output."""

    return cast(
        dict[str, str | int | bool | list[str] | dict[str, Any]],
        summarize_config(
            resolved.model,
            cwd=Path.cwd(),
            loaded_files=resolved.loaded_files,
            local_config_path=resolved.local_config_path,
        ),
    )


def path_status(
    layout: WorkdirLayout,
    resolved: ResolvedConfig,
) -> dict[str, dict[str, str | bool]]:
    """Return existence status for important local paths."""

    cwd = Path.cwd()
    workspace = resolve_runtime_path(resolved.model.project.workspace_root, cwd)
    source_docs = resolve_runtime_path(resolved.model.runtime.source_docs_root, cwd)
    paths = {
        "workspace_root": workspace,
        "source_docs_root": source_docs,
        "workdir": layout.workdir,
        "baseline_dir": layout.baseline_dir,
        "local_dir": layout.local_dir,
        "local_config": layout.local_config_path,
    }
    return {
        name: {"path": str(path), "exists": path.exists(), "kind": path_kind(path)}
        for name, path in paths.items()
    }


def validation_checks(
    layout: WorkdirLayout,
    resolved: ResolvedConfig,
    *,
    strict: bool,
) -> list[dict[str, str]]:
    """Build setup validation checks."""

    statuses = path_status(layout, resolved)
    checks: list[dict[str, str]] = []
    for name, info in statuses.items():
        exists = bool(info["exists"])
        missing_is_error = strict and name in {"workspace_root", "source_docs_root", "workdir"}
        if exists:
            checks.append({"name": name, "level": "ok", "message": f"{info['path']} exists"})
        else:
            level = "error" if missing_is_error else "warning"
            checks.append(
                {
                    "name": name,
                    "level": level,
                    "message": f"{info['path']} does not exist",
                }
            )

    transport = resolved.get("server.transport")
    if transport in {"stdio", "streamable-http"}:
        checks.append({"name": "server.transport", "level": "ok", "message": str(transport)})
    else:
        checks.append(
            {
                "name": "server.transport",
                "level": "error",
                "message": f"unsupported transport: {transport}",
            }
        )

    security_result = validate_startup_security(resolved.model)
    if security_result.ok:
        checks.append(
            {
                "name": "security.fail_safe",
                "level": "ok",
                "message": "fail-safe startup security checks passed",
            }
        )
    else:
        for warning in security_result.warnings:
            checks.append(
                {
                    "name": warning.code,
                    "level": "error",
                    "message": warning.message,
                }
            )
    return checks


def path_kind(path: Path) -> str:
    """Classify an existing or missing path."""

    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "missing"


def exists_label(path: Path) -> str:
    """Return a short existence label."""

    return "exists" if path.exists() else "missing"


def print_json(payload: object) -> None:
    """Print stable JSON output."""

    print(json.dumps(payload, indent=2, sort_keys=True))


def emit_blocked_result(args: argparse.Namespace, result: SecurityValidationResult) -> int:
    """Emit a structured blocked result for JSON callers or a concise text error."""

    payload = result.to_blocked_response()
    if getattr(args, "format", None) == "json":
        print_json(payload)
    else:
        warnings = payload["warnings"]
        if isinstance(warnings, list):
            for warning in warnings:
                if isinstance(warning, dict):
                    print(
                        f"active-kb: blocked [{warning.get('code')}]: {warning.get('message')}",
                        file=sys.stderr,
                    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
