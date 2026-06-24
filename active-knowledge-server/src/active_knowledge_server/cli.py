"""Command-line entry point for Active Knowledge Server."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from active_knowledge_server import __version__
from active_knowledge_server.cli_progress import create_index_progress_reporter
from active_knowledge_server.config.loader import (
    ConfigDict,
    ConfigError,
    ResolvedConfig,
    normalize_transport,
    resolve_config,
    resolve_runtime_path,
    set_nested,
)
from active_knowledge_server.config.schema import IndexResumeMode, summarize_config
from active_knowledge_server.config.workdir import (
    WorkdirLayout,
    initialize_workdir,
    inspect_baseline_manifest,
    inspect_tracked_local_files,
    layout_from_config,
)
from active_knowledge_server.connectors.source_docs import SourceDocsConnector
from active_knowledge_server.connectors.workspace import WorkspaceConnector
from active_knowledge_server.eval import EvalRunner
from active_knowledge_server.eval.baseline import (
    compare_against_baseline,
    create_baseline_snapshot,
    load_baseline_snapshot,
    load_eval_report_payload,
    save_baseline_snapshot,
)
from active_knowledge_server.eval.metrics import PERFORMANCE_GATE_THRESHOLDS
from active_knowledge_server.eval.runner import EvalRunReport
from active_knowledge_server.eval.stability import StabilityBenchmark
from active_knowledge_server.indexing import (
    CODE_INDEXER_SCHEMA_VERSION,
    CURRENT_SNAPSHOT_ID,
    DOC_INDEXER_SCHEMA_VERSION,
    PROFILE_COLLECTOR_SCHEMA_VERSION,
    PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION,
    SNAPSHOT_COLLECTOR_SCHEMA_VERSION,
    WORKSPACE_MAP_SCHEMA_VERSION,
    CodeIndexer,
    DocumentIndexer,
    IncrementalIndexPipeline,
    IncrementalIndexPlan,
    IncrementalIndexResult,
    IndexProgressCallback,
    IndexProgressEvent,
    IndexRunContext,
    ProfileCollector,
    ProfileConditionedRelationExtractor,
    SnapshotCollector,
    WorkspaceMapBuilder,
    count_indexable_workspace_files,
    make_index_plan_signature,
    make_index_task_list,
    noop_progress_callback,
    utc_timestamp,
)
from active_knowledge_server.indexing.jobs import (
    INDEX_JOB_LOCK_ID,
    RESUMABLE_INDEX_JOB_STATUSES,
    RUNNING_JOB_STATUSES,
    JobLockConflictError,
    JobStateTransitionError,
    SQLiteJobStore,
    lock_expired,
)
from active_knowledge_server.mcp.schemas import MCP_INTERFACE_SCHEMA_VERSION
from active_knowledge_server.models import QueryResult, Warning
from active_knowledge_server.models.responses import QUERY_RESULT_SCHEMA_VERSION
from active_knowledge_server.observability.metrics import ObservabilityStore
from active_knowledge_server.parsers import (
    C_FAMILY_PARSER_SCHEMA_VERSION,
    DOC_PARSER_SCHEMA_VERSION,
    KCONFIG_PARSER_SCHEMA_VERSION,
    MAKEFILE_PARSER_SCHEMA_VERSION,
)
from active_knowledge_server.security.config import (
    SecurityBlockedWarning,
    SecurityConfigError,
    SecurityValidationResult,
    validate_startup_security,
)
from active_knowledge_server.server import build_server_app, server_name
from active_knowledge_server.storage import (
    ALL_SCOPE,
    JobRecord,
    StorageMetadata,
    StorageWriteRequest,
    StorageWriteTarget,
    activate_published_storage,
    configured_lancedb_paths,
    materialize_published_storage,
    resolve_published_storage_for_job,
    resolve_staging_storage_paths,
)
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.maintenance import clean_local_state
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    checkpoint_sqlite_database,
    configured_sqlite_paths,
    migrate_local_sqlite_stores,
    migrate_sqlite_store,
)
from active_knowledge_server.storage.validation import validate_storage_consistency

_TRANSPORT_CHOICES = ("stdio", "streamable-http", "http")
_FORMAT_CHOICES = ("text", "json")
_INDEX_JOB_CONTRACT_SCHEMA_VERSION = "index_job_contract.v1"
_INDEX_RESUME_POLICY_SCHEMA_VERSION = "index_resume_policy.v1"
_INDEX_JOB_LOCK_TTL_SECONDS = 24 * 60 * 60
IndexOutputMode = Literal["json_final", "text_dynamic", "text_plain"]
IndexProgressOutputMode = Literal["none", "text_dynamic", "text_plain"]


@dataclass
class IndexJobContext:
    """Runtime context for a persisted CLI index job."""

    store: SQLiteJobStore
    job: JobRecord
    resumed: bool
    plan_signature: dict[str, object] | None
    plan_signature_digest: str | None
    tasks_total: int | None
    tasks_applied: int | None = 0
    tasks_skipped: int | None = 0
    tasks_failed: int | None = 0


class IndexCommandInterrupted(KeyboardInterrupt):
    """KeyboardInterrupt carrying the final CLI payload for a persisted job."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        super().__init__("index interrupted")
        self.payload = dict(payload)


def storage_write_target_for_cli_target(target: str) -> StorageWriteTarget:
    """Translate CLI target names into storage write targets."""

    if target == "baseline":
        return "baseline"
    if target == "local":
        return "overlay"
    raise ValueError(f"unsupported write target: {target}")


def resolve_index_output_mode(
    *,
    output_format: str,
    stream: TextIO | None = None,
    rich_available: bool = True,
) -> IndexOutputMode:
    """Resolve the Phase 0 CLI output contract for indexing progress."""

    if output_format == "json":
        return "json_final"
    if output_format != "text":
        raise ValueError(f"unsupported output format: {output_format}")
    output_stream = stream or sys.stdout
    is_tty = bool(getattr(output_stream, "isatty", lambda: False)())
    if not is_tty or not rich_available:
        return "text_plain"
    return "text_dynamic"


def resolve_index_progress_output_mode(
    *,
    output_format: str,
    output_stream: TextIO | None = None,
    progress_stream: TextIO | None = None,
    rich_available: bool = True,
) -> IndexProgressOutputMode:
    """Resolve where live indexing progress should be rendered."""

    if output_format == "json":
        stream = progress_stream or sys.stderr
        is_tty = bool(getattr(stream, "isatty", lambda: False)())
        if not is_tty:
            return "none"
        return "text_dynamic" if rich_available else "text_plain"
    mode = resolve_index_output_mode(
        output_format=output_format,
        stream=output_stream,
        rich_available=rich_available,
    )
    if mode == "json_final":
        return "none"
    return mode


@contextmanager
def _translate_sigterm_to_keyboard_interrupt():
    """Route SIGTERM through the existing interrupted-index cleanup path."""

    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is None:
        yield
        return

    def handle_sigterm(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.getsignal(sigterm)
        signal.signal(sigterm, handle_sigterm)
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        signal.signal(sigterm, previous)


def resolve_index_resume_policy(args: argparse.Namespace) -> dict[str, object]:
    """Resolve the CLI resume/restart contract into a JSON-safe policy payload."""

    requested_resume = str(getattr(args, "resume", "auto") or "auto").strip()
    requested_job_id = getattr(args, "job_id", None)
    job_id = None if requested_job_id is None else str(requested_job_id).strip()
    if not requested_resume:
        raise ConfigError("--resume requires 'auto' or a non-empty job id.")
    if requested_job_id is not None and not job_id:
        raise ConfigError("--job-id requires a non-empty job id.")

    mode: IndexResumeMode
    resume_job_id: str | None = None
    planned_job_id: str | None = job_id
    if bool(getattr(args, "restart", False)):
        mode = "restart"
    elif bool(getattr(args, "no_resume", False)):
        mode = "disabled"
    elif requested_resume == "auto":
        mode = "auto"
    else:
        if job_id is not None:
            raise ConfigError(
                "--job-id cannot be combined with --resume JOB_ID; "
                "the resume value already identifies the job to continue."
            )
        mode = "job_id"
        resume_job_id = requested_resume
        planned_job_id = requested_resume

    return {
        "schema_version": _INDEX_RESUME_POLICY_SCHEMA_VERSION,
        "mode": mode,
        "resume": requested_resume if mode in {"auto", "job_id"} else None,
        "resume_job_id": resume_job_id,
        "restart": mode == "restart",
        "resume_enabled": mode in {"auto", "job_id"},
        "planned_job_id": planned_job_id,
    }


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
    init_reuse_group = init_parser.add_mutually_exclusive_group()
    init_reuse_group.add_argument(
        "--reuse-baseline",
        dest="reuse_baseline",
        action="store_true",
        help="Validate and reuse a shipped baseline when available.",
    )
    init_reuse_group.add_argument(
        "--no-reuse-baseline",
        dest="reuse_baseline",
        action="store_false",
        help="Skip baseline reuse and initialize a local-only overlay skeleton.",
    )
    init_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    init_parser.set_defaults(handler=handle_init, reuse_baseline=None)

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
        "--target",
        choices=("local", "baseline"),
        default="local",
        help="Write target for this run. Baseline writes require publish mode.",
    )
    index_parser.add_argument(
        "--publish-mode",
        choices=("publish", "build"),
        help="Required when writing baseline data.",
    )
    index_parser.add_argument(
        "--source",
        choices=("all", "code", "docs"),
        default="all",
        help="Source family to index.",
    )
    resume_group = index_parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        metavar="auto|JOB_ID",
        default="auto",
        help=(
            "Resume policy for interrupted index jobs. 'auto' is the default and resumes "
            "the newest compatible job; any other value is treated as an explicit job id."
        ),
    )
    resume_group.add_argument(
        "--restart",
        action="store_true",
        help=(
            "Start a fresh index job and supersede compatible unfinished jobs. "
            "Cannot be combined with --resume or --no-resume."
        ),
    )
    resume_group.add_argument(
        "--no-resume",
        action="store_true",
        help=(
            "Do not search for resumable jobs; create a fresh job. "
            "Cannot be combined with --resume or --restart."
        ),
    )
    index_parser.add_argument(
        "--job-id",
        help=(
            "Use a caller-supplied id for the new index job. Intended for CI/debug runs; "
            "do not combine with --resume JOB_ID."
        ),
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
        "--staging-jobs",
        action="store_true",
        help="Delete staging artifacts left by failed or superseded full-index jobs.",
    )
    clean_parser.add_argument(
        "--old-live-versions",
        action="store_true",
        help="Delete old published live metadata/vector versions.",
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

    rebuild_parser = subparsers.add_parser(
        "rebuild",
        parents=[common],
        help="Rebuild selected index artifacts.",
    )
    rebuild_parser.add_argument(
        "--vectors",
        action="store_true",
        help="Rebuild vector payloads from source documents.",
    )
    rebuild_parser.add_argument(
        "--target",
        choices=("local", "baseline"),
        default="local",
        help="Write target for rebuilt vectors.",
    )
    rebuild_parser.add_argument(
        "--publish-mode",
        choices=("publish", "build"),
        help="Required when rebuilding baseline vectors.",
    )
    rebuild_parser.add_argument(
        "--source",
        choices=("all", "docs"),
        default="docs",
        help="Source family used for vector rebuild.",
    )
    rebuild_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    rebuild_parser.set_defaults(handler=handle_rebuild)

    baseline_parser = subparsers.add_parser(
        "baseline",
        parents=[common],
        help="Baseline lifecycle operations.",
    )
    baseline_subparsers = baseline_parser.add_subparsers(
        dest="baseline_command",
        metavar="BASELINE_COMMAND",
    )

    baseline_validate_parser = baseline_subparsers.add_parser(
        "validate",
        parents=[common],
        help="Validate baseline manifest and baseline storage consistency.",
    )
    baseline_validate_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    baseline_validate_parser.set_defaults(handler=handle_baseline_validate)

    baseline_publish_parser = baseline_subparsers.add_parser(
        "publish",
        parents=[common],
        help="Build and publish a baseline snapshot manifest.",
    )
    baseline_publish_parser.add_argument(
        "--source",
        choices=("all", "code", "docs"),
        default="all",
        help="Source family to publish into baseline.",
    )
    baseline_publish_parser.add_argument(
        "--baseline-id",
        help="Baseline identifier written into baseline/manifest.json.",
    )
    baseline_publish_parser.add_argument(
        "--publish-mode",
        choices=("publish", "build"),
        required=True,
        help="Explicit publish/build mode required for baseline writes.",
    )
    baseline_publish_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    baseline_publish_parser.set_defaults(handler=handle_baseline_publish)

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
        help=(
            "Existing performance report JSON. When omitted, the command runs the performance gate."
        ),
    )
    baseline_save_parser.add_argument(
        "--stability-report",
        type=Path,
        help="Optional stability report JSON to attach to the baseline snapshot.",
    )
    baseline_save_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Optional explicit baseline snapshot path. "
            "Defaults to baseline artifacts/eval-baseline/<baseline-id>.json."
        ),
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
        help=(
            "Saved baseline snapshot path. "
            "Defaults to baseline artifacts/eval-baseline/latest.json."
        ),
    )
    baseline_compare_parser.add_argument(
        "--quality-report",
        type=Path,
        help=(
            "Existing current quality report JSON. When omitted, the command runs the quality gate."
        ),
    )
    baseline_compare_parser.add_argument(
        "--performance-report",
        type=Path,
        help=(
            "Existing current performance report JSON. "
            "When omitted, the command runs the performance gate."
        ),
    )
    baseline_compare_parser.add_argument(
        "--stability-report",
        type=Path,
        help="Optional current stability report JSON to include in the regression report context.",
    )
    baseline_compare_parser.add_argument(
        "--performance-exemption",
        action="append",
        default=[],
        metavar="PROBE_ID=REASON",
        help=(
            "Explicitly exempt one P95 regression above 20%%. Repeatable; "
            "the reason is written into the regression report."
        ),
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

    release_parser = subparsers.add_parser(
        "release",
        help="Run release-oriented checklist validations and emit machine-readable reports.",
    )
    release_subparsers = release_parser.add_subparsers(
        dest="release_command",
        metavar="RELEASE_COMMAND",
    )

    release_checklist_parser = release_subparsers.add_parser(
        "checklist",
        parents=[common],
        help="Run the E7-07 release checklist against the current baseline and gate artifacts.",
    )
    release_checklist_parser.add_argument(
        "--quality-report",
        type=Path,
        help="Existing quality gate report JSON. When omitted, the command runs the quality gate.",
    )
    release_checklist_parser.add_argument(
        "--performance-report",
        type=Path,
        help=(
            "Existing performance gate report JSON. When omitted, the command runs the "
            "performance gate."
        ),
    )
    release_checklist_parser.add_argument(
        "--stability-report",
        type=Path,
        help=(
            "Existing stability gate report JSON. When omitted, the command runs the "
            "stability gate with the configured probe window."
        ),
    )
    release_checklist_parser.add_argument(
        "--readme",
        type=Path,
        help="README file to verify for the documented release command set.",
    )
    release_checklist_parser.add_argument(
        "--remote-config",
        type=Path,
        help="remote_shared example config to validate as part of the release checklist.",
    )
    release_checklist_parser.add_argument(
        "--report",
        type=Path,
        help="Optional output path for the checklist JSON report.",
    )
    release_checklist_parser.add_argument(
        "--soak-seconds",
        type=int,
        default=60,
        help="Timed soak window used when the checklist needs to run the stability gate.",
    )
    release_checklist_parser.add_argument(
        "--mixed-query-count",
        type=int,
        default=500,
        help="Mixed-query count used when the checklist needs to run the stability gate.",
    )
    release_checklist_parser.add_argument(
        "--readonly-workers",
        type=int,
        default=8,
        help="Readonly worker count used when the checklist needs to run the stability gate.",
    )
    release_checklist_parser.add_argument(
        "--readonly-queries",
        type=int,
        default=64,
        help="Readonly query count used when the checklist needs to run the stability gate.",
    )
    release_checklist_parser.add_argument(
        "--readonly-timeout-seconds",
        type=float,
        default=5.0,
        help="Per-query timeout used when the checklist needs to run the stability gate.",
    )
    release_checklist_parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="text",
        help="Output format.",
    )
    release_checklist_parser.set_defaults(handler=handle_release_checklist)

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

    resolved = resolve_from_args(args, command_overrides=init_overrides(args))
    result = initialize_workdir(resolved, force=bool(args.force))
    layout = result.layout

    summary = config_summary(resolved)
    debug_progress("init: collect quick index status")
    index_status, index_warnings = collect_index_status(resolved, validation_mode="quick")
    baseline_reuse, baseline_warnings = collect_baseline_reuse_status(
        resolved,
        layout=layout,
        storage_validation=index_status["storage_validation"],
    )
    profile_status, profile_warnings = collect_profile_status(resolved)
    warnings = collect_cli_warnings(
        [warning.to_dict() for warning in result.warnings],
        baseline_warnings,
        profile_warnings,
        index_warnings,
    )
    payload = {
        "command": "init",
        "status": "ok",
        "created": [str(path) for path in result.created],
        "warnings": warnings,
        "baseline_manifest": result.baseline_manifest.to_dict(),
        "baseline_reuse": baseline_reuse,
        "profile": profile_status,
        "index": index_status,
        "config": summary,
    }
    if args.format == "json":
        print_json(payload)
    else:
        print(f"Initialized Active Knowledge workdir: {layout.workdir}")
        print(f"Local config: {layout.local_config_path}")
        print(f"Workspace: {summary['workspace_root']}")
        print(format_baseline_reuse_line(baseline_reuse))
        print(format_profile_line(profile_status))
        print(format_index_line(index_status))
        print("Next: active-kb validate --format json")
        print("Next: active-kb status --format json")
        for warning in warnings:
            print(f"Warning [{warning['code']}]: {warning['message']}")
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
    """Execute one index run."""

    resume_policy = resolve_index_resume_policy(args)
    resolved = resolve_from_args(args, command_overrides=index_overrides(args))
    summary = config_summary(resolved)

    mode = "full" if args.full else "incremental"
    target = str(args.target)
    publish_mode = getattr(args, "publish_mode", None)
    blocked = validate_baseline_write_intent(
        args,
        tool_name="index",
        target=target,
        mode=mode,
        publish_mode=publish_mode,
    )
    if blocked is not None:
        return blocked
    output_mode = resolve_index_progress_output_mode(output_format=args.format)
    progress_callback = noop_progress_callback
    reporter = None
    if output_mode != "none":
        progress_stream = sys.stderr if args.format == "json" else sys.stdout
        reporter = create_index_progress_reporter(
            output_mode=output_mode,
            stream=progress_stream,
        )
        progress_callback = reporter.handle

    try:
        with _translate_sigterm_to_keyboard_interrupt():
            if reporter is None:
                payload = _run_index_command(
                    resolved,
                    summary=summary,
                    mode=mode,
                    target=target,
                    source=str(args.source),
                    resume_policy=resume_policy,
                    progress_callback=progress_callback,
                )
            else:
                with reporter:
                    payload = _run_index_command(
                        resolved,
                        summary=summary,
                        mode=mode,
                        target=target,
                        source=str(args.source),
                        resume_policy=resume_policy,
                        progress_callback=progress_callback,
                    )
    except KeyboardInterrupt as exc:
        if reporter is not None:
            reporter.emit_interrupt_summary()
        interrupted_payload: Mapping[str, object] | None = (
            exc.payload if isinstance(exc, IndexCommandInterrupted) else None
        )
        if args.format == "json":
            print_json(
                interrupted_payload
                or {
                    "command": "index",
                    "status": "interrupted",
                    "message": "Indexing was interrupted before completion.",
                    "job": build_index_job_payload(
                        resume_policy=resume_policy,
                        status="interrupted",
                    ),
                }
            )
        elif interrupted_payload is not None:
            job = interrupted_payload.get("job")
            if isinstance(job, Mapping) and job.get("job_id"):
                print(f"Job id: {job['job_id']}")
                print(f"Resumed: {job.get('resumed', False)}")
        return 130
    except JobLockConflictError as exc:
        return emit_command_blocked(
            args,
            tool_name="index",
            code="index.job_lock_active",
            message=str(exc),
            suggested_action=(
                "Wait for the running index job to finish, or retry after the lock expires."
            ),
        )

    if args.format == "json":
        print_json(payload)
    else:
        print(f"Index completed: {payload['mode']} ({payload['source']})")
        print(f"Target: {payload['target']}")
        print(f"Workdir: {summary['workdir']}")
        result_payload = payload.get("result")
        result_status = (
            result_payload.get("result_status", "ready")
            if isinstance(result_payload, Mapping)
            else "ready"
        )
        print(f"Result status: {result_status}")
        job = payload.get("job")
        if isinstance(job, dict):
            print(f"Resume policy: {job['resume_policy']['mode']}")
            if job.get("job_id"):
                print(f"Job id: {job['job_id']}")
                print(f"Resumed: {job.get('resumed', False)}")
    return 0


def _run_index_command(
    resolved: ResolvedConfig,
    *,
    summary: dict[str, str | int | bool | list[str] | dict[str, Any]],
    mode: str,
    target: str,
    source: str,
    resume_policy: Mapping[str, object],
    progress_callback: IndexProgressCallback,
) -> dict[str, object]:
    """Execute the index command and return the final payload."""

    if mode == "incremental" and target == "local":
        pipeline = IncrementalIndexPipeline(resolved.model, cwd=Path.cwd())
        plan = pipeline.plan(
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source=cast(Any, source),
            progress_callback=progress_callback,
        )
        job_context = prepare_incremental_index_job(
            resolved,
            plan=plan,
            mode=mode,
            target=target,
            source=source,
            resume_policy=resume_policy,
        )
        job_progress_callback = build_index_job_progress_callback(
            job_context,
            progress_callback=progress_callback,
        )
        index_started_at = time.perf_counter()
        try:
            run_signature = make_index_plan_signature(
                plan,
                config=resolved.model,
                mode=mode,
                target=target,
            )
            result = pipeline.run(
                snapshot_id=CURRENT_SNAPSHOT_ID,
                source=cast(Any, source),
                progress_callback=job_progress_callback,
                plan=plan,
                run_context=IndexRunContext(
                    job_store=job_context.store,
                    job_id=job_context.job.job_id,
                    resume_policy=resume_policy,
                    plan_signature=run_signature,
                ),
            )
        except KeyboardInterrupt:
            mark_index_job_interrupted(job_context)
            raise IndexCommandInterrupted(
                {
                    "command": "index",
                    "status": "interrupted",
                    "message": "Indexing was interrupted before completion.",
                    "job": build_index_job_payload(
                        resume_policy=resume_policy,
                        status="interrupted",
                        result=None,
                        mode=mode,
                        target=target,
                        source=source,
                        job_context=job_context,
                    ),
                    "config": summary,
                }
            ) from None
        except Exception:
            mark_index_job_failed(job_context, error_summary="index command failed")
            raise
        else:
            finalize_index_job(job_context, result_status=result.result_status)
            record_index_observability(
                resolved,
                result=result,
                duration_seconds=max(time.perf_counter() - index_started_at, 0.0),
                job_id=job_context.job.job_id,
            )
        finally:
            job_context.store.release_lock(INDEX_JOB_LOCK_ID, owner_job_id=job_context.job.job_id)

        return {
            "command": "index",
            "status": "ok",
            "target": target,
            "source": source,
            "mode": mode,
            "result": result.to_dict(),
            "job": build_index_job_payload(
                resume_policy=resume_policy,
                status=result.result_status,
                result=result,
                config=resolved.model,
                mode=mode,
                target=target,
                source=source,
                job_context=job_context,
            ),
            "config": summary,
        }

    job_context = prepare_nonresumable_index_job(
        resolved,
        mode=mode,
        target=target,
        source=source,
        resume_policy=resume_policy,
    )
    job_progress_callback = build_index_job_progress_callback(
        job_context,
        progress_callback=progress_callback,
    )
    full_index_started_at = time.perf_counter()
    try:
        full_result = run_full_index(
            resolved,
            target=target,
            source=source,
            operation_mode="baseline_publish" if target == "baseline" else "normal",
            progress_callback=job_progress_callback,
            staging_storage=cast(
                Mapping[str, object] | None,
                job_context.job.metadata.get("staging_storage"),
            ),
            job_id=job_context.job.job_id,
        )
    except KeyboardInterrupt:
        mark_index_job_interrupted(job_context)
        raise IndexCommandInterrupted(
            {
                "command": "index",
                "status": "interrupted",
                "message": "Indexing was interrupted before completion.",
                "job": build_index_job_payload(
                    resume_policy=resume_policy,
                    status="interrupted",
                    mode=mode,
                    target=target,
                    source=source,
                    job_context=job_context,
                ),
                "config": summary,
            }
        ) from None
    except Exception:
        mark_index_job_failed(job_context, error_summary="index command failed")
        raise
    else:
        finalize_index_job(
            job_context,
            result_status=str(full_result.get("result_status", "ready")),
        )
        record_index_observability(
            resolved,
            result=full_result,
            duration_seconds=max(time.perf_counter() - full_index_started_at, 0.0),
            job_id=job_context.job.job_id,
        )
    finally:
        job_context.store.release_lock(INDEX_JOB_LOCK_ID, owner_job_id=job_context.job.job_id)

    return {
        "command": "index",
        "status": "ok",
        "target": target,
        "source": source,
        "mode": mode,
        "result": full_result,
        "job": build_index_job_payload(
            resume_policy=resume_policy,
            status=str(full_result.get("result_status", "ready")),
            result=full_result,
            config=resolved.model,
            mode=mode,
            target=target,
            source=source,
            job_context=job_context,
        ),
        "config": summary,
    }


def build_index_job_payload(
    *,
    resume_policy: Mapping[str, object],
    status: str,
    result: IncrementalIndexResult | Mapping[str, object] | None = None,
    config: Any | None = None,
    mode: str | None = None,
    target: str | None = None,
    source: str | None = None,
    job_context: IndexJobContext | None = None,
) -> dict[str, object]:
    """Build the stable AR0-03 final JSON job contract."""

    if job_context is not None:
        _sync_index_job_context_counts(job_context)

    plan = getattr(result, "plan", None)
    plan_signature: dict[str, object] | None = (
        None if job_context is None else job_context.plan_signature
    )
    tasks_total: int | None = None if job_context is None else job_context.tasks_total
    if plan is not None and config is not None:
        signature = make_index_plan_signature(
            plan,
            config=config,
            mode=mode,
            target=target,
        )
        plan_signature = signature.to_dict()
        tasks_total = len(make_index_task_list(plan))

    return {
        "schema_version": _INDEX_JOB_CONTRACT_SCHEMA_VERSION,
        "job_id": (
            resume_policy.get("planned_job_id")
            if job_context is None
            else job_context.job.job_id
        ),
        "status": status,
        "resumed": False if job_context is None else job_context.resumed,
        "resume_policy": cast(
            StorageMetadata,
            {str(key): value for key, value in resume_policy.items()},
        ),
        "mode": mode,
        "target": target,
        "source": source,
        "plan_signature": plan_signature,
        "tasks_total": tasks_total,
        "tasks_applied": None if job_context is None else job_context.tasks_applied,
        "tasks_skipped": None if job_context is None else job_context.tasks_skipped,
        "tasks_failed": None if job_context is None else job_context.tasks_failed,
        "staging_storage": (
            None if job_context is None else job_context.job.metadata.get("staging_storage")
        ),
    }


def prepare_incremental_index_job(
    resolved: ResolvedConfig,
    *,
    plan: IncrementalIndexPlan,
    mode: str,
    target: str,
    source: str,
    resume_policy: Mapping[str, object],
) -> IndexJobContext:
    """Create or resume the persisted job for one incremental local index run."""

    paths = configured_sqlite_paths(resolved.model, cwd=Path.cwd())
    migrate_sqlite_store(paths["jobs"], target="jobs")
    store = SQLiteJobStore(paths["jobs"])
    signature = make_index_plan_signature(
        plan,
        config=resolved.model,
        mode=mode,
        target=target,
    )
    tasks_total = len(make_index_task_list(plan))
    metadata_match = {
        "requested_mode": mode,
        "requested_target": target,
        "requested_source": source,
    }
    metadata = build_index_job_metadata(
        resume_policy=resume_policy,
        mode=mode,
        target=target,
        source=source,
        plan_signature=signature.to_dict(),
        tasks_total=tasks_total,
    )
    policy_mode = str(resume_policy.get("mode", "auto"))
    resumed = False
    job: JobRecord | None = None
    planned_job_id = _optional_nonempty_text(resume_policy.get("planned_job_id"))

    if policy_mode == "auto":
        job = store.find_resumable_index_job(
            plan_signature=signature.digest,
            write_target=storage_write_target_for_cli_target(target),
            snapshot_id=plan.snapshot_id,
            profile_id=ALL_SCOPE,
            metadata_match=metadata_match,
        )
        if job is not None:
            resumed = True
    elif policy_mode == "job_id":
        resume_job_id = _optional_nonempty_text(resume_policy.get("resume_job_id"))
        if resume_job_id is None:
            raise ConfigError("--resume JOB_ID requires a non-empty job id.")
        job = load_explicit_resumable_index_job(
            store,
            job_id=resume_job_id,
            plan_signature_digest=signature.digest,
            metadata_match=metadata_match,
        )
        resumed = True
    elif policy_mode == "restart":
        job = store.find_resumable_index_job(
            plan_signature=signature.digest,
            write_target=storage_write_target_for_cli_target(target),
            snapshot_id=plan.snapshot_id,
            profile_id=ALL_SCOPE,
            metadata_match=metadata_match,
        )
        if job is not None:
            store.supersede_job(
                job.job_id,
                superseded_by_job_id=planned_job_id,
                reason="restart",
            )
        job = None
    elif policy_mode != "disabled":
        raise ConfigError(f"unsupported index resume policy: {policy_mode}")

    if job is not None and job.status in {"failed", "partial_ready"}:
        job = store.retry_job(job.job_id)
    if job is not None:
        resume_state = store.resume_job(job.job_id, increment_resume_count=resumed)
        job = resume_state.job
        metadata = {
            **metadata,
            "resume_count": job.metadata.get("resume_count", 0),
            "resumed_from_status": job.status,
        }
        job = store.transition_or_update_running_metadata(
            job.job_id,
            metadata_update=metadata,
        )
    else:
        job = store.create_job(
            job_id=planned_job_id,
            job_type="index",
            write_target=storage_write_target_for_cli_target(target),
            snapshot_id=plan.snapshot_id,
            profile_id=ALL_SCOPE,
            metadata=metadata,
        )

    store.acquire_lock(
        INDEX_JOB_LOCK_ID,
        owner_job_id=job.job_id,
        ttl_seconds=_INDEX_JOB_LOCK_TTL_SECONDS,
        metadata={
            "job_type": "index",
            "requested_mode": mode,
            "requested_target": target,
            "requested_source": source,
            "plan_signature": signature.digest,
        },
    )
    job = store.transition_or_update_running_metadata(
        job.job_id,
        "discovering",
        metadata_update={
            "execution_state": "running",
            "started_at": utc_timestamp(),
            "last_phase": "plan",
            "tasks_total": tasks_total,
            "tasks_applied": 0,
            "tasks_skipped": 0,
            "tasks_failed": 0,
        },
    )
    return IndexJobContext(
        store=store,
        job=job,
        resumed=resumed,
        plan_signature=signature.to_dict(),
        plan_signature_digest=signature.digest,
        tasks_total=tasks_total,
        tasks_applied=0,
        tasks_skipped=0,
        tasks_failed=0,
    )


def prepare_nonresumable_index_job(
    resolved: ResolvedConfig,
    *,
    mode: str,
    target: str,
    source: str,
    resume_policy: Mapping[str, object],
) -> IndexJobContext:
    """Create a persisted job for an index path that has no resume plan yet."""

    if str(resume_policy.get("mode", "auto")) == "job_id":
        raise ConfigError("--resume JOB_ID is only supported for incremental local indexing.")
    paths = configured_sqlite_paths(resolved.model, cwd=Path.cwd())
    migrate_sqlite_store(paths["jobs"], target="jobs")
    store = SQLiteJobStore(paths["jobs"])
    planned_job_id = _optional_nonempty_text(resume_policy.get("planned_job_id"))
    write_target = storage_write_target_for_cli_target(target)
    job = store.create_job(
        job_id=planned_job_id,
        job_type="index",
        write_target=write_target,
        snapshot_id=CURRENT_SNAPSHOT_ID,
        profile_id=ALL_SCOPE,
        metadata=build_index_job_metadata(
            resume_policy=resume_policy,
            mode=mode,
            target=target,
            source=source,
            plan_signature=None,
            tasks_total=None,
        ),
    )
    staging_storage = None
    if mode == "full":
        staging_storage = resolve_staging_storage_paths(
            resolved.model,
            cwd=Path.cwd(),
            target=write_target,
            job_id=job.job_id,
        ).to_dict()
    store.acquire_lock(
        INDEX_JOB_LOCK_ID,
        owner_job_id=job.job_id,
        ttl_seconds=_INDEX_JOB_LOCK_TTL_SECONDS,
        metadata={
            "job_type": "index",
            "requested_mode": mode,
            "requested_target": target,
            "requested_source": source,
        },
    )
    job = store.transition_or_update_running_metadata(
        job.job_id,
        "discovering",
        metadata_update={
            "execution_state": "running",
            "started_at": utc_timestamp(),
            "last_phase": "discovering",
            "tasks_total": None,
            "tasks_applied": None,
            "tasks_skipped": None,
            "tasks_failed": None,
            **({} if staging_storage is None else {"staging_storage": staging_storage}),
        },
    )
    return IndexJobContext(
        store=store,
        job=job,
        resumed=False,
        plan_signature=None,
        plan_signature_digest=None,
        tasks_total=None,
        tasks_applied=None,
        tasks_skipped=None,
        tasks_failed=None,
    )


def build_index_job_metadata(
    *,
    resume_policy: Mapping[str, object],
    mode: str,
    target: str,
    source: str,
    plan_signature: Mapping[str, object] | None,
    tasks_total: int | None,
) -> StorageMetadata:
    """Build persisted metadata for a CLI-owned index job."""

    metadata: StorageMetadata = {
        "schema_version": _INDEX_JOB_CONTRACT_SCHEMA_VERSION,
        "execution_state": "running",
        "requested_mode": mode,
        "requested_target": target,
        "requested_source": source,
        "resume_policy": cast(
            StorageMetadata,
            {str(key): value for key, value in resume_policy.items()},
        ),
        "tasks_total": tasks_total,
        "tasks_applied": 0,
        "tasks_skipped": 0,
        "tasks_failed": 0,
    }
    if plan_signature is not None:
        metadata["plan_signature"] = str(plan_signature["digest"])
        metadata["plan_signature_payload"] = cast(
            StorageMetadata,
            {str(key): value for key, value in plan_signature.items()},
        )
    return metadata


def load_explicit_resumable_index_job(
    store: SQLiteJobStore,
    *,
    job_id: str,
    plan_signature_digest: str,
    metadata_match: Mapping[str, object],
) -> JobRecord:
    """Load and validate an explicitly requested resumable job."""

    job = store.get_job(job_id)
    if job is None:
        raise ConfigError(f"index job {job_id!r} does not exist.")
    if job.job_type != "index":
        raise ConfigError(f"job {job_id!r} is not an index job.")
    if job.status not in RESUMABLE_INDEX_JOB_STATUSES:
        raise ConfigError(f"index job {job_id!r} is not resumable from status {job.status!r}.")
    if str(job.metadata.get("plan_signature", "")) != plan_signature_digest:
        raise ConfigError(
            f"index job {job_id!r} is not compatible with the current index plan."
        )
    for key, expected in metadata_match.items():
        if job.metadata.get(key) != expected:
            raise ConfigError(
                f"index job {job_id!r} does not match current {key}={expected!r}."
            )
    if job.metadata.get("execution_state") == "superseded":
        raise ConfigError(f"index job {job_id!r} was superseded and cannot be resumed.")
    if bool(job.metadata.get("cancelled")):
        raise ConfigError(f"index job {job_id!r} was cancelled and cannot be resumed.")
    lock = store.get_lock(INDEX_JOB_LOCK_ID)
    if lock is not None and not lock_expired(lock.expires_at, datetime.now(UTC)):
        raise JobLockConflictError(
            f"index lock is still active for job {lock.owner_job_id!r} "
            f"until {lock.expires_at or 'never'}"
        )
    return job


def build_index_job_progress_callback(
    job_context: IndexJobContext,
    *,
    progress_callback: IndexProgressCallback,
) -> IndexProgressCallback:
    """Wrap progress rendering with light job metadata and lock heartbeats."""

    last_heartbeat_at = 0.0

    def handle(event: IndexProgressEvent) -> None:
        nonlocal last_heartbeat_at
        progress_callback(event)
        now = time.monotonic()
        phase = str(event.phase)
        current_path = event.current_path
        if now - last_heartbeat_at < 5 and phase != "done":
            return
        last_heartbeat_at = now
        metadata_update: StorageMetadata = {
            "execution_state": "running",
            "last_phase": phase,
            "last_message": event.message,
            "global_total": event.global_total,
            "global_done": event.global_done,
        }
        if current_path is not None:
            metadata_update["last_path"] = current_path
        try:
            job_context.store.transition_or_update_running_metadata(
                job_context.job.job_id,
                metadata_update=metadata_update,
            )
            job_context.store.heartbeat_lock(
                INDEX_JOB_LOCK_ID,
                owner_job_id=job_context.job.job_id,
                ttl_seconds=_INDEX_JOB_LOCK_TTL_SECONDS,
                metadata_update=metadata_update,
            )
        except (JobLockConflictError, JobStateTransitionError, KeyError):
            return

    return handle


def finalize_index_job(job_context: IndexJobContext, *, result_status: str) -> None:
    """Transition a CLI index job to ready or partial_ready and update task counts."""

    _advance_index_job_to_reporting(job_context)
    _sync_index_job_context_counts(job_context)
    terminal_status = "partial_ready" if result_status == "partial_ready" else "ready"
    tasks_failed = 0 if terminal_status == "ready" else job_context.tasks_failed
    metadata_update = {
        "execution_state": "complete",
        "finished_at": utc_timestamp(),
        "tasks_total": job_context.tasks_total,
        "tasks_applied": job_context.tasks_applied,
        "tasks_skipped": job_context.tasks_skipped,
        "tasks_failed": tasks_failed,
    }
    job_context.job = job_context.store.transition_job(
        job_context.job.job_id,
        cast(Any, terminal_status),
        metadata_update=metadata_update,
    )
    job_context.tasks_failed = tasks_failed


def mark_index_job_interrupted(job_context: IndexJobContext) -> None:
    """Mark a CLI index job as interrupted and release its lock."""

    try:
        mark_index_job_failed(
            job_context,
            error_summary="interrupted",
            execution_state="interrupted",
        )
    finally:
        job_context.store.release_lock(
            INDEX_JOB_LOCK_ID,
            owner_job_id=job_context.job.job_id,
        )


def mark_index_job_failed(
    job_context: IndexJobContext,
    *,
    error_summary: str,
    execution_state: str = "failed",
) -> None:
    """Best-effort transition of a CLI index job to failed."""

    job = job_context.store.get_job(job_context.job.job_id)
    if job is None or job.status in {"ready", "failed", "partial_ready"}:
        return
    job_context.job = job_context.store.transition_job(
        job.job_id,
        "failed",
        error_summary=error_summary,
        metadata_update={
            "execution_state": execution_state,
            "finished_at": utc_timestamp(),
            "tasks_total": job_context.tasks_total,
            "tasks_applied": job_context.tasks_applied,
            "tasks_skipped": job_context.tasks_skipped,
            "tasks_failed": job_context.tasks_failed,
        },
    )


def _advance_index_job_to_reporting(job_context: IndexJobContext) -> None:
    order = ("discovering", *RUNNING_JOB_STATUSES[1:])
    job = job_context.store.get_job(job_context.job.job_id)
    if job is None:
        raise KeyError(f"job {job_context.job.job_id!r} does not exist")
    if job.status == "reporting":
        return
    if job.status == "pending":
        remaining = order
    elif job.status in order:
        remaining = order[order.index(job.status) + 1 :]
    else:
        raise JobStateTransitionError(f"job {job.job_id!r} cannot report from {job.status!r}")
    for status in remaining:
        job_context.job = job_context.store.transition_or_update_running_metadata(
            job_context.job.job_id,
            cast(Any, status),
            metadata_update={"last_phase": status},
        )


def _sync_index_job_context_counts(job_context: IndexJobContext) -> None:
    job = job_context.store.get_job(job_context.job.job_id)
    if job is None:
        return
    job_context.job = job
    metadata = job.metadata
    job_context.tasks_total = _metadata_int(metadata.get("tasks_total"), job_context.tasks_total)
    job_context.tasks_applied = _metadata_int(
        metadata.get("tasks_applied"),
        job_context.tasks_applied,
    )
    job_context.tasks_skipped = _metadata_int(
        metadata.get("tasks_skipped"),
        job_context.tasks_skipped,
    )
    job_context.tasks_failed = _metadata_int(
        metadata.get("tasks_failed"),
        job_context.tasks_failed,
    )


def _metadata_int(value: object, default: int | None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return default


def _optional_nonempty_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def handle_rebuild(args: argparse.Namespace) -> int:
    """Execute rebuild operations for selected artifacts."""

    if not args.vectors:
        return emit_command_blocked(
            args,
            tool_name="rebuild",
            code="rebuild.no_target_selected",
            message="No rebuild target was selected.",
            suggested_action="Pass --vectors to rebuild vector payloads.",
        )

    resolved = resolve_from_args(args)
    target = str(args.target)
    blocked = validate_baseline_write_intent(
        args,
        tool_name="rebuild",
        target=target,
        mode="full",
        publish_mode=getattr(args, "publish_mode", None),
    )
    if blocked is not None:
        return blocked

    result = rebuild_vectors(
        resolved,
        target=target,
        source=args.source,
        operation_mode="baseline_publish" if target == "baseline" else "normal",
    )
    payload = {
        "command": "rebuild",
        "status": "ok",
        "target": target,
        "source": args.source,
        "rebuild": result,
        "config": config_summary(resolved),
    }
    if args.format == "json":
        print_json(payload)
    else:
        print("Rebuild completed")
        print(f"Target: {target}")
        print(f"Vectors rebuilt: {result['vectors_rebuilt']}")
    return 0


def handle_baseline_validate(args: argparse.Namespace) -> int:
    """Validate baseline manifest and baseline storage consistency."""

    resolved = resolve_from_args(args)
    layout = layout_from_config(resolved)
    manifest_status, manifest_warning = inspect_baseline_manifest(layout.baseline_manifest_path)
    storage_report = validate_storage_consistency(resolved.model, cwd=Path.cwd()).to_dict()
    checks = cast(list[dict[str, object]], storage_report["checks"])
    baseline_root = str(layout.baseline_dir)
    baseline_checks = [
        check
        for check in checks
        if str(check.get("check_code", "")).startswith("baseline.")
        or any(str(item).startswith(baseline_root) for item in check.get("affected_objects", []))
    ]
    payload = {
        "command": "baseline validate",
        "status": "ok" if manifest_status.exists and manifest_status.readable else "fail",
        "manifest": manifest_status.to_dict(),
        "storage_report": storage_report,
        "baseline_checks": baseline_checks,
        "warnings": [] if manifest_warning is None else [manifest_warning.to_dict()],
    }
    if args.format == "json":
        print_json(payload)
    else:
        print("Baseline validate")
        print(f"Manifest: {manifest_status.path} ({exists_label(manifest_status.path)})")
        print(f"Storage status: {storage_report['status']}")
        for check in baseline_checks:
            print(f"- {check['severity']}: {check['check_code']} - {check['message']}")
        if manifest_warning is not None:
            print(f"Warning [{manifest_warning.code}]: {manifest_warning.message}")
    if not manifest_status.exists or not manifest_status.readable:
        return 1
    return 1 if str(storage_report["status"]) == "blocked" else 0


def handle_baseline_publish(args: argparse.Namespace) -> int:
    """Publish one baseline build and write baseline manifest."""

    resolved = resolve_from_args(resolved_args_for_baseline_publish(args))
    result = run_full_index(
        resolved,
        target="baseline",
        source=args.source,
        operation_mode="baseline_publish",
    )
    layout = layout_from_config(resolved)
    baseline_id = str(args.baseline_id or _default_baseline_id())
    manifest_payload = build_baseline_manifest_payload(
        resolved,
        layout=layout,
        baseline_id=baseline_id,
        source=args.source,
        publish_mode=args.publish_mode,
        result=result,
    )
    layout.baseline_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    layout.baseline_manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    payload = {
        "command": "baseline publish",
        "status": "ok",
        "baseline_id": baseline_id,
        "manifest_path": str(layout.baseline_manifest_path),
        "result": result,
    }
    if args.format == "json":
        print_json(payload)
    else:
        print(f"Baseline published: {baseline_id}")
        print(f"Manifest: {layout.baseline_manifest_path}")
    return 0


def handle_status(args: argparse.Namespace) -> int:
    """Show local state and config summary."""

    resolved = resolve_from_args(args)
    layout = workdir_layout(resolved)
    summary = config_summary(resolved)
    debug_progress("status: collect quick index status")
    index_status, index_warnings = collect_index_status(resolved, validation_mode="quick")
    baseline_reuse, baseline_warnings = collect_baseline_reuse_status(
        resolved,
        layout=layout,
        storage_validation=index_status["storage_validation"],
    )
    profile_status, profile_warnings = collect_profile_status(resolved)
    observability = collect_observability_status(
        resolved,
        layout=layout,
        index_status=index_status,
    )
    payload = {
        "command": "status",
        "status": "ok",
        "config": summary,
        "paths": path_status(layout, resolved),
        "baseline_reuse": baseline_reuse,
        "profile": profile_status,
        "index": index_status,
        "observability": observability,
        "warnings": collect_cli_warnings(
            baseline_warnings,
            profile_warnings,
            index_warnings,
        ),
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
        print(format_baseline_reuse_line(baseline_reuse))
        print(format_profile_line(profile_status))
        print(format_index_line(index_status))
        print(format_query_health_line(observability))
        print(format_recent_index_health_line(observability))
        for warning in payload["warnings"]:
            print(f"Warning [{warning['code']}]: {warning['message']}")
    return 0


def handle_validate(args: argparse.Namespace) -> int:
    """Validate basic setup readiness."""

    resolved = resolve_from_args(args)
    layout = workdir_layout(resolved)
    checks = validation_checks(layout, resolved, strict=bool(args.strict))
    debug_progress("validate: collect full index status")
    index_status, index_warnings = collect_index_status(
        resolved,
        validation_mode="full",
        emit_progress=True,
    )
    baseline_reuse, baseline_warnings = collect_baseline_reuse_status(
        resolved,
        layout=layout,
        storage_validation=index_status["storage_validation"],
    )
    profile_status, profile_warnings = collect_profile_status(resolved)
    storage_report = index_status["storage_validation"]
    errors = [check for check in checks if check["level"] == "error"]
    payload = {
        "schema_version": "active_kb_validate.v1",
        "command": "validate",
        "status": "error" if errors or str(storage_report["status"]) == "blocked" else "ok",
        "checks": checks,
        "storage_report": storage_report,
        "baseline_reuse": baseline_reuse,
        "profile": profile_status,
        "index": index_status,
        "warnings": collect_cli_warnings(
            baseline_warnings,
            profile_warnings,
            index_warnings,
        ),
        "config": config_summary(resolved),
    }

    if args.format == "json":
        print_json(payload)
    else:
        print("Validation checks")
        for check in checks:
            print(f"- {check['level']}: {check['name']} - {check['message']}")
        print(format_baseline_reuse_line(baseline_reuse))
        print(format_profile_line(profile_status))
        print(format_index_line(index_status))
        print(f"Storage consistency: {storage_report['status']}")
        for storage_check in storage_report["checks"]:
            print(
                f"- {storage_check['severity']}: "
                f"{storage_check['check_code']} - {storage_check['message']}"
            )
        for warning in payload["warnings"]:
            print(f"Warning [{warning['code']}]: {warning['message']}")
    return 1 if errors or str(storage_report["status"]) == "blocked" else 0


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
        clean_staging_jobs=bool(args.staging_jobs),
        live_versions_keep=int(args.keep) if args.old_live_versions else None,
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
        print(f"Deleted staging artifacts: {report.deleted_staging_artifacts}")
        print(f"Deleted live versions: {report.deleted_live_versions}")
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
        print(f"Cases: {report.metrics['passed_cases']}/{report.metrics['executed_cases']} passed")
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
        performance_exemptions=parse_performance_exemptions(args.performance_exemption),
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


def handle_release_checklist(args: argparse.Namespace) -> int:
    """Run the E7-07 release checklist and emit a machine-readable report."""

    resolved = resolve_from_args(args)
    layout = layout_from_config(resolved)
    repo_root = discover_release_repo_root(Path.cwd())
    readme_path = Path(args.readme) if args.readme is not None else default_release_readme_path(repo_root)
    remote_config_path = (
        Path(args.remote_config)
        if args.remote_config is not None
        else default_release_remote_config_path(repo_root)
    )

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
        load_eval_report_payload(Path(args.stability_report))
        if args.stability_report is not None
        else _run_stability_gate_report(resolved, args)
    )

    checks = [
        build_manifest_check(layout.baseline_manifest_path),
        build_quality_gate_check(quality_report),
        build_performance_gate_check(performance_report),
        build_stability_gate_check(stability_report),
        build_tracked_local_release_check(layout.local_dir, cwd=Path.cwd()),
        build_remote_config_release_check(remote_config_path),
        build_readme_command_check(readme_path),
    ]
    overall_status = summarize_release_checklist_status(checks)
    payload = {
        "schema_version": "release_checklist.v1",
        "command": "release checklist",
        "status": overall_status,
        "baseline_manifest": str(layout.baseline_manifest_path),
        "reports": {
            "quality": quality_report.to_dict(),
            "performance": performance_report.to_dict(),
            "stability": stability_report.to_dict(),
        },
        "checks": checks,
        "warnings": tuple(
            {
                "code": str(check["check_id"]),
                "message": str(check["message"]),
                "details": dict(check.get("details", {})),
            }
            for check in checks
            if check["status"] != "pass"
        ),
        "config": config_summary(resolved),
    }
    if args.report is not None:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        payload["artifacts"] = (str(report_path),)
    if args.format == "json":
        print_json(payload)
    else:
        print("Release checklist")
        print(f"Status: {overall_status}")
        for check in checks:
            print(f"- {check['status']}: {check['check_id']} - {check['message']}")
    return 0 if overall_status == "pass" else 1


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
    if str(args.gate) == "reproducibility":
        return Path("eval") / "reproducibility_cases.yaml"
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


def _run_stability_gate_report(
    resolved: ResolvedConfig,
    args: argparse.Namespace,
) -> EvalRunReport:
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
    return runner.run(
        Path("eval") / "stability_cases.yaml",
        gate_id="stability",
        suite_kind="stability",
    )


def _baseline_snapshot_dir(resolved: ResolvedConfig) -> Path:
    return resolve_runtime_path(resolved.model.storage.artifacts_root, Path.cwd()) / "eval-baseline"


def parse_performance_exemptions(values: Sequence[str]) -> dict[str, str]:
    """Parse explicit P95 regression exemptions from CLI values."""

    exemptions: dict[str, str] = {}
    for value in values:
        probe_id, separator, reason = value.partition("=")
        probe_id = probe_id.strip()
        reason = reason.strip()
        if separator != "=" or not probe_id or not reason:
            raise ConfigError(
                "--performance-exemption must use PROBE_ID=REASON with a non-empty reason"
            )
        if probe_id not in PERFORMANCE_GATE_THRESHOLDS:
            valid = ", ".join(sorted(PERFORMANCE_GATE_THRESHOLDS))
            raise ConfigError(
                f"unknown performance exemption probe_id '{probe_id}'. Valid probes: {valid}"
            )
        exemptions[probe_id] = reason
    return exemptions


def _default_baseline_id() -> str:
    return "release-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_baseline_manifest_payload(
    resolved: ResolvedConfig,
    *,
    layout: WorkdirLayout,
    baseline_id: str,
    source: str,
    publish_mode: str,
    result: dict[str, object],
) -> dict[str, object]:
    """Build the release-oriented baseline manifest payload."""

    del layout
    cwd = Path.cwd()
    source_docs_manifest = SourceDocsConnector.from_config(resolved.model, cwd=cwd).scan()
    profiles = ProfileCollector.from_config(resolved.model, cwd=cwd).collect(
        snapshot_id=str(result.get("snapshot_id") or CURRENT_SNAPSHOT_ID),
    )
    embedding_model_version = DocumentIndexer.from_config(
        resolved.model,
        cwd=cwd,
    ).embedding_model_version
    parser_versions = {
        "c_family": C_FAMILY_PARSER_SCHEMA_VERSION,
        "doc": DOC_PARSER_SCHEMA_VERSION,
        "kconfig": KCONFIG_PARSER_SCHEMA_VERSION,
        "makefile": MAKEFILE_PARSER_SCHEMA_VERSION,
    }
    extractor_versions = {
        "snapshot_collector": SNAPSHOT_COLLECTOR_SCHEMA_VERSION,
        "profile_collector": PROFILE_COLLECTOR_SCHEMA_VERSION,
        "code_indexer": str(result.get("code_indexer_schema_version") or CODE_INDEXER_SCHEMA_VERSION),
        "doc_indexer": str(result.get("doc_indexer_schema_version") or DOC_INDEXER_SCHEMA_VERSION),
        "profile_conditioned_relations": str(
            result.get("relation_schema_version") or PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION
        ),
        "workspace_map": WORKSPACE_MAP_SCHEMA_VERSION,
    }
    return {
        "schema_version": "active_kb_baseline_manifest.v1",
        "baseline_id": baseline_id,
        "default_profile": resolved.model.project.default_profile,
        "published_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "snapshot_id": str(result.get("snapshot_id") or CURRENT_SNAPSHOT_ID),
        "source": source,
        "publish_mode": publish_mode,
        "snapshots": [str(result.get("snapshot_id") or CURRENT_SNAPSHOT_ID)],
        "profiles": sorted({record.profile_id for record in profiles.profile_records}),
        "source_docs_hash": source_docs_manifest.manifest_hash,
        "parser_version": "+".join(parser_versions.values()),
        "extractor_version": "+".join(str(value) for value in extractor_versions.values()),
        "embedding_model": resolved.model.indexing.embeddings.model,
        "embedding_model_version": embedding_model_version,
        "artifacts": {
            "metadata": "db/metadata.db",
            "vectors": "vectors/lancedb",
            "workspace_map": "artifacts/workspace-maps/current.json",
        },
        "versions": {
            "config_schema_version": resolved.model.config_schema_version,
            "query_result_schema_version": QUERY_RESULT_SCHEMA_VERSION,
            "mcp_schema_version": MCP_INTERFACE_SCHEMA_VERSION,
            "parser_versions": parser_versions,
            "extractor_versions": extractor_versions,
            "embedding_model_version": embedding_model_version,
        },
        **source_docs_manifest.to_baseline_manifest_fragment(),
    }


def discover_release_repo_root(cwd: Path) -> Path | None:
    """Discover the repository root that contains release assets."""

    for candidate in (cwd, *cwd.parents):
        if (candidate / "README.md").exists() and (candidate / "examples" / "remote-shared.yaml").exists():
            return candidate
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists() and (candidate / "README.md").exists():
            return candidate
    return None


def default_release_readme_path(repo_root: Path | None) -> Path:
    """Return the default README path used by the release checklist."""

    if repo_root is not None:
        return repo_root / "README.md"
    return Path("README.md")


def default_release_remote_config_path(repo_root: Path | None) -> Path:
    """Return the default remote_shared example config path."""

    if repo_root is not None:
        return repo_root / "examples" / "remote-shared.yaml"
    return Path("examples") / "remote-shared.yaml"


def build_manifest_check(manifest_path: Path) -> dict[str, object]:
    """Validate baseline manifest completeness for release."""

    if not manifest_path.exists():
        return {
            "check_id": "baseline_manifest_complete",
            "status": "fail",
            "blocking": True,
            "message": "Baseline manifest is missing.",
            "details": {"manifest": str(manifest_path)},
        }
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "check_id": "baseline_manifest_complete",
            "status": "fail",
            "blocking": True,
            "message": "Baseline manifest is unreadable or invalid JSON.",
            "details": {"manifest": str(manifest_path), "error": str(exc)},
        }

    missing_fields: list[str] = []
    for field in (
        "schema_version",
        "baseline_id",
        "published_at",
        "source_docs_hash",
        "parser_version",
        "extractor_version",
        "embedding_model",
        "embedding_model_version",
        "snapshots",
        "profiles",
        "artifacts",
        "versions",
        "source_docs",
    ):
        if field not in payload or payload.get(field) in (None, "", [], {}):
            missing_fields.append(field)

    versions = payload.get("versions")
    if not isinstance(versions, dict):
        missing_fields.append("versions")
    else:
        for field in (
            "config_schema_version",
            "query_result_schema_version",
            "mcp_schema_version",
            "parser_versions",
            "extractor_versions",
            "embedding_model_version",
        ):
            if field not in versions or versions.get(field) in (None, "", [], {}):
                missing_fields.append(f"versions.{field}")

    source_docs = payload.get("source_docs")
    if isinstance(source_docs, dict):
        manifest_hash = source_docs.get("manifest_hash")
        if not manifest_hash:
            missing_fields.append("source_docs.manifest_hash")
        elif manifest_hash != payload.get("source_docs_hash"):
            missing_fields.append("source_docs_hash_mismatch")
    else:
        missing_fields.append("source_docs")

    status = "pass" if not missing_fields else "fail"
    return {
        "check_id": "baseline_manifest_complete",
        "status": status,
        "blocking": True,
        "message": (
            "Baseline manifest records source docs and version metadata required for release."
            if status == "pass"
            else "Baseline manifest is missing required release metadata."
        ),
        "details": {
            "manifest": str(manifest_path),
            "missing_fields": missing_fields,
        },
    }


def build_quality_gate_check(report: EvalRunReport) -> dict[str, object]:
    """Validate the quality gate portion of the release checklist."""

    quality_gate = report.metrics.get("quality_gate", {})
    passed = bool(quality_gate.get("passed", report.status == "pass")) and report.status == "pass"
    return {
        "check_id": "quality_gate_passed",
        "status": "pass" if passed else "fail",
        "blocking": True,
        "message": "Quality gate passed." if passed else "Quality gate did not pass.",
        "details": {
            "gate_id": report.gate_id,
            "report_status": report.status,
            "quality_gate_passed": quality_gate.get("passed"),
        },
    }


def build_performance_gate_check(report: EvalRunReport) -> dict[str, object]:
    """Validate the performance gate portion of the release checklist."""

    performance_gate = report.metrics.get("performance_gate", {})
    passed = bool(performance_gate.get("passed", report.status == "pass")) and report.status == "pass"
    return {
        "check_id": "performance_gate_passed",
        "status": "pass" if passed else "fail",
        "blocking": True,
        "message": "Performance gate passed." if passed else "Performance gate did not pass.",
        "details": {
            "gate_id": report.gate_id,
            "report_status": report.status,
            "performance_gate_passed": performance_gate.get("passed"),
        },
    }


def build_stability_gate_check(report: EvalRunReport) -> dict[str, object]:
    """Validate the stability gate portion of the release checklist."""

    stability_gate = report.metrics.get("stability_gate", {})
    gate_passed = bool(stability_gate.get("passed", report.status in {"pass", "partial_ready"}))
    release_window = stability_gate.get("release_window", {})
    release_window_passed = bool(release_window.get("passed", report.status == "pass"))
    if gate_passed and release_window_passed and report.status == "pass":
        status = "pass"
        message = "Stability gate passed, including the release soak window."
    elif gate_passed:
        status = "partial_ready"
        message = "Stability probes passed, but the release soak window is not complete."
    else:
        status = "fail"
        message = "Stability gate did not pass."
    return {
        "check_id": "stability_gate_passed",
        "status": status,
        "blocking": True,
        "message": message,
        "details": {
            "gate_id": report.gate_id,
            "report_status": report.status,
            "stability_gate_passed": stability_gate.get("passed"),
            "release_window": release_window,
        },
    }


def build_tracked_local_release_check(local_dir: Path, *, cwd: Path) -> dict[str, object]:
    """Ensure machine-local artifacts are not tracked for release."""

    warning = inspect_tracked_local_files(local_dir, cwd=cwd)
    if warning is None:
        return {
            "check_id": "local_artifacts_excluded",
            "status": "pass",
            "blocking": True,
            "message": "No unexpected .active-kb/local files are tracked by git.",
            "details": {"local_dir": str(local_dir)},
        }
    return {
        "check_id": "local_artifacts_excluded",
        "status": "fail",
        "blocking": True,
        "message": ".active-kb/local contains tracked files that would leak machine-local artifacts into release.",
        "details": {
            "local_dir": str(local_dir),
            "tracked_files": list(warning.details or ()),
        },
    }


def build_remote_config_release_check(config_path: Path) -> dict[str, object]:
    """Validate the remote_shared example config with a placeholder auth token."""

    if not config_path.exists():
        return {
            "check_id": "remote_shared_config_valid",
            "status": "fail",
            "blocking": True,
            "message": "remote_shared example config is missing.",
            "details": {"config": str(config_path)},
        }
    env = {"ACTIVE_KB_AUTH_TOKEN": "release-checklist-placeholder-token"}
    try:
        resolved = resolve_config(
            config_path=config_path,
            local_config_path=config_path.parent / ".release-checklist.local.yaml",
            env=env,
            cwd=Path.cwd(),
        )
    except (ConfigError, ValueError) as exc:
        return {
            "check_id": "remote_shared_config_valid",
            "status": "fail",
            "blocking": True,
            "message": "remote_shared example config could not be resolved.",
            "details": {"config": str(config_path), "error": str(exc)},
        }
    security_result = validate_startup_security(resolved.model, env=env)
    passed = security_result.ok
    return {
        "check_id": "remote_shared_config_valid",
        "status": "pass" if passed else "fail",
        "blocking": True,
        "message": (
            "remote_shared example config satisfies fail-safe security checks."
            if passed
            else "remote_shared example config fails fail-safe security validation."
        ),
        "details": {
            "config": str(config_path),
            "warnings": [warning.to_dict() for warning in security_result.warnings],
        },
    }


def build_readme_command_check(readme_path: Path) -> dict[str, object]:
    """Validate that README documents the minimum release command surface."""

    required_commands = (
        "active-kb init",
        "active-kb index",
        "active-kb serve",
        "active-kb validate",
        "active-kb clean",
        "active-kb migrate",
    )
    if not readme_path.exists():
        return {
            "check_id": "readme_release_commands",
            "status": "fail",
            "blocking": True,
            "message": "README is missing.",
            "details": {"readme": str(readme_path)},
        }
    content = readme_path.read_text(encoding="utf-8")
    missing = [command for command in required_commands if command not in content]
    status = "pass" if not missing else "fail"
    return {
        "check_id": "readme_release_commands",
        "status": status,
        "blocking": True,
        "message": (
            "README documents init/index/serve/validate/clean/migrate."
            if status == "pass"
            else "README is missing one or more required release commands."
        ),
        "details": {"readme": str(readme_path), "missing_commands": missing},
    }


def summarize_release_checklist_status(checks: Sequence[dict[str, object]]) -> str:
    """Collapse individual checklist checks into one overall status."""

    if any(check.get("status") == "fail" for check in checks):
        return "fail"
    if any(check.get("status") == "partial_ready" for check in checks):
        return "partial_ready"
    return "pass"


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


def init_overrides(args: argparse.Namespace) -> ConfigDict:
    """Build init-specific config overrides."""

    overrides: ConfigDict = {}
    reuse_baseline = getattr(args, "reuse_baseline", None)
    if reuse_baseline is not None:
        set_nested(overrides, ("indexing", "reuse_baseline"), bool(reuse_baseline))
    return overrides


def index_overrides(args: argparse.Namespace) -> ConfigDict:
    """Build index-specific config overrides."""

    overrides: ConfigDict = {}
    if args.full:
        set_nested(overrides, ("indexing", "incremental"), False)
    elif args.incremental:
        set_nested(overrides, ("indexing", "incremental"), True)
    target = getattr(args, "target", "local")
    write_target = "baseline" if target == "baseline" else "local_overlay"
    set_nested(overrides, ("indexing", "write_target"), write_target)
    return overrides


def resolved_args_for_baseline_publish(args: argparse.Namespace) -> argparse.Namespace:
    """Clone parsed args while forcing baseline write target for publish."""

    payload = vars(args).copy()
    payload["target"] = "baseline"
    payload["full"] = True
    payload["incremental"] = False
    return argparse.Namespace(**payload)


def validate_baseline_write_intent(
    args: argparse.Namespace,
    *,
    tool_name: str,
    target: str,
    mode: str,
    publish_mode: str | None,
) -> int | None:
    """Return blocked exit code when baseline writes do not use publish/build mode."""

    if target != "baseline":
        return None
    if mode != "full":
        return emit_command_blocked(
            args,
            tool_name=tool_name,
            code="baseline.full_required",
            message="Baseline writes require full mode.",
            suggested_action="Use --full with --target baseline.",
        )
    if publish_mode not in {"publish", "build"}:
        return emit_command_blocked(
            args,
            tool_name=tool_name,
            code="baseline.publish_mode_required",
            message="Baseline writes require explicit publish/build mode.",
            suggested_action="Pass --publish-mode publish or --publish-mode build.",
        )
    return None


def run_full_index(
    resolved: ResolvedConfig,
    *,
    target: str,
    source: str,
    operation_mode: str,
    progress_callback=None,
    staging_storage: Mapping[str, object] | None = None,
    job_id: str | None = None,
) -> dict[str, object]:
    """Execute one full indexing pass for local overlay or baseline target."""

    model = resolved.model
    cwd = Path.cwd()
    callback = progress_callback or noop_progress_callback
    started_at = utc_timestamp()
    write_target = storage_write_target_for_cli_target(target)
    runtime_model = _resolve_full_index_runtime_model(
        model,
        cwd=cwd,
        target=write_target,
        staging_storage=staging_storage,
    )
    request = StorageWriteRequest(
        target=write_target, operation_mode=cast(Any, operation_mode)
    )

    if target == "baseline":
        baseline_path = configured_sqlite_paths(runtime_model, cwd=cwd)["baseline_metadata"]
        migrate_sqlite_store(baseline_path, target="baseline_metadata")
    else:
        migrate_local_sqlite_stores(runtime_model, cwd=cwd)

    metadata_adapter = SQLiteStorageAdapter.from_config(runtime_model, cwd=cwd)
    vector_adapter = LanceDBVectorAdapter.from_config(
        runtime_model, cwd=cwd, metadata_adapter=metadata_adapter
    )
    writer = metadata_adapter.writer(request)
    vector_writer = vector_adapter.writer(request)
    workspace_inventory = None
    if source in {"all", "code"}:
        workspace_inventory = WorkspaceConnector.from_config(runtime_model, cwd=cwd).scan()
    source_docs_manifest = None
    if source in {"all", "docs"}:
        source_docs_manifest = SourceDocsConnector.from_config(runtime_model, cwd=cwd).scan()

    code_collect_total = (
        0 if workspace_inventory is None else count_indexable_workspace_files(workspace_inventory)
    )
    doc_collect_total = 0 if source_docs_manifest is None else len(source_docs_manifest.files)
    vectors_apply_total = int(source_docs_manifest is not None)
    profile_relations_total = int(source in {"all", "code"})
    workspace_map_total = int(source in {"all", "code"})
    validation_total = int(staging_storage is not None)
    publish_total = int(staging_storage is not None)
    global_total = (
        1
        + 2
        + code_collect_total
        + doc_collect_total
        + vectors_apply_total
        + profile_relations_total
        + workspace_map_total
        + validation_total
        + publish_total
    )
    global_done = 0
    result_metadata: dict[str, object] = {
        "writer": {
            "batch_size": runtime_model.indexing.writer.batch_size,
            "commit_interval_ms": runtime_model.indexing.writer.commit_interval_ms,
        },
        "timings": {
            "parser_seconds": 0.0,
            "embedding_seconds": 0.0,
            "metadata_write_seconds": 0.0,
            "vector_write_seconds": 0.0,
        },
        "diagnostics": {
            "slowest_items": [],
        },
    }

    def emit(
        *,
        phase: str,
        stage_total: int | None,
        stage_done: int | None,
        current_path: str | None = None,
        message: str | None = None,
        explicit_global_done: int | None = None,
    ) -> None:
        callback(
            IndexProgressEvent(
                phase=cast(Any, phase),
                stage_total=stage_total,
                stage_done=stage_done,
                global_total=global_total,
                global_done=global_done if explicit_global_done is None else explicit_global_done,
                current_path=current_path,
                message=message,
                started_at=started_at,
                updated_at=utc_timestamp(),
            )
        )

    global_done += 1
    emit(phase="plan", stage_total=1, stage_done=1, message="Full index plan ready")

    snapshot = SnapshotCollector.from_config(runtime_model, cwd=cwd).collect_and_store(writer)
    global_done += 1
    emit(
        phase="discover",
        stage_total=2,
        stage_done=1,
        current_path=snapshot.snapshot_record.snapshot_id,
        message="Collecting snapshot metadata",
    )
    profiles = ProfileCollector.from_config(runtime_model, cwd=cwd).collect_and_store(
        writer,
        snapshot_id=snapshot.snapshot_record.snapshot_id,
    )
    global_done += 1
    emit(
        phase="discover",
        stage_total=2,
        stage_done=2,
        current_path=profiles.resolution.resolved_profile_id,
        message="Collecting profile metadata",
    )

    code_records = None
    if source in {"all", "code"}:
        code_collect_base = global_done

        def handle_code_collect(event: IndexProgressEvent) -> None:
            callback(
                IndexProgressEvent(
                    phase=event.phase,
                    stage_total=event.stage_total,
                    stage_done=event.stage_done,
                    global_total=global_total,
                    global_done=code_collect_base + (event.stage_done or 0),
                    current_path=event.current_path,
                    message=event.message,
                    warnings_count=event.warnings_count,
                    started_at=started_at,
                    updated_at=utc_timestamp(),
                )
            )

        code_records = CodeIndexer.from_config(runtime_model, cwd=cwd).collect_and_store(
            writer,
            snapshot_id=snapshot.snapshot_record.snapshot_id,
            workspace_inventory=workspace_inventory,
            progress_callback=handle_code_collect,
        )
        _merge_index_result_metadata(result_metadata, "code_collect", code_records.metadata)
        global_done += code_collect_total

    doc_records = None
    if source in {"all", "docs"}:
        doc_collect_base = global_done

        def handle_doc_collect(event: IndexProgressEvent) -> None:
            callback(
                IndexProgressEvent(
                    phase=event.phase,
                    stage_total=event.stage_total,
                    stage_done=event.stage_done,
                    global_total=global_total,
                    global_done=doc_collect_base + (event.stage_done or 0),
                    current_path=event.current_path,
                    message=event.message,
                    warnings_count=event.warnings_count,
                    started_at=started_at,
                    updated_at=utc_timestamp(),
                )
            )

        doc_records = DocumentIndexer.from_config(runtime_model, cwd=cwd).collect_and_store(
            writer,
            vector_writer=vector_writer,
            snapshot_id=snapshot.snapshot_record.snapshot_id,
            source_docs_manifest=source_docs_manifest,
            progress_callback=handle_doc_collect,
        )
        _merge_index_result_metadata(result_metadata, "doc_collect", doc_records.metadata)
        global_done += doc_collect_total
        global_done += vectors_apply_total
        emit(
            phase="vectors_apply",
            stage_total=vectors_apply_total,
            stage_done=vectors_apply_total,
            message="Applying document vectors",
            explicit_global_done=global_done,
        )

    relation_records = None
    if code_records is not None:
        relation_started_at = time.perf_counter()
        relation_records = ProfileConditionedRelationExtractor().collect_and_store(
            writer,
            snapshot_id=snapshot.snapshot_record.snapshot_id,
            profiles=profiles.profile_records,
            entities=code_records.entity_records,
            relations=code_records.relation_records,
        )
        global_done += 1
        emit(
            phase="profile_relations",
            stage_total=1,
            stage_done=1,
            message="Refreshing profile-conditioned relations",
        )
        timings = result_metadata["timings"]
        assert isinstance(timings, dict)
        timings["metadata_write_seconds"] = round(
            float(timings.get("metadata_write_seconds", 0.0))
            + (time.perf_counter() - relation_started_at),
            6,
        )

    if source in {"all", "code"}:
        workspace_map_started_at = time.perf_counter()
        WorkspaceMapBuilder.from_config(runtime_model, cwd=cwd).collect_and_write(
            snapshot_id=snapshot.snapshot_record.snapshot_id,
            workspace_inventory=workspace_inventory,
            reader=metadata_adapter.reader(),
            profiles=profiles.profile_records,
            profile_resolution=profiles.resolution.to_dict(),
        )
        global_done += 1
        emit(
            phase="workspace_map",
            stage_total=1,
            stage_done=1,
            message="Refreshing workspace map",
        )
        timings = result_metadata["timings"]
        assert isinstance(timings, dict)
        timings["workspace_map_seconds"] = round(time.perf_counter() - workspace_map_started_at, 6)

    metadata_adapter.close()
    vector_adapter.close()

    result_status = "ready"
    if staging_storage is not None:
        emit(
            phase="validate",
            stage_total=1,
            stage_done=0,
            message="Running staging storage validation",
        )
        validation_report = validate_storage_consistency(runtime_model, cwd=cwd, mode="full")
        result_metadata["validation"] = validation_report.to_dict()
        global_done += 1
        emit(
            phase="validate",
            stage_total=1,
            stage_done=1,
            message=f"Staging validation finished ({validation_report.status})",
        )
        if validation_report.status != "ok":
            result_status = "partial_ready"
            global_done += 1
            result_metadata["publish"] = {
                "status": "skipped",
                "reason": "validation_not_ok",
                "validation_status": validation_report.status,
            }
            emit(
                phase="publish",
                stage_total=1,
                stage_done=1,
                message="Skipped publish pointer switch because staging validation did not pass",
            )
        else:
            emit(
                phase="publish",
                stage_total=1,
                stage_done=0,
                message="Checkpointing staging SQLite and switching publish pointer",
            )
            publish_payload = _publish_full_index_storage(
                runtime_model=runtime_model,
                cwd=cwd,
                target=write_target,
                staging_storage=staging_storage,
                job_id=job_id,
            )
            result_metadata["publish"] = publish_payload
            global_done += 1
            emit(
                phase="publish",
                stage_total=1,
                stage_done=1,
                message="Published new live storage pointer",
            )

    emit(
        phase="done",
        stage_total=1,
        stage_done=1,
        message="Full indexing finished",
        explicit_global_done=global_total,
    )

    return {
        "schema_version": "index_full_result.v1",
        "result_status": result_status,
        "target": target,
        "operation_mode": operation_mode,
        "snapshot_id": snapshot.snapshot_record.snapshot_id,
        "source_count": len(code_records.source_records) if code_records is not None else 0,
        "profile_count": len(profiles.profile_records),
        "code_file_count": len(code_records.file_records) if code_records is not None else 0,
        "doc_file_count": len(doc_records.file_records) if doc_records is not None else 0,
        "vector_write_count": len(doc_records.vector_writes) if doc_records is not None else 0,
        "relation_count": len(relation_records.relation_records)
        if relation_records is not None
        else 0,
        "code_indexer_schema_version": None
        if code_records is None
        else code_records.schema_version,
        "doc_indexer_schema_version": None if doc_records is None else doc_records.schema_version,
        "profile_collector_schema_version": profiles.schema_version,
        "relation_schema_version": None
        if relation_records is None
        else relation_records.schema_version,
        "metadata": result_metadata,
    }


def _resolve_full_index_runtime_model(
    model: Any,
    *,
    cwd: Path,
    target: StorageWriteTarget,
    staging_storage: Mapping[str, object] | None,
) -> Any:
    if staging_storage is None:
        return model
    live_sqlite_paths = configured_sqlite_paths(model, cwd=cwd)
    live_vector_paths = configured_lancedb_paths(model, cwd=cwd)
    storage = model.storage
    staging = _require_mapping(staging_storage, "staging")
    target_metadata_path = Path(_require_mapping_text(staging, "metadata_path"))
    target_vector_path = Path(_require_mapping_text(staging, "vector_path"))
    if target == "baseline":
        return model.model_copy(
            update={
                "storage": storage.model_copy(
                    update={
                        "metadata": storage.metadata.model_copy(
                            update={"path": str(target_metadata_path), "mode": "readwrite"}
                        ),
                        "overlay": storage.overlay.model_copy(
                            update={"path": str(live_sqlite_paths["overlay_metadata"])}
                        ),
                        "vector": storage.vector.model_copy(
                            update={"path": str(target_vector_path), "mode": "readwrite"}
                        ),
                        "vector_delta": storage.vector_delta.model_copy(
                            update={"path": str(live_vector_paths["overlay"])}
                        ),
                    }
                )
            }
        )
    return model.model_copy(
        update={
            "storage": storage.model_copy(
                update={
                    "metadata": storage.metadata.model_copy(
                        update={"path": str(live_sqlite_paths["baseline_metadata"])}
                    ),
                    "overlay": storage.overlay.model_copy(
                        update={"path": str(target_metadata_path), "mode": "readwrite"}
                    ),
                    "vector": storage.vector.model_copy(
                        update={"path": str(live_vector_paths["baseline"])}
                    ),
                    "vector_delta": storage.vector_delta.model_copy(
                        update={"path": str(target_vector_path), "mode": "readwrite"}
                    ),
                }
            )
        }
    )


def _publish_full_index_storage(
    *,
    runtime_model: Any,
    cwd: Path,
    target: StorageWriteTarget,
    staging_storage: Mapping[str, object],
    job_id: str | None,
) -> dict[str, object]:
    if not job_id:
        raise ValueError("full staging publish requires a persisted job_id")
    live = _require_mapping(staging_storage, "live")
    staging = _require_mapping(staging_storage, "staging")
    publish_token = str(staging_storage.get("job_token", "")).strip()
    if not publish_token:
        raise ValueError("staging_storage.job_token must not be empty")
    staging_metadata_path = Path(_require_mapping_text(staging, "metadata_path"))
    staging_vector_path = Path(_require_mapping_text(staging, "vector_path"))
    published = resolve_published_storage_for_job(
        target=target,
        job_id=job_id,
        publish_token=publish_token,
        metadata_anchor_path=Path(_require_mapping_text(live, "metadata_path")),
        vector_anchor_path=Path(_require_mapping_text(live, "vector_path")),
    )
    checkpoint_result = checkpoint_sqlite_database(staging_metadata_path, mode="truncate")
    materialize_published_storage(
        staging_metadata_path=staging_metadata_path,
        staging_vector_path=staging_vector_path,
        published=published,
    )
    activate_published_storage(published)
    return {
        "status": "published",
        "target": target,
        "published_storage": published.to_dict(),
        "sqlite_checkpoint": None
        if checkpoint_result is None
        else {
            "mode": checkpoint_result.mode,
            "busy": checkpoint_result.busy,
            "log_frames": checkpoint_result.log_frames,
            "checkpointed_frames": checkpoint_result.checkpointed_frames,
        },
        "resolved_live_storage": {
            "metadata_path": str(published.metadata_path),
            "vector_path": str(published.vector_path),
            "manifest_path": str(published.manifest_path),
        },
    }


def _require_mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ValueError(f"staging_storage.{key} must be a mapping")
    return cast(Mapping[str, object], nested)


def _require_mapping_text(value: Mapping[str, object], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"staging_storage.{key} must be a non-empty string")
    return raw


def _merge_index_result_metadata(
    result_metadata: dict[str, object],
    section_name: str,
    section_metadata: Mapping[str, object],
) -> None:
    result_metadata[f"{section_name}_metadata"] = dict(section_metadata)
    timings = result_metadata.get("timings")
    assert isinstance(timings, dict)
    section_timings = section_metadata.get("timings", {})
    if isinstance(section_timings, Mapping):
        timings["parser_seconds"] = round(
            float(timings.get("parser_seconds", 0.0))
            + float(section_timings.get("parser_seconds", 0.0)),
            6,
        )
        timings["embedding_seconds"] = round(
            float(timings.get("embedding_seconds", 0.0))
            + float(section_timings.get("embedding_seconds", 0.0)),
            6,
        )
        timings["metadata_write_seconds"] = round(
            float(timings.get("metadata_write_seconds", 0.0))
            + float(section_timings.get("metadata_write_seconds", 0.0)),
            6,
        )
        timings["vector_write_seconds"] = round(
            float(timings.get("vector_write_seconds", 0.0))
            + float(section_timings.get("vector_write_seconds", 0.0)),
            6,
        )
    diagnostics = result_metadata.get("diagnostics")
    assert isinstance(diagnostics, dict)
    section_diagnostics = section_metadata.get("diagnostics", {})
    if isinstance(section_diagnostics, Mapping):
        slowest = section_diagnostics.get("slowest_items", ())
        existing = diagnostics.get("slowest_items", [])
        if isinstance(slowest, Sequence) and isinstance(existing, list):
            existing.extend(dict(item) for item in slowest if isinstance(item, Mapping))
            diagnostics["slowest_items"] = list(_top_slowest_items(existing))


def _top_slowest_items(
    items: Sequence[Mapping[str, object]],
    *,
    limit: int = 5,
) -> tuple[dict[str, object], ...]:
    ranked = sorted(
        (
            dict(item)
            for item in items
            if isinstance(item.get("elapsed_seconds"), (int, float))
        ),
        key=lambda item: float(item["elapsed_seconds"]),
        reverse=True,
    )
    return tuple(ranked[:limit])


def rebuild_vectors(
    resolved: ResolvedConfig,
    *,
    target: str,
    source: str,
    operation_mode: str,
) -> dict[str, object]:
    """Rebuild vectors by re-indexing document embeddings for selected target."""

    model = resolved.model
    cwd = Path.cwd()
    write_target = storage_write_target_for_cli_target(target)
    request = StorageWriteRequest(
        target=write_target, operation_mode=cast(Any, operation_mode)
    )

    if target == "baseline":
        baseline_path = configured_sqlite_paths(model, cwd=cwd)["baseline_metadata"]
        migrate_sqlite_store(baseline_path, target="baseline_metadata")
    else:
        migrate_local_sqlite_stores(model, cwd=cwd)

    metadata_adapter = SQLiteStorageAdapter.from_config(model, cwd=cwd)
    vector_adapter = LanceDBVectorAdapter.from_config(
        model, cwd=cwd, metadata_adapter=metadata_adapter
    )
    writer = metadata_adapter.writer(request)
    vector_writer = vector_adapter.writer(request)

    indexed = DocumentIndexer.from_config(model, cwd=cwd).collect_and_store(
        writer,
        vector_writer=vector_writer,
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )
    return {
        "schema_version": "rebuild_vectors_result.v1",
        "result_status": "ready",
        "target": target,
        "source": source,
        "vectors_rebuilt": len(indexed.vector_writes),
        "doc_files_scanned": len(indexed.file_records),
        "embedding_model_version": DocumentIndexer.from_config(
            model, cwd=cwd
        ).embedding_model_version,
    }


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


def collect_baseline_reuse_status(
    resolved: ResolvedConfig,
    *,
    layout: WorkdirLayout | None = None,
    storage_validation: dict[str, object] | None = None,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Summarize baseline reuse readiness and related warnings."""

    runtime_layout = layout or workdir_layout(resolved)
    manifest_status, manifest_warning = inspect_baseline_manifest(
        runtime_layout.baseline_manifest_path
    )
    manifest_payload, manifest_payload_warning = read_baseline_manifest_payload(
        runtime_layout.baseline_manifest_path
    )
    enabled = bool(resolved.model.indexing.reuse_baseline)
    if not enabled:
        status = "disabled"
    elif not manifest_status.exists:
        status = "missing"
    elif not manifest_status.readable or manifest_payload_warning is not None:
        status = "blocked"
    else:
        status = baseline_reuse_storage_status(
            runtime_layout=runtime_layout,
            storage_validation=storage_validation,
        )

    warnings: list[dict[str, object]] = []
    if enabled and manifest_warning is not None:
        warnings.append(manifest_warning.to_dict())
    if enabled and manifest_payload_warning is not None:
        warnings.append(manifest_payload_warning)
    return (
        {
            "enabled": enabled,
            "status": status,
            "manifest": manifest_status.to_dict(),
            "baseline_id": manifest_payload.get("baseline_id"),
            "default_profile": manifest_payload.get("default_profile"),
            "project_id": manifest_payload.get("project_id"),
            "schema_version": manifest_payload.get("schema_version"),
            "storage_status": baseline_reuse_storage_status(
                runtime_layout=runtime_layout,
                storage_validation=storage_validation,
            ),
        },
        tuple(warnings),
    )


def baseline_reuse_storage_status(
    *,
    runtime_layout: WorkdirLayout,
    storage_validation: dict[str, object] | None,
) -> str:
    """Map baseline-related storage findings to a baseline reuse readiness status."""

    if storage_validation is None:
        return "ready"
    checks = storage_validation.get("checks")
    if not isinstance(checks, list):
        return "ready"
    baseline_root = str(runtime_layout.baseline_dir)
    status = "ready"
    for raw_check in checks:
        if not isinstance(raw_check, dict):
            continue
        code = str(raw_check.get("check_code", ""))
        severity = str(raw_check.get("severity", ""))
        affected = tuple(str(item) for item in raw_check.get("affected_objects", ()))
        baseline_related = code.startswith("baseline.") or any(
            item.startswith(baseline_root) for item in affected
        )
        if not baseline_related:
            continue
        if code == "storage.schema_missing":
            return "missing"
        if severity == "blocked":
            return "blocked"
        if severity in {"degraded", "caution"}:
            status = "partial_ready"
    return status


def read_baseline_manifest_payload(
    path: Path,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Read a baseline manifest payload when it is available and valid JSON."""

    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, {
            "level": "blocked",
            "code": "baseline.manifest_invalid",
            "message": f"Baseline manifest is not valid JSON: {exc}",
            "path": str(path),
        }
    if not isinstance(payload, dict):
        return {}, {
            "level": "blocked",
            "code": "baseline.manifest_invalid",
            "message": "Baseline manifest must decode to a JSON object.",
            "path": str(path),
        }
    return cast(dict[str, object], payload), None


def collect_profile_status(
    resolved: ResolvedConfig,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Summarize the current default profile resolution state."""

    collected = ProfileCollector.from_config(resolved.model, cwd=Path.cwd()).collect()
    resolution = collected.resolution
    warnings = tuple(
        warning.to_dict()
        for warning in (
            *collected.warnings,
            *resolution.warnings,
        )
    )
    return (
        {
            "requested": resolution.requested,
            "status": resolution.status,
            "resolved_profile_id": resolution.resolved_profile_id,
            "source": resolution.source,
            "confidence": resolution.confidence,
            "profile_count": len(collected.profile_records),
            "candidate_count": len(resolution.candidates),
            "candidate_profile_ids": [candidate.profile_id for candidate in resolution.candidates],
            "manifest_hash": collected.manifest_hash,
        },
        warnings,
    )


def collect_index_status(
    resolved: ResolvedConfig,
    *,
    validation_mode: Literal["quick", "full"] = "full",
    emit_progress: bool = False,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Summarize storage validation plus recent index job state."""

    adapter = SQLiteStorageAdapter.from_config(resolved.model, cwd=Path.cwd())
    reader = adapter.reader()
    storage_report = validate_storage_consistency(
        resolved.model,
        cwd=Path.cwd(),
        mode=validation_mode,
        emit_progress=emit_progress,
    )
    recent_jobs_raw = tuple(reader.iter_jobs())[:10]
    recent_jobs = tuple(
        {
            "job_id": job.job_id,
            "job_type": job.job_type,
            "status": job.status,
            "write_target": job.write_target,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "snapshot_id": job.snapshot_id,
            "profile_id": job.profile_id,
            "error_summary": job.error_summary,
            "metadata": dict(job.metadata),
        }
        for job in recent_jobs_raw
    )
    current_snapshot = reader.get_snapshot(resolved.model.project.default_snapshot)
    result_status = infer_index_result_status(
        storage_status=storage_report.status,
        current_snapshot_exists=current_snapshot is not None,
        recent_jobs=recent_jobs_raw,
    )
    payload = {
        "result_status": result_status,
        "message": index_status_message(result_status),
        "snapshot_id": None if current_snapshot is None else current_snapshot.snapshot_id,
        "storage_validation": storage_report.to_dict(),
        "recent_jobs": list(recent_jobs),
        "job_status_counts": dict(sorted(Counter(job.status for job in recent_jobs_raw).items())),
    }
    warnings = tuple(
        warning_from_storage_check(check)
        for check in storage_report.checks
        if check.severity != "info"
    )
    return payload, warnings


def infer_index_result_status(
    *,
    storage_status: str,
    current_snapshot_exists: bool,
    recent_jobs: Sequence[Any],
) -> str:
    """Infer the user-facing index status from validation and job state."""

    if recent_jobs:
        latest = recent_jobs[0]
        status = str(latest.status)
        if status in RUNNING_JOB_STATUSES or status in {
            "pending",
            "ready",
            "failed",
            "partial_ready",
        }:
            return status
    if storage_status == "blocked":
        return "blocked"
    if not current_snapshot_exists:
        return "missing"
    if storage_status == "degraded":
        return "partial_ready"
    return "ready"


def index_status_message(result_status: str) -> str:
    """Return a short human-readable index health summary."""

    messages = {
        "pending": "An index job is queued.",
        "discovering": "An index job is collecting workspace and source metadata.",
        "parsing": "An index job is parsing source inputs.",
        "extracting": "An index job is extracting entities, chunks, and relations.",
        "embedding": "An index job is building vector payloads.",
        "reporting": "An index job is writing reports and final metadata.",
        "ready": "The current snapshot is queryable.",
        "failed": "The most recent index job failed; inspect recent_jobs for details.",
        "partial_ready": "The index is queryable with degraded coverage or warnings.",
        "blocked": "Storage validation found blocking issues.",
        "missing": "No indexed snapshot is available yet.",
    }
    return messages.get(result_status, "Index status is unknown.")


def warning_from_storage_check(check: Any) -> dict[str, object]:
    """Convert one storage validation finding into the shared warning shape."""

    return {
        "level": check.severity,
        "code": check.check_code,
        "message": check.message,
        "affected_objects": list(check.affected_objects),
        "suggested_action": check.suggested_action,
        "details": dict(check.details),
    }


def collect_observability_status(
    resolved: ResolvedConfig,
    *,
    layout: WorkdirLayout,
    index_status: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Load the persisted observability snapshot and refresh runtime gauges."""

    store = ObservabilityStore.from_layout(layout)
    return store.collect_status(
        config=resolved.model,
        layout=layout,
        cwd=Path.cwd(),
        index_status=index_status,
    )


def record_index_observability(
    resolved: ResolvedConfig,
    *,
    result: IncrementalIndexResult | Mapping[str, object],
    duration_seconds: float,
    job_id: str | None,
) -> None:
    """Best-effort persistence of index observability data."""

    try:
        store = ObservabilityStore.from_layout(workdir_layout(resolved))
        store.record_index_run(
            result=result,
            duration_seconds=duration_seconds,
            job_id=job_id,
        )
    except OSError:
        return


def collect_cli_warnings(*warning_groups: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Merge warning payloads while preserving order and removing duplicates."""

    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for group in warning_groups:
        for warning in group:
            code = str(warning.get("code", "unknown"))
            message = str(warning.get("message", ""))
            key = (code, message)
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(warning))
    return merged


def format_baseline_reuse_line(summary: dict[str, object]) -> str:
    """Render one text-mode baseline reuse summary line."""

    status = str(summary["status"])
    baseline_id = summary.get("baseline_id")
    suffix = f" [{baseline_id}]" if baseline_id else ""
    return f"Baseline reuse: {status}{suffix}"


def format_profile_line(summary: dict[str, object]) -> str:
    """Render one text-mode profile summary line."""

    status = str(summary["status"])
    resolved_profile = summary.get("resolved_profile_id")
    requested = summary.get("requested")
    if resolved_profile:
        return f"Profile: {status} ({resolved_profile})"
    if requested:
        return f"Profile: {status} (requested={requested})"
    return f"Profile: {status}"


def format_index_line(summary: dict[str, object]) -> str:
    """Render one text-mode index summary line."""

    return f"Index: {summary['result_status']} ({summary['message']})"


def format_query_health_line(observability: Mapping[str, object]) -> str:
    """Render the latest query health summary line for text status output."""

    health_summary = observability.get("health_summary")
    if not isinstance(health_summary, Mapping):
        return "Query health: unknown"
    query = health_summary.get("query")
    if not isinstance(query, Mapping):
        return "Query health: unknown"
    health_state = str(query.get("health_state", "unknown"))
    latest = query.get("latest")
    if not isinstance(latest, Mapping):
        return f"Query health: {health_state}"
    latency = _format_seconds(latest.get("latency_seconds"))
    candidates = _metadata_int(latest.get("retrieval_candidates"), 0)
    evidence = _metadata_int(latest.get("evidence_items_returned"), 0)
    warnings_total = _metadata_int(latest.get("warnings_total"), 0)
    return (
        "Query health: "
        f"{health_state} (last={latency}, candidates={candidates}, "
        f"evidence={evidence}, warnings={warnings_total})"
    )


def format_recent_index_health_line(observability: Mapping[str, object]) -> str:
    """Render the latest index health summary line for text status output."""

    health_summary = observability.get("health_summary")
    if not isinstance(health_summary, Mapping):
        return "Recent index health: unknown"
    index = health_summary.get("index")
    if not isinstance(index, Mapping):
        return "Recent index health: unknown"
    health_state = str(index.get("health_state", "unknown"))
    latest = index.get("latest")
    current_result_status = index.get("current_result_status")
    if not isinstance(latest, Mapping):
        if current_result_status is None:
            return f"Recent index health: {health_state}"
        return f"Recent index health: {health_state} (status={current_result_status})"
    duration = _format_seconds(latest.get("duration_seconds"))
    files_total = _metadata_int(latest.get("files_total"), 0)
    files_failed = _metadata_int(latest.get("files_failed"), 0)
    result_status = current_result_status or latest.get("result_status")
    return (
        "Recent index health: "
        f"{health_state} (status={result_status}, last={duration}, "
        f"files={files_total}, failed={files_failed})"
    )


def _format_seconds(value: object) -> str:
    if isinstance(value, int | float):
        return f"{float(value):.3f}s"
    return "n/a"


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


def debug_progress(message: str) -> None:
    """Emit one progress hint to stderr without affecting JSON stdout."""

    print(f"active-kb: {message}", file=sys.stderr, flush=True)


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


def emit_command_blocked(
    args: argparse.Namespace,
    *,
    tool_name: str,
    code: str,
    message: str,
    suggested_action: str,
    details: dict[str, object] | None = None,
) -> int:
    """Emit one blocked result for non-security command policy violations."""

    warning = Warning(
        level="blocked",
        code=code,
        message=message,
        details=details or {},
        actionable=True,
        suggested_action=suggested_action,
    )
    payload = QueryResult.blocked(
        tool_name=tool_name,
        summary=message,
        warnings=(warning,),
        next_queries=(suggested_action,),
        diagnostics={"blocked_reason": "command_policy", "warning_codes": [code]},
    ).to_dict()
    if getattr(args, "format", None) == "json":
        print_json(payload)
    else:
        print(f"active-kb: blocked [{code}]: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
