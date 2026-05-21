"""Command-line entry point for Active Knowledge Server."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
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
