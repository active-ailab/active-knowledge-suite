"""SQLite storage schema, migration helpers, and metadata/FTS adapters."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage.base import (
    ALL_SCOPE,
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    FTSMatch,
    FTSQuery,
    JobRecord,
    LogicalChunk,
    LogicalEntity,
    LogicalEvidence,
    LogicalRelation,
    ProfileRecord,
    QueryScope,
    RelationRecord,
    RelationValidationIssue,
    ReplacementRecord,
    ReplacementResolution,
    SnapshotRecord,
    SourceRecord,
    StorageAccessError,
    StorageFTSTable,
    StorageMetadata,
    StorageObjectType,
    StorageSourceIndex,
    StorageWriteRequest,
    TombstoneRecord,
    VectorRefRecord,
    make_replacement_id,
    make_tombstone_id,
    validate_write_request,
)

SQLiteTarget = Literal["baseline_metadata", "overlay_metadata", "jobs"]

LATEST_SQLITE_SCHEMA_VERSION: Final = "1.0.1"
_SCHEMA_VERSION_V1: Final = "1.0.0"
_SCHEMA_VERSION_TABLE = "schema_version"
_MIGRATION_HISTORY_TABLE = "migration_history"
_FTS_QUERY_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_./:-]+")
_RRF_K: Final = 60.0


@dataclass(frozen=True)
class SQLiteMigrationStep:
    """One SQLite migration step between schema versions."""

    migration_id: str
    target: SQLiteTarget
    from_versions: tuple[str | None, ...]
    to_version: str
    statements: tuple[str, ...]


@dataclass(frozen=True)
class SQLiteMigrationSpec:
    """Migration policy and steps for one SQLite target."""

    target: SQLiteTarget
    latest_version: str
    allow_auto_major_migration: bool
    steps: tuple[SQLiteMigrationStep, ...]


@dataclass(frozen=True)
class SQLiteMigrationPlan:
    """Planned migrations for one SQLite target."""

    target: SQLiteTarget
    path: Path
    current_version: str | None
    target_version: str
    pending_steps: tuple[SQLiteMigrationStep, ...]
    requires_backup: bool
    requires_confirmation: bool
    exists: bool


@dataclass(frozen=True)
class SQLiteMigrationResult:
    """Result of applying or previewing SQLite migrations."""

    target: SQLiteTarget
    path: Path
    current_version: str | None
    target_version: str
    applied_migration_ids: tuple[str, ...]
    pending_migration_ids: tuple[str, ...]
    backup_ref: Path | None
    dry_run: bool
    created: bool


@dataclass(frozen=True)
class _FTSRow:
    """One raw row returned from one physical FTS table."""

    index_name: StorageFTSTable
    object_id: str
    object_type: Literal["chunk", "entity"]
    file_id: str | None
    snapshot_id: str
    profile_id: str
    source_scope: str
    relative_path: str | None
    domain: str | None
    doc_type: str | None
    title: str | None
    snippet: str | None
    raw_score: float
    source_index: Literal["baseline", "overlay"]
    metadata: StorageMetadata


@dataclass(frozen=True)
class _FTSSpec:
    """Compile-time schema and query details for one FTS table."""

    index_name: StorageFTSTable
    object_type: Literal["chunk", "entity"]
    indexed_columns: tuple[str, ...]
    select_sql: str
    default_title: str


class SQLiteMigrationError(RuntimeError):
    """Raised when a SQLite schema migration cannot proceed safely."""


COMMON_METADATA_STATEMENTS: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE IF NOT EXISTS {_SCHEMA_VERSION_TABLE} (
      target TEXT PRIMARY KEY,
      schema_version TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_MIGRATION_HISTORY_TABLE} (
      migration_id TEXT PRIMARY KEY,
      from_version TEXT,
      to_version TEXT NOT NULL,
      target TEXT NOT NULL,
      status TEXT NOT NULL,
      backup_ref TEXT,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      error_summary TEXT
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_{_MIGRATION_HISTORY_TABLE}_target_status
    ON {_MIGRATION_HISTORY_TABLE}(target, status)
    """,
)

METADATA_CORE_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS source (
      source_id TEXT PRIMARY KEY,
      source_type TEXT NOT NULL,
      display_name TEXT NOT NULL,
      root_path TEXT NOT NULL,
      revision TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot (
      snapshot_id TEXT PRIMARY KEY,
      workspace_revision TEXT NOT NULL,
      baseline_id TEXT,
      manifest_version TEXT,
      created_at TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile (
      profile_record_id TEXT PRIMARY KEY,
      snapshot_id TEXT NOT NULL,
      profile_id TEXT NOT NULL,
      defconfig_hash TEXT,
      dotconfig_hash TEXT,
      defconfig_path TEXT,
      dotconfig_path TEXT,
      app TEXT,
      board TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file (
      file_id TEXT PRIMARY KEY,
      snapshot_id TEXT NOT NULL,
      source_id TEXT NOT NULL,
      relative_path TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      source_scope TEXT NOT NULL DEFAULT 'all',
      profile_id TEXT NOT NULL DEFAULT 'all',
      language TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunk (
      chunk_id TEXT PRIMARY KEY,
      snapshot_id TEXT NOT NULL,
      file_id TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      chunk_type TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      text TEXT NOT NULL,
      source_scope TEXT NOT NULL DEFAULT 'all',
      profile_id TEXT NOT NULL DEFAULT 'all',
      start_line INTEGER,
      end_line INTEGER,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entity (
      entity_id TEXT PRIMARY KEY,
      snapshot_id TEXT NOT NULL,
      file_id TEXT NOT NULL,
      entity_type TEXT NOT NULL,
      name TEXT NOT NULL,
      qualified_name TEXT NOT NULL,
      path TEXT NOT NULL,
      source_scope TEXT NOT NULL DEFAULT 'all',
      profile_id TEXT NOT NULL DEFAULT 'all',
      start_line INTEGER,
      end_line INTEGER,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relation (
      relation_id TEXT PRIMARY KEY,
      snapshot_id TEXT NOT NULL,
      relation_type TEXT NOT NULL,
      src_entity_id TEXT NOT NULL,
      dst_entity_id TEXT NOT NULL,
      source_scope TEXT NOT NULL DEFAULT 'all',
      profile_id TEXT NOT NULL DEFAULT 'all',
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence (
      evidence_id TEXT PRIMARY KEY,
      snapshot_id TEXT NOT NULL,
      object_type TEXT NOT NULL,
      object_id TEXT NOT NULL,
      file_id TEXT NOT NULL,
      source_scope TEXT NOT NULL DEFAULT 'all',
      profile_id TEXT NOT NULL DEFAULT 'all',
      chunk_id TEXT,
      excerpt TEXT,
      citation_label TEXT,
      start_line INTEGER,
      end_line INTEGER,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vector_ref (
      vector_ref_id TEXT PRIMARY KEY,
      object_type TEXT NOT NULL,
      object_id TEXT NOT NULL,
      chunk_id TEXT,
      embedding_model_version TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      source_scope TEXT NOT NULL DEFAULT 'all',
      profile_id TEXT NOT NULL DEFAULT 'all',
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_profile_snapshot_profile
    ON profile(snapshot_id, profile_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_file_snapshot_path
    ON file(snapshot_id, relative_path)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_chunk_file_ordinal
    ON chunk(file_id, ordinal)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_chunk_scope
    ON chunk(snapshot_id, profile_id, source_scope)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_entity_snapshot_name
    ON entity(snapshot_id, name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_entity_scope
    ON entity(snapshot_id, profile_id, source_scope)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_relation_snapshot_src
    ON relation(snapshot_id, src_entity_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_relation_snapshot_dst
    ON relation(snapshot_id, dst_entity_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_evidence_object
    ON evidence(snapshot_id, object_type, object_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_vector_ref_object
    ON vector_ref(object_type, object_id)
    """,
)

FTS_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
      object_id UNINDEXED,
      file_id UNINDEXED,
      snapshot_id UNINDEXED,
      profile_id UNINDEXED,
      source_scope UNINDEXED,
      chunk_type UNINDEXED,
      rel_path UNINDEXED,
      domain UNINDEXED,
      doc_type UNINDEXED,
      title,
      text,
      symbols,
      tags,
      tokenize = 'unicode61'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS entity_fts USING fts5(
      object_id UNINDEXED,
      file_id UNINDEXED,
      snapshot_id UNINDEXED,
      profile_id UNINDEXED,
      source_scope UNINDEXED,
      entity_type UNINDEXED,
      rel_path UNINDEXED,
      domain UNINDEXED,
      doc_type UNINDEXED,
      name,
      qualified_name,
      aliases,
      summary,
      tokenize = 'unicode61'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
      object_id UNINDEXED,
      file_id UNINDEXED,
      snapshot_id UNINDEXED,
      profile_id UNINDEXED,
      source_scope UNINDEXED,
      rel_path UNINDEXED,
      domain UNINDEXED,
      doc_type UNINDEXED,
      title,
      text,
      tokenize = 'unicode61'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
      object_id UNINDEXED,
      file_id UNINDEXED,
      snapshot_id UNINDEXED,
      profile_id UNINDEXED,
      source_scope UNINDEXED,
      chunk_type UNINDEXED,
      rel_path UNINDEXED,
      domain UNINDEXED,
      doc_type UNINDEXED,
      symbol_names,
      comments,
      code_text,
      tokenize = 'unicode61'
    )
    """,
)

METADATA_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    METADATA_CORE_SCHEMA_STATEMENTS + FTS_SCHEMA_STATEMENTS
)

OVERLAY_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = METADATA_SCHEMA_STATEMENTS + (
    """
    CREATE TABLE IF NOT EXISTS tombstone (
      tombstone_id TEXT PRIMARY KEY,
      object_type TEXT NOT NULL,
      object_id TEXT NOT NULL,
      baseline_id TEXT,
      snapshot_id TEXT NOT NULL,
      profile_id TEXT NOT NULL DEFAULT 'all',
      source_scope TEXT NOT NULL DEFAULT 'all',
      reason TEXT NOT NULL,
      created_by_job TEXT NOT NULL,
      created_at TEXT,
      active INTEGER NOT NULL DEFAULT 1,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS replacement (
      replacement_id TEXT PRIMARY KEY,
      object_type TEXT NOT NULL,
      old_object_id TEXT NOT NULL,
      new_object_id TEXT NOT NULL,
      baseline_id TEXT,
      snapshot_id TEXT NOT NULL,
      profile_id TEXT NOT NULL DEFAULT 'all',
      source_scope TEXT NOT NULL DEFAULT 'all',
      path_scope TEXT,
      reason TEXT NOT NULL,
      created_by_job TEXT NOT NULL,
      created_at TEXT,
      active INTEGER NOT NULL DEFAULT 1,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tombstone_scope
    ON tombstone(object_type, object_id, snapshot_id, profile_id, source_scope)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_replacement_scope
    ON replacement(object_type, old_object_id, snapshot_id, profile_id, source_scope)
    """,
)

JOBS_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = COMMON_METADATA_STATEMENTS + (
    """
    CREATE TABLE IF NOT EXISTS job (
      job_id TEXT PRIMARY KEY,
      job_type TEXT NOT NULL,
      status TEXT NOT NULL,
      write_target TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      snapshot_id TEXT,
      profile_id TEXT,
      error_summary TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_checkpoint (
      job_id TEXT NOT NULL,
      checkpoint_key TEXT NOT NULL,
      checkpoint_value TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (job_id, checkpoint_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_lock (
      lock_id TEXT PRIMARY KEY,
      owner_job_id TEXT NOT NULL,
      acquired_at TEXT NOT NULL,
      expires_at TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_job_status_updated
    ON job(status, updated_at)
    """,
)

_MIGRATION_SPECS: Final[dict[SQLiteTarget, SQLiteMigrationSpec]] = {
    "baseline_metadata": SQLiteMigrationSpec(
        target="baseline_metadata",
        latest_version=LATEST_SQLITE_SCHEMA_VERSION,
        allow_auto_major_migration=False,
        steps=(
            SQLiteMigrationStep(
                migration_id="baseline_metadata.v1.bootstrap",
                target="baseline_metadata",
                from_versions=(None, "0.9.0"),
                to_version=LATEST_SQLITE_SCHEMA_VERSION,
                statements=METADATA_SCHEMA_STATEMENTS,
            ),
            SQLiteMigrationStep(
                migration_id="baseline_metadata.v1.fts",
                target="baseline_metadata",
                from_versions=(_SCHEMA_VERSION_V1,),
                to_version=LATEST_SQLITE_SCHEMA_VERSION,
                statements=FTS_SCHEMA_STATEMENTS,
            ),
        ),
    ),
    "overlay_metadata": SQLiteMigrationSpec(
        target="overlay_metadata",
        latest_version=LATEST_SQLITE_SCHEMA_VERSION,
        allow_auto_major_migration=True,
        steps=(
            SQLiteMigrationStep(
                migration_id="overlay_metadata.v1.bootstrap",
                target="overlay_metadata",
                from_versions=(None, "0.9.0"),
                to_version=LATEST_SQLITE_SCHEMA_VERSION,
                statements=OVERLAY_SCHEMA_STATEMENTS,
            ),
            SQLiteMigrationStep(
                migration_id="overlay_metadata.v1.fts",
                target="overlay_metadata",
                from_versions=(_SCHEMA_VERSION_V1,),
                to_version=LATEST_SQLITE_SCHEMA_VERSION,
                statements=FTS_SCHEMA_STATEMENTS,
            ),
        ),
    ),
    "jobs": SQLiteMigrationSpec(
        target="jobs",
        latest_version=LATEST_SQLITE_SCHEMA_VERSION,
        allow_auto_major_migration=True,
        steps=(
            SQLiteMigrationStep(
                migration_id="jobs.v1.bootstrap",
                target="jobs",
                from_versions=(None, "0.9.0"),
                to_version=LATEST_SQLITE_SCHEMA_VERSION,
                statements=JOBS_SCHEMA_STATEMENTS,
            ),
            SQLiteMigrationStep(
                migration_id="jobs.v1.refresh",
                target="jobs",
                from_versions=(_SCHEMA_VERSION_V1,),
                to_version=LATEST_SQLITE_SCHEMA_VERSION,
                statements=(),
            ),
        ),
    ),
}

_FTS_SPECS: Final[dict[StorageFTSTable, _FTSSpec]] = {
    "chunk_fts": _FTSSpec(
        index_name="chunk_fts",
        object_type="chunk",
        indexed_columns=("title", "text", "symbols", "tags"),
        select_sql=(
            "SELECT object_id, file_id, snapshot_id, profile_id, source_scope, rel_path, "
            "domain, doc_type, COALESCE(title, rel_path, chunk_type) AS title, "
            "substr(text, 1, 240) AS snippet, bm25(chunk_fts) AS raw_score "
            "FROM chunk_fts"
        ),
        default_title="chunk",
    ),
    "entity_fts": _FTSSpec(
        index_name="entity_fts",
        object_type="entity",
        indexed_columns=("name", "qualified_name", "aliases", "summary"),
        select_sql=(
            "SELECT object_id, file_id, snapshot_id, profile_id, source_scope, rel_path, "
            "domain, doc_type, COALESCE(name, qualified_name, rel_path, entity_type) AS title, "
            "substr(summary, 1, 240) AS snippet, bm25(entity_fts) AS raw_score "
            "FROM entity_fts"
        ),
        default_title="entity",
    ),
    "doc_fts": _FTSSpec(
        index_name="doc_fts",
        object_type="chunk",
        indexed_columns=("title", "text"),
        select_sql=(
            "SELECT object_id, file_id, snapshot_id, profile_id, source_scope, rel_path, "
            "domain, doc_type, COALESCE(title, rel_path) AS title, "
            "substr(text, 1, 240) AS snippet, bm25(doc_fts) AS raw_score "
            "FROM doc_fts"
        ),
        default_title="document",
    ),
    "code_fts": _FTSSpec(
        index_name="code_fts",
        object_type="chunk",
        indexed_columns=("symbol_names", "comments", "code_text"),
        select_sql=(
            "SELECT object_id, file_id, snapshot_id, profile_id, source_scope, rel_path, "
            "domain, doc_type, COALESCE(symbol_names, rel_path, chunk_type) AS title, "
            "substr(COALESCE(comments, code_text), 1, 240) AS snippet, "
            "bm25(code_fts) AS raw_score FROM code_fts"
        ),
        default_title="code",
    ),
}

_OBJECT_TABLES: Final[dict[StorageObjectType, str]] = {
    "source": "source",
    "snapshot": "snapshot",
    "profile": "profile",
    "file": "file",
    "chunk": "chunk",
    "entity": "entity",
    "relation": "relation",
    "evidence": "evidence",
    "job": "job",
    "fts_row": "",
    "vector_ref": "vector_ref",
}


def migrate_local_sqlite_stores(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
    dry_run: bool = False,
) -> tuple[SQLiteMigrationResult, SQLiteMigrationResult]:
    """Migrate the writable local overlay and jobs stores."""

    validate_write_request(config, StorageWriteRequest(target="overlay"))
    paths = configured_sqlite_paths(config, cwd=cwd)
    overlay = migrate_sqlite_store(
        paths["overlay_metadata"],
        target="overlay_metadata",
        dry_run=dry_run,
    )
    jobs = migrate_sqlite_store(
        paths["jobs"],
        target="jobs",
        dry_run=dry_run,
    )
    return overlay, jobs


def configured_sqlite_paths(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
) -> dict[SQLiteTarget, Path]:
    """Resolve configured SQLite store paths for each target."""

    return {
        "baseline_metadata": resolve_runtime_path(config.storage.metadata.path, cwd),
        "overlay_metadata": resolve_runtime_path(config.storage.overlay.path, cwd),
        "jobs": resolve_runtime_path(config.storage.jobs.path, cwd),
    }


def plan_sqlite_migration(path: Path, *, target: SQLiteTarget) -> SQLiteMigrationPlan:
    """Return the migration plan for one SQLite store."""

    spec = _MIGRATION_SPECS[target]
    exists = path.exists()
    current_version = read_schema_version(path, target=target)
    pending_steps = resolve_pending_steps(spec, current_version=current_version)
    requires_backup = exists and bool(pending_steps)
    requires_confirmation = (
        exists
        and bool(pending_steps)
        and not spec.allow_auto_major_migration
        and is_major_version_change(current_version, spec.latest_version)
    )
    return SQLiteMigrationPlan(
        target=target,
        path=path,
        current_version=current_version,
        target_version=spec.latest_version,
        pending_steps=pending_steps,
        requires_backup=requires_backup,
        requires_confirmation=requires_confirmation,
        exists=exists,
    )


def migrate_sqlite_store(
    path: Path,
    *,
    target: SQLiteTarget,
    dry_run: bool = False,
    confirm_major: bool = False,
    backup_dir: Path | None = None,
    steps_override: Sequence[SQLiteMigrationStep] | None = None,
) -> SQLiteMigrationResult:
    """Apply SQLite migrations for one target with rollback and optional dry-run."""

    plan = plan_sqlite_migration(path, target=target)
    if steps_override is not None:
        spec = _MIGRATION_SPECS[target]
        overridden_spec = SQLiteMigrationSpec(
            target=spec.target,
            latest_version=spec.latest_version,
            allow_auto_major_migration=spec.allow_auto_major_migration,
            steps=tuple(steps_override),
        )
        plan = SQLiteMigrationPlan(
            target=target,
            path=path,
            current_version=plan.current_version,
            target_version=plan.target_version,
            pending_steps=resolve_pending_steps(
                overridden_spec,
                current_version=plan.current_version,
            ),
            requires_backup=plan.requires_backup,
            requires_confirmation=plan.requires_confirmation,
            exists=plan.exists,
        )

    if plan.requires_confirmation and not confirm_major:
        raise SQLiteMigrationError(
            f"{target} requires explicit confirm_major=True for major schema migration."
        )

    pending_ids = tuple(step.migration_id for step in plan.pending_steps)
    if dry_run or not plan.pending_steps:
        return SQLiteMigrationResult(
            target=target,
            path=path,
            current_version=plan.current_version,
            target_version=plan.target_version,
            applied_migration_ids=(),
            pending_migration_ids=pending_ids,
            backup_ref=None,
            dry_run=dry_run,
            created=False,
        )

    created = not path.exists()
    backup_ref = (
        maybe_backup(path, target=target, backup_dir=backup_dir) if plan.requires_backup else None
    )
    try:
        apply_sqlite_migration_plan(path, plan, backup_ref=backup_ref)
    except Exception as exc:
        cleanup_failed_new_store(path, created=created)
        raise SQLiteMigrationError(f"failed to migrate {target} at {path}: {exc}") from exc

    return SQLiteMigrationResult(
        target=target,
        path=path,
        current_version=plan.current_version,
        target_version=plan.target_version,
        applied_migration_ids=pending_ids,
        pending_migration_ids=(),
        backup_ref=backup_ref,
        dry_run=False,
        created=created,
    )


def apply_sqlite_migration_plan(
    path: Path,
    plan: SQLiteMigrationPlan,
    *,
    backup_ref: Path | None,
) -> None:
    """Apply one SQLite migration plan transactionally."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for step in plan.pending_steps:
            started_at = utc_now()
            ensure_metadata_tables(connection)
            record_migration_start(
                connection,
                step=step,
                backup_ref=backup_ref,
                from_version=plan.current_version,
                started_at=started_at,
            )
            execute_statements(connection, step.statements)
            write_schema_version(connection, target=plan.target, version=step.to_version)
            record_migration_finish(
                connection,
                migration_id=step.migration_id,
                finished_at=utc_now(),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def maybe_backup(path: Path, *, target: SQLiteTarget, backup_dir: Path | None) -> Path:
    """Create a point-in-time backup before mutating an existing SQLite store."""

    destination_dir = backup_dir or (path.parent / "backups")
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = destination_dir / f"{path.stem}.{target}.{timestamp}.bak"
    shutil.copy2(path, backup_path)
    return backup_path


def cleanup_failed_new_store(path: Path, *, created: bool) -> None:
    """Remove a newly created SQLite file when initial migration fails."""

    if created and path.exists():
        path.unlink(missing_ok=True)


def read_schema_version(path: Path, *, target: SQLiteTarget) -> str | None:
    """Read the stored schema version for one SQLite target, if present."""

    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(path)
    except sqlite3.DatabaseError as exc:
        raise SQLiteMigrationError(f"{path} is not a valid SQLite database: {exc}") from exc
    try:
        if not table_exists(connection, _SCHEMA_VERSION_TABLE):
            return None
        row = connection.execute(
            f"SELECT schema_version FROM {_SCHEMA_VERSION_TABLE} WHERE target = ?",
            (target,),
        ).fetchone()
        if row is None:
            return None
        value = row[0]
        return str(value) if value is not None else None
    finally:
        connection.close()


def resolve_pending_steps(
    spec: SQLiteMigrationSpec,
    *,
    current_version: str | None,
) -> tuple[SQLiteMigrationStep, ...]:
    """Resolve the ordered migration steps needed to reach the latest schema version."""

    if current_version == spec.latest_version:
        return ()

    version = current_version
    pending: list[SQLiteMigrationStep] = []
    while version != spec.latest_version:
        step = next(
            (candidate for candidate in spec.steps if version in candidate.from_versions),
            None,
        )
        if step is None:
            raise SQLiteMigrationError(
                f"No migration path for target={spec.target} from version={version!r}."
            )
        pending.append(step)
        version = step.to_version
    return tuple(pending)


def ensure_metadata_tables(connection: sqlite3.Connection) -> None:
    """Create migration bookkeeping tables if they do not exist."""

    execute_statements(connection, COMMON_METADATA_STATEMENTS)


def execute_statements(connection: sqlite3.Connection, statements: Iterable[str]) -> None:
    """Execute a sequence of SQLite DDL/DML statements."""

    for statement in statements:
        connection.execute(statement)


def record_migration_start(
    connection: sqlite3.Connection,
    *,
    step: SQLiteMigrationStep,
    backup_ref: Path | None,
    from_version: str | None,
    started_at: str,
) -> None:
    """Insert or replace the running migration history row."""

    connection.execute(
        f"""
        INSERT OR REPLACE INTO {_MIGRATION_HISTORY_TABLE} (
          migration_id,
          from_version,
          to_version,
          target,
          status,
          backup_ref,
          started_at,
          finished_at,
          error_summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            step.migration_id,
            from_version,
            step.to_version,
            step.target,
            "running",
            str(backup_ref) if backup_ref else None,
            started_at,
            None,
            None,
        ),
    )


def record_migration_finish(
    connection: sqlite3.Connection,
    *,
    migration_id: str,
    finished_at: str,
) -> None:
    """Mark one migration as applied."""

    connection.execute(
        f"""
        UPDATE {_MIGRATION_HISTORY_TABLE}
        SET status = ?, finished_at = ?, error_summary = ?
        WHERE migration_id = ?
        """,
        ("applied", finished_at, None, migration_id),
    )


def write_schema_version(
    connection: sqlite3.Connection,
    *,
    target: SQLiteTarget,
    version: str,
) -> None:
    """Upsert the current schema version row."""

    connection.execute(
        f"""
        INSERT INTO {_SCHEMA_VERSION_TABLE} (target, schema_version, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(target) DO UPDATE SET
          schema_version = excluded.schema_version,
          updated_at = excluded.updated_at
        """,
        (target, version, utc_now()),
    )


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    """Return whether a table exists in the current SQLite database."""

    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def is_major_version_change(current: str | None, target: str) -> bool:
    """Return whether moving to target crosses a semantic major version boundary."""

    if current is None:
        return True
    return semver_major(current) != semver_major(target)


def semver_major(version: str) -> int:
    """Return the semantic major component for a version string."""

    try:
        return int(version.split(".", 1)[0])
    except ValueError as exc:
        raise SQLiteMigrationError(f"invalid semantic version {version!r}") from exc


def utc_now() -> str:
    """Return a compact UTC timestamp for migration records."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class SQLiteStorageAdapter:
    """Local-first SQLite adapter with baseline+overlay FTS logical views."""

    def __init__(
        self,
        *,
        baseline_metadata_path: Path,
        overlay_metadata_path: Path,
        jobs_path: Path | None = None,
        config: ActiveKnowledgeConfig | None = None,
    ) -> None:
        self._baseline_metadata_path = baseline_metadata_path
        self._overlay_metadata_path = overlay_metadata_path
        self._jobs_path = jobs_path
        self._config = config

    @classmethod
    def from_config(cls, config: ActiveKnowledgeConfig, *, cwd: Path) -> SQLiteStorageAdapter:
        """Build a SQLite adapter from validated config."""

        paths = configured_sqlite_paths(config, cwd=cwd)
        return cls(
            baseline_metadata_path=paths["baseline_metadata"],
            overlay_metadata_path=paths["overlay_metadata"],
            jobs_path=paths["jobs"],
            config=config,
        )

    def reader(self) -> SQLiteStorageReader:
        """Return a reader over physical and logical metadata views."""

        return SQLiteStorageReader(
            baseline_metadata_path=self._baseline_metadata_path,
            overlay_metadata_path=self._overlay_metadata_path,
            jobs_path=self._jobs_path,
        )

    def writer(self, request: StorageWriteRequest) -> SQLiteStorageWriter:
        """Return a writer scoped to one explicit baseline or overlay target."""

        if self._config is not None:
            validate_write_request(self._config, request)
        return SQLiteStorageWriter(
            baseline_metadata_path=self._baseline_metadata_path,
            overlay_metadata_path=self._overlay_metadata_path,
            jobs_path=self._jobs_path,
            request=request,
        )

    def close(self) -> None:
        """Release adapter resources."""

        return None


class SQLiteStorageReader:
    """Read physical rows, logical views, and merged FTS candidates."""

    def __init__(
        self,
        *,
        baseline_metadata_path: Path,
        overlay_metadata_path: Path,
        jobs_path: Path | None = None,
    ) -> None:
        self._baseline_metadata_path = baseline_metadata_path
        self._overlay_metadata_path = overlay_metadata_path
        self._jobs_path = jobs_path

    def get_source(self, source_id: str) -> SourceRecord | None:
        return self._get_first_physical(
            "source",
            source_id,
            reader=row_to_source_record,
        )

    def iter_sources(self) -> tuple[SourceRecord, ...]:
        return self._iter_unique_simple("source", "source_id", row_to_source_record)

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord | None:
        return self._get_first_physical(
            "snapshot",
            snapshot_id,
            reader=row_to_snapshot_record,
        )

    def iter_snapshots(self) -> tuple[SnapshotRecord, ...]:
        return self._iter_unique_simple("snapshot", "snapshot_id", row_to_snapshot_record)

    def get_profile(self, profile_record_id: str) -> ProfileRecord | None:
        return self._get_first_physical(
            "profile",
            profile_record_id,
            reader=row_to_profile_record,
        )

    def iter_profiles(self, snapshot_id: str | None = None) -> tuple[ProfileRecord, ...]:
        rows = self._iter_simple_with_optional_filter(
            "profile",
            "profile_record_id",
            row_to_profile_record,
            "snapshot_id",
            snapshot_id,
        )
        return rows

    def get_file(self, file_id: str) -> FileRecord | None:
        return self._get_first_physical("file", file_id, reader=row_to_file_record)

    def iter_files(self, scope: QueryScope) -> tuple[FileRecord, ...]:
        rows = self._iter_scoped_rows("file", scope, reader=row_to_file_record)
        return tuple(record for record in rows if self._path_matches(scope, record.relative_path))

    def get_chunk(self, chunk_id: str) -> ChunkRecord | None:
        return self._get_first_physical("chunk", chunk_id, reader=row_to_chunk_record)

    def iter_chunks(self, scope: QueryScope) -> tuple[ChunkRecord, ...]:
        rows = self._iter_scoped_rows("chunk", scope, reader=row_to_chunk_record)
        return tuple(record for record in rows if self._record_path_matches(scope, record.file_id))

    def get_entity(self, entity_id: str) -> EntityRecord | None:
        return self._get_first_physical("entity", entity_id, reader=row_to_entity_record)

    def iter_entities(self, scope: QueryScope) -> tuple[EntityRecord, ...]:
        rows = self._iter_scoped_rows("entity", scope, reader=row_to_entity_record)
        return tuple(record for record in rows if self._record_path_matches(scope, record.file_id))

    def get_relation(self, relation_id: str) -> RelationRecord | None:
        return self._get_first_physical("relation", relation_id, reader=row_to_relation_record)

    def iter_relations(self, scope: QueryScope) -> tuple[RelationRecord, ...]:
        return self._iter_scoped_rows("relation", scope, reader=row_to_relation_record)

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        return self._get_first_physical("evidence", evidence_id, reader=row_to_evidence_record)

    def iter_evidence(self, scope: QueryScope) -> tuple[EvidenceRecord, ...]:
        rows = self._iter_scoped_rows("evidence", scope, reader=row_to_evidence_record)
        return tuple(record for record in rows if self._record_path_matches(scope, record.file_id))

    def get_vector_ref(self, vector_ref_id: str) -> VectorRefRecord | None:
        return self._get_first_physical(
            "vector_ref",
            vector_ref_id,
            reader=row_to_vector_ref_record,
        )

    def iter_vector_refs(self, scope: QueryScope) -> tuple[VectorRefRecord, ...]:
        rows = self._iter_unique_simple(
            "vector_ref",
            "vector_ref_id",
            row_to_vector_ref_record,
        )
        return tuple(record for record in rows if self._vector_ref_matches_scope(record, scope))

    def get_job(self, job_id: str) -> JobRecord | None:
        if self._jobs_path is None:
            return None
        return self._get_one_by_id(self._jobs_path, "job", "job_id", job_id, row_to_job_record)

    def iter_jobs(self, status: str | None = None) -> tuple[JobRecord, ...]:
        if self._jobs_path is None or not self._jobs_path.exists():
            return ()
        query = "SELECT * FROM job"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC, job_id ASC"
        return self._query_records(self._jobs_path, query, tuple(params), row_to_job_record)

    def logical_chunks(self, scope: QueryScope) -> tuple[LogicalChunk, ...]:
        overlay = tuple(
            LogicalChunk(
                logical_object_id=record.chunk_id,
                physical_object_id=record.chunk_id,
                source_index="overlay",
                record=record,
            )
            for record in self.iter_chunks_from_source(self._overlay_metadata_path, scope)
            if not self._chunk_hidden(record, scope)
        )
        seen = {item.logical_object_id for item in overlay}
        baseline_items: list[LogicalChunk] = []
        for record in self.iter_chunks_from_source(self._baseline_metadata_path, scope):
            object_scope = scope_for_record(
                record.snapshot_id,
                record.profile_id,
                record.source_scope,
                scope,
            )
            resolution = self.resolve_replacement("chunk", record.chunk_id, object_scope)
            logical_id = resolution.resolved_object_id
            if logical_id in seen:
                continue
            if resolution.replaced:
                if not self._logical_object_exists("chunk", logical_id, scope):
                    continue
                baseline_items.append(
                    LogicalChunk(
                        logical_object_id=logical_id,
                        physical_object_id=record.chunk_id,
                        source_index="merged",
                        record=record,
                        replaced_from=(record.chunk_id, *resolution.chain),
                    )
                )
                seen.add(logical_id)
                continue
            if self._chunk_hidden(record, scope):
                continue
            baseline_items.append(
                LogicalChunk(
                    logical_object_id=record.chunk_id,
                    physical_object_id=record.chunk_id,
                    source_index="baseline",
                    record=record,
                )
            )
            seen.add(record.chunk_id)
        return overlay + tuple(baseline_items)

    def logical_entities(self, scope: QueryScope) -> tuple[LogicalEntity, ...]:
        overlay = tuple(
            LogicalEntity(
                logical_object_id=record.entity_id,
                physical_object_id=record.entity_id,
                source_index="overlay",
                record=record,
            )
            for record in self.iter_entities_from_source(self._overlay_metadata_path, scope)
            if not self._entity_hidden(record, scope)
        )
        seen = {item.logical_object_id for item in overlay}
        baseline_items: list[LogicalEntity] = []
        for record in self.iter_entities_from_source(self._baseline_metadata_path, scope):
            object_scope = scope_for_record(
                record.snapshot_id,
                record.profile_id,
                record.source_scope,
                scope,
            )
            resolution = self.resolve_replacement("entity", record.entity_id, object_scope)
            logical_id = resolution.resolved_object_id
            if logical_id in seen:
                continue
            if resolution.replaced:
                if not self._logical_object_exists("entity", logical_id, scope):
                    continue
                baseline_items.append(
                    LogicalEntity(
                        logical_object_id=logical_id,
                        physical_object_id=record.entity_id,
                        source_index="merged",
                        record=record,
                        replaced_from=(record.entity_id, *resolution.chain),
                    )
                )
                seen.add(logical_id)
                continue
            if self._entity_hidden(record, scope):
                continue
            baseline_items.append(
                LogicalEntity(
                    logical_object_id=record.entity_id,
                    physical_object_id=record.entity_id,
                    source_index="baseline",
                    record=record,
                )
            )
            seen.add(record.entity_id)
        return overlay + tuple(baseline_items)

    def logical_relations(self, scope: QueryScope) -> tuple[LogicalRelation, ...]:
        items: list[LogicalRelation] = []
        seen: set[str] = set()
        for source_index, path in (
            ("overlay", self._overlay_metadata_path),
            ("baseline", self._baseline_metadata_path),
        ):
            source = cast(StorageSourceIndex, source_index)
            for record in self.iter_relations_from_source(path, scope):
                if source == "baseline" and record.relation_id in seen:
                    continue
                object_scope = scope_for_record(
                    record.snapshot_id,
                    record.profile_id,
                    record.source_scope,
                    scope,
                )
                if self.is_tombstoned("relation", record.relation_id, object_scope):
                    continue
                relation_resolution = self.resolve_replacement(
                    "relation",
                    record.relation_id,
                    object_scope,
                )
                if relation_resolution.replaced and self._relation_exists(
                    relation_resolution.resolved_object_id,
                    scope,
                ):
                    continue
                src = self.resolve_replacement("entity", record.src_entity_id, object_scope)
                dst = self.resolve_replacement("entity", record.dst_entity_id, object_scope)
                if not self._logical_object_exists("entity", src.resolved_object_id, scope):
                    continue
                if not self._logical_object_exists("entity", dst.resolved_object_id, scope):
                    continue
                relation = record
                merged_source: StorageSourceIndex = source
                if src.replaced or dst.replaced:
                    relation = replace(
                        relation,
                        src_entity_id=src.resolved_object_id,
                        dst_entity_id=dst.resolved_object_id,
                    )
                    merged_source = "merged"
                if source == "baseline" and (
                    self._logical_entity_source(src.resolved_object_id, scope) == "overlay"
                    or self._logical_entity_source(dst.resolved_object_id, scope) == "overlay"
                ):
                    merged_source = "merged"
                items.append(
                    LogicalRelation(
                        logical_object_id=record.relation_id,
                        physical_object_id=record.relation_id,
                        source_index=merged_source,
                        record=relation,
                        replaced_from=tuple(
                            relation_resolution.chain + src.chain + dst.chain
                        ),
                    )
                )
                seen.add(record.relation_id)
        return tuple(items)

    def logical_evidence(self, scope: QueryScope) -> tuple[LogicalEvidence, ...]:
        items: list[LogicalEvidence] = []
        seen: set[str] = set()
        for source_index, path in (
            ("overlay", self._overlay_metadata_path),
            ("baseline", self._baseline_metadata_path),
        ):
            source = cast(StorageSourceIndex, source_index)
            for record in self.iter_evidence_from_source(path, scope):
                if source == "baseline" and record.evidence_id in seen:
                    continue
                if self._evidence_hidden(record, scope):
                    continue
                object_scope = scope_for_record(
                    record.snapshot_id,
                    record.profile_id,
                    record.source_scope,
                    scope,
                )
                resolution = self.resolve_replacement(
                    record.object_type,
                    record.object_id,
                    object_scope,
                )
                evidence = record
                merged_source = source
                replaced_from: tuple[str, ...] = ()
                if resolution.replaced:
                    if record.object_type not in {"chunk", "entity"}:
                        continue
                    if not self._logical_object_exists(
                        cast(Literal["chunk", "entity"], record.object_type),
                        resolution.resolved_object_id,
                        scope,
                    ):
                        continue
                    evidence = replace(evidence, object_id=resolution.resolved_object_id)
                    if evidence.chunk_id == resolution.requested_object_id:
                        evidence = replace(evidence, chunk_id=resolution.resolved_object_id)
                    merged_source = "merged"
                    replaced_from = (record.object_id, *resolution.chain)
                items.append(
                    LogicalEvidence(
                        logical_object_id=record.evidence_id,
                        physical_object_id=record.evidence_id,
                        source_index=merged_source,
                        record=evidence,
                        replaced_from=replaced_from,
                    )
                )
                seen.add(record.evidence_id)
        return tuple(items)

    def validate_relations(self, scope: QueryScope) -> tuple[RelationValidationIssue, ...]:
        live_entities = {item.logical_object_id for item in self.logical_entities(scope)}
        issues: list[RelationValidationIssue] = []
        seen: set[str] = set()
        for source_index, path in (
            ("overlay", self._overlay_metadata_path),
            ("baseline", self._baseline_metadata_path),
        ):
            source = cast(StorageSourceIndex, source_index)
            for record in self.iter_relations_from_source(path, scope):
                if source == "baseline" and record.relation_id in seen:
                    continue
                object_scope = scope_for_record(
                    record.snapshot_id,
                    record.profile_id,
                    record.source_scope,
                    scope,
                )
                if self.is_tombstoned("relation", record.relation_id, object_scope):
                    continue
                relation_resolution = self.resolve_replacement(
                    "relation",
                    record.relation_id,
                    object_scope,
                )
                if relation_resolution.replaced and self._relation_exists(
                    relation_resolution.resolved_object_id,
                    scope,
                ):
                    continue
                src = self.resolve_replacement("entity", record.src_entity_id, object_scope)
                dst = self.resolve_replacement("entity", record.dst_entity_id, object_scope)
                missing_src = src.resolved_object_id not in live_entities
                missing_dst = dst.resolved_object_id not in live_entities
                if missing_src or missing_dst:
                    issues.append(
                        RelationValidationIssue(
                            issue_code="storage.orphan_relation",
                            relation_id=record.relation_id,
                            source_index=source,
                            level="degraded",
                            message=(
                                "Relation endpoint points to a missing or hidden logical entity."
                            ),
                            src_entity_id=record.src_entity_id,
                            dst_entity_id=record.dst_entity_id,
                            resolved_src_entity_id=src.resolved_object_id,
                            resolved_dst_entity_id=dst.resolved_object_id,
                            metadata={
                                "missing_src": missing_src,
                                "missing_dst": missing_dst,
                            },
                        )
                    )
                seen.add(record.relation_id)
        return tuple(issues)

    def resolve_replacement(
        self,
        object_type: StorageObjectType,
        object_id: str,
        scope: QueryScope,
    ) -> ReplacementResolution:
        seen: set[str] = {object_id}
        chain: list[str] = []
        current = object_id
        while True:
            row = self._find_replacement_row(object_type, current, scope)
            if row is None:
                return ReplacementResolution(
                    requested_object_id=object_id,
                    resolved_object_id=current,
                    replaced=bool(chain),
                    chain=tuple(chain),
                )
            next_id = str(row["new_object_id"])
            if next_id in seen:
                return ReplacementResolution(
                    requested_object_id=object_id,
                    resolved_object_id=current,
                    replaced=bool(chain),
                    chain=tuple(chain),
                )
            chain.append(next_id)
            seen.add(next_id)
            current = next_id

    def is_tombstoned(
        self,
        object_type: StorageObjectType,
        object_id: str,
        scope: QueryScope,
    ) -> bool:
        if not self._overlay_metadata_path.exists():
            return False
        query = """
            SELECT 1
            FROM tombstone
            WHERE object_type = ?
              AND active = 1
              AND snapshot_id = ?
              AND (profile_id = ? OR profile_id = ?)
              AND (source_scope = ? OR source_scope = ?)
              AND (object_id = ? OR baseline_id = ?)
            LIMIT 1
        """
        row = self._query_one(
            self._overlay_metadata_path,
            query,
            (
                object_type,
                scope.snapshot_id,
                scope.profile_id,
                ALL_SCOPE,
                scope.source_scope,
                ALL_SCOPE,
                object_id,
                object_id,
            ),
        )
        return row is not None

    def search_fts(self, request: FTSQuery) -> tuple[FTSMatch, ...]:
        compiled_query = normalize_fts_query(request.query)
        if not compiled_query:
            return ()

        source_filter = request.source_index
        source_paths = source_paths_for_filter(source_filter)
        aggregated: dict[str, FTSMatch] = {}
        aggregated_scores: dict[str, float] = {}

        for source_name in source_paths:
            path = (
                self._baseline_metadata_path
                if source_name == "baseline"
                else self._overlay_metadata_path
            )
            raw_rows = self._search_fts_source(path, source_name, request, compiled_query)
            for rank, raw in enumerate(raw_rows, start=1):
                candidate = self._fts_candidate_from_raw(raw, request.scope)
                if candidate is None:
                    continue
                if source_filter is not None and candidate.source_index != source_filter:
                    continue
                score = 1.0 / (_RRF_K + float(rank))
                current = aggregated.get(candidate.logical_object_id)
                if current is None:
                    aggregated[candidate.logical_object_id] = replace(candidate, score=score)
                    aggregated_scores[candidate.logical_object_id] = score
                    continue
                aggregated_scores[candidate.logical_object_id] += score
                preferred = prefer_match(current, candidate)
                aggregated[candidate.logical_object_id] = replace(
                    preferred,
                    score=aggregated_scores[candidate.logical_object_id],
                )

        results = sorted(
            aggregated.values(),
            key=lambda item: (
                -item.score,
                -source_priority(item.source_index),
                item.logical_object_id,
            ),
        )
        return tuple(results[: request.top_k])

    def iter_chunks_from_source(self, path: Path, scope: QueryScope) -> tuple[ChunkRecord, ...]:
        if not path.exists():
            return ()
        return self._iter_scoped_rows_from_path(path, "chunk", scope, row_to_chunk_record)

    def iter_entities_from_source(self, path: Path, scope: QueryScope) -> tuple[EntityRecord, ...]:
        if not path.exists():
            return ()
        return self._iter_scoped_rows_from_path(path, "entity", scope, row_to_entity_record)

    def iter_relations_from_source(
        self,
        path: Path,
        scope: QueryScope,
    ) -> tuple[RelationRecord, ...]:
        if not path.exists():
            return ()
        return self._iter_scoped_rows_from_path(path, "relation", scope, row_to_relation_record)

    def iter_evidence_from_source(
        self,
        path: Path,
        scope: QueryScope,
    ) -> tuple[EvidenceRecord, ...]:
        if not path.exists():
            return ()
        return self._iter_scoped_rows_from_path(path, "evidence", scope, row_to_evidence_record)

    def _get_first_physical(
        self,
        table: str,
        object_id: str,
        *,
        reader: Callable[[sqlite3.Row], Any],
    ) -> Any | None:
        id_column = primary_key_for_table(table)
        overlay = self._get_one_by_id(
            self._overlay_metadata_path,
            table,
            id_column,
            object_id,
            reader,
        )
        if overlay is not None:
            return overlay
        return self._get_one_by_id(
            self._baseline_metadata_path,
            table,
            id_column,
            object_id,
            reader,
        )

    def _get_one_by_id(
        self,
        path: Path,
        table: str,
        id_column: str,
        object_id: str,
        reader: Callable[[sqlite3.Row], Any],
    ) -> Any | None:
        if not path.exists():
            return None
        query = f"SELECT * FROM {table} WHERE {id_column} = ? LIMIT 1"
        row = self._query_one(path, query, (object_id,))
        return None if row is None else reader(row)

    def _iter_unique_simple(
        self,
        table: str,
        id_column: str,
        reader: Callable[[sqlite3.Row], Any],
    ) -> tuple[Any, ...]:
        items: list[Any] = []
        seen: set[str] = set()
        for path in (self._overlay_metadata_path, self._baseline_metadata_path):
            if not path.exists():
                continue
            query = f"SELECT * FROM {table} ORDER BY {id_column} ASC"
            for record in self._query_records(path, query, (), reader):
                key = str(getattr(record, id_column))
                if key in seen:
                    continue
                seen.add(key)
                items.append(record)
        return tuple(items)

    def _iter_simple_with_optional_filter(
        self,
        table: str,
        id_column: str,
        reader: Callable[[sqlite3.Row], Any],
        filter_column: str,
        filter_value: str | None,
    ) -> tuple[Any, ...]:
        items: list[Any] = []
        seen: set[str] = set()
        query = f"SELECT * FROM {table}"
        params: tuple[Any, ...] = ()
        if filter_value is not None:
            query += f" WHERE {filter_column} = ?"
            params = (filter_value,)
        query += f" ORDER BY {id_column} ASC"
        for path in (self._overlay_metadata_path, self._baseline_metadata_path):
            if not path.exists():
                continue
            for record in self._query_records(path, query, params, reader):
                key = str(getattr(record, id_column))
                if key in seen:
                    continue
                seen.add(key)
                items.append(record)
        return tuple(items)

    def _iter_scoped_rows(
        self,
        table: str,
        scope: QueryScope,
        *,
        reader: Callable[[sqlite3.Row], Any],
    ) -> tuple[Any, ...]:
        items: list[Any] = []
        seen: set[str] = set()
        for path in (self._overlay_metadata_path, self._baseline_metadata_path):
            if not path.exists():
                continue
            for record in self._iter_scoped_rows_from_path(path, table, scope, reader):
                key = str(getattr(record, primary_key_for_table(table)))
                if key in seen:
                    continue
                seen.add(key)
                items.append(record)
        return tuple(items)

    def _iter_scoped_rows_from_path(
        self,
        path: Path,
        table: str,
        scope: QueryScope,
        reader: Callable[[sqlite3.Row], Any],
    ) -> tuple[Any, ...]:
        query = f"SELECT * FROM {table} WHERE snapshot_id = ?"
        params: list[Any] = [scope.snapshot_id]
        if has_scope_columns(table):
            if scope.profile_id != ALL_SCOPE:
                query += " AND (profile_id = ? OR profile_id = ?)"
                params.extend([scope.profile_id, ALL_SCOPE])
            if scope.source_scope != ALL_SCOPE:
                query += " AND (source_scope = ? OR source_scope = ?)"
                params.extend([scope.source_scope, ALL_SCOPE])
        query += f" ORDER BY {primary_key_for_table(table)} ASC"
        return self._query_records(path, query, tuple(params), reader)

    def _search_fts_source(
        self,
        path: Path,
        source_index: Literal["baseline", "overlay"],
        request: FTSQuery,
        compiled_query: str,
    ) -> tuple[_FTSRow, ...]:
        if not path.exists():
            return ()
        spec = _FTS_SPECS[request.index_name]
        query = spec.select_sql + f" WHERE {request.index_name} MATCH ? AND snapshot_id = ?"
        params: list[Any] = [compiled_query, request.scope.snapshot_id]
        if request.scope.profile_id != ALL_SCOPE:
            query += " AND (profile_id = ? OR profile_id = ?)"
            params.extend([request.scope.profile_id, ALL_SCOPE])
        if request.scope.source_scope != ALL_SCOPE:
            query += " AND (source_scope = ? OR source_scope = ?)"
            params.extend([request.scope.source_scope, ALL_SCOPE])
        if request.scope.path_scope is not None:
            query += " AND rel_path LIKE ?"
            params.append(path_prefix_pattern(request.scope.path_scope))
        if request.domain is not None:
            query += " AND domain = ?"
            params.append(request.domain)
        if request.doc_type is not None:
            query += " AND doc_type = ?"
            params.append(request.doc_type)
        query += " ORDER BY raw_score ASC LIMIT ?"
        params.append(max(request.top_k * 4, request.top_k))

        rows = self._query_rows(path, query, tuple(params))
        return tuple(
            _FTSRow(
                index_name=request.index_name,
                object_id=str(row["object_id"]),
                object_type=spec.object_type,
                file_id=optional_text(row["file_id"]),
                snapshot_id=str(row["snapshot_id"]),
                profile_id=str(row["profile_id"]),
                source_scope=str(row["source_scope"]),
                relative_path=optional_text(row["rel_path"]),
                domain=optional_text(row["domain"]),
                doc_type=optional_text(row["doc_type"]),
                title=optional_text(row["title"]) or spec.default_title,
                snippet=optional_text(row["snippet"]),
                raw_score=float(row["raw_score"]),
                source_index=source_index,
                metadata={"bm25": float(row["raw_score"])},
            )
            for row in rows
        )

    def _fts_candidate_from_raw(self, raw: _FTSRow, scope: QueryScope) -> FTSMatch | None:
        object_scope = scope_for_record(
            raw.snapshot_id,
            raw.profile_id,
            raw.source_scope,
            scope,
        )
        if raw.file_id is not None and self.is_tombstoned("file", raw.file_id, object_scope):
            return None
        resolution = self.resolve_replacement(raw.object_type, raw.object_id, object_scope)
        if resolution.replaced:
            logical_id = resolution.resolved_object_id
            if not self._logical_object_exists(raw.object_type, logical_id, scope):
                return None
            source_index: StorageSourceIndex = "merged"
            replaced_from = (raw.object_id, *resolution.chain)
        else:
            logical_id = raw.object_id
            if self.is_tombstoned(raw.object_type, logical_id, object_scope):
                return None
            if raw.source_index == "baseline" and self._overlay_object_live(
                raw.object_type,
                logical_id,
                scope,
            ):
                return None
            source_index = raw.source_index
            replaced_from = ()

        if source_index == "merged" and self.is_tombstoned(
            raw.object_type,
            logical_id,
            scope,
        ):
            return None

        return FTSMatch(
            index_name=raw.index_name,
            logical_object_id=logical_id,
            physical_object_id=raw.object_id,
            object_type=raw.object_type,
            source_index=source_index,
            score=0.0,
            file_id=raw.file_id,
            relative_path=raw.relative_path,
            chunk_id=raw.object_id if raw.object_type == "chunk" else None,
            entity_id=raw.object_id if raw.object_type == "entity" else None,
            profile_id=raw.profile_id,
            source_scope=raw.source_scope,
            domain=raw.domain,
            doc_type=raw.doc_type,
            title=raw.title,
            snippet=raw.snippet,
            replaced_from=replaced_from,
            metadata=raw.metadata,
        )

    def _find_replacement_row(
        self,
        object_type: StorageObjectType,
        object_id: str,
        scope: QueryScope,
    ) -> sqlite3.Row | None:
        if not self._overlay_metadata_path.exists():
            return None
        query = """
            SELECT *
            FROM replacement
            WHERE object_type = ?
              AND old_object_id = ?
              AND active = 1
              AND snapshot_id = ?
              AND (profile_id = ? OR profile_id = ?)
              AND (source_scope = ? OR source_scope = ?)
            ORDER BY
              CASE WHEN profile_id = ? THEN 0 ELSE 1 END,
              CASE WHEN source_scope = ? THEN 0 ELSE 1 END,
              created_at DESC,
              replacement_id DESC
            LIMIT 1
        """
        return self._query_one(
            self._overlay_metadata_path,
            query,
            (
                object_type,
                object_id,
                scope.snapshot_id,
                scope.profile_id,
                ALL_SCOPE,
                scope.source_scope,
                ALL_SCOPE,
                scope.profile_id,
                scope.source_scope,
            ),
        )

    def _logical_object_exists(
        self,
        object_type: Literal["chunk", "entity"],
        object_id: str,
        scope: QueryScope,
    ) -> bool:
        getter: Callable[[str], Any | None] = (
            self.get_chunk if object_type == "chunk" else self.get_entity
        )
        record = getter(object_id)
        if record is None:
            return False
        snapshot_id = record.snapshot_id
        profile_id = record.profile_id
        source_scope = record.source_scope
        if snapshot_id != scope.snapshot_id:
            return False
        if scope.profile_id != ALL_SCOPE and profile_id not in (
            ALL_SCOPE,
            scope.profile_id,
        ):
            return False
        if scope.source_scope != ALL_SCOPE and source_scope not in (
            ALL_SCOPE,
            scope.source_scope,
        ):
            return False
        if object_type == "chunk":
            chunk = cast(ChunkRecord, record)
            if self._chunk_hidden(chunk, scope):
                return False
            return self._record_path_matches(scope, chunk.file_id)
        entity = cast(EntityRecord, record)
        if self._entity_hidden(entity, scope):
            return False
        return self._record_path_matches(scope, entity.file_id)

    def _relation_exists(self, relation_id: str, scope: QueryScope) -> bool:
        record = self.get_relation(relation_id)
        if record is None or record.snapshot_id != scope.snapshot_id:
            return False
        if scope.profile_id != ALL_SCOPE and record.profile_id not in (
            ALL_SCOPE,
            scope.profile_id,
        ):
            return False
        if scope.source_scope != ALL_SCOPE and record.source_scope not in (
            ALL_SCOPE,
            scope.source_scope,
        ):
            return False
        object_scope = scope_for_record(
            record.snapshot_id,
            record.profile_id,
            record.source_scope,
            scope,
        )
        return not self.is_tombstoned("relation", relation_id, object_scope)

    def _logical_entity_source(
        self,
        entity_id: str,
        scope: QueryScope,
    ) -> StorageSourceIndex | None:
        for item in self.logical_entities(scope):
            if item.logical_object_id == entity_id:
                return item.source_index
        return None

    def _chunk_hidden(self, record: ChunkRecord, scope: QueryScope) -> bool:
        object_scope = scope_for_record(
            record.snapshot_id,
            record.profile_id,
            record.source_scope,
            scope,
        )
        return self.is_tombstoned("chunk", record.chunk_id, object_scope) or self.is_tombstoned(
            "file",
            record.file_id,
            object_scope,
        )

    def _entity_hidden(self, record: EntityRecord, scope: QueryScope) -> bool:
        object_scope = scope_for_record(
            record.snapshot_id,
            record.profile_id,
            record.source_scope,
            scope,
        )
        return self.is_tombstoned("entity", record.entity_id, object_scope) or self.is_tombstoned(
            "file",
            record.file_id,
            object_scope,
        )

    def _evidence_hidden(self, record: EvidenceRecord, scope: QueryScope) -> bool:
        object_scope = scope_for_record(
            record.snapshot_id,
            record.profile_id,
            record.source_scope,
            scope,
        )
        if self.is_tombstoned("evidence", record.evidence_id, object_scope):
            return True
        if self.is_tombstoned("file", record.file_id, object_scope):
            return True
        if record.chunk_id is not None and self.is_tombstoned(
            "chunk",
            record.chunk_id,
            object_scope,
        ):
            return True
        return record.object_type in {"chunk", "entity", "relation"} and self.is_tombstoned(
            record.object_type,
            record.object_id,
            object_scope,
        )

    def _vector_ref_matches_scope(
        self,
        record: VectorRefRecord,
        scope: QueryScope,
    ) -> bool:
        object_scope = scope_for_record(
            scope.snapshot_id,
            record.profile_id,
            record.source_scope,
            scope,
        )
        if self.is_tombstoned("vector_ref", record.vector_ref_id, object_scope):
            return False
        if scope.profile_id != ALL_SCOPE and record.profile_id not in (
            ALL_SCOPE,
            scope.profile_id,
        ):
            return False
        if scope.source_scope != ALL_SCOPE and record.source_scope not in (
            ALL_SCOPE,
            scope.source_scope,
        ):
            return False

        if record.object_type == "chunk":
            return self._logical_object_exists("chunk", record.object_id, scope)
        if record.object_type == "entity":
            return self._logical_object_exists("entity", record.object_id, scope)

        evidence = self.get_evidence(record.object_id)
        if evidence is None or evidence.snapshot_id != scope.snapshot_id:
            return False
        if scope.profile_id != ALL_SCOPE and evidence.profile_id not in (
            ALL_SCOPE,
            scope.profile_id,
        ):
            return False
        if scope.source_scope != ALL_SCOPE and evidence.source_scope not in (
            ALL_SCOPE,
            scope.source_scope,
        ):
            return False
        return self._record_path_matches(scope, evidence.file_id)

    def _overlay_object_live(
        self,
        object_type: Literal["chunk", "entity"],
        object_id: str,
        scope: QueryScope,
    ) -> bool:
        if not self._overlay_metadata_path.exists():
            return False
        table = "chunk" if object_type == "chunk" else "entity"
        id_column = primary_key_for_table(table)
        query = f"""
            SELECT 1
            FROM {table}
            WHERE {id_column} = ?
              AND snapshot_id = ?
              AND (profile_id = ? OR profile_id = ?)
              AND (source_scope = ? OR source_scope = ?)
            LIMIT 1
        """
        row = self._query_one(
            self._overlay_metadata_path,
            query,
            (
                object_id,
                scope.snapshot_id,
                scope.profile_id,
                ALL_SCOPE,
                scope.source_scope,
                ALL_SCOPE,
            ),
        )
        return row is not None and not self.is_tombstoned(object_type, object_id, scope)

    def _record_path_matches(self, scope: QueryScope, file_id: str) -> bool:
        if scope.path_scope is None:
            return True
        file_record = self.get_file(file_id)
        return file_record is not None and self._path_matches(scope, file_record.relative_path)

    def _path_matches(self, scope: QueryScope, relative_path: str) -> bool:
        if scope.path_scope is None:
            return True
        normalized_scope = normalize_path_scope(scope.path_scope)
        normalized_path = normalize_path_scope(relative_path)
        return (
            normalized_path == normalized_scope
            or normalized_path.startswith(f"{normalized_scope}/")
        )

    def _query_rows(
        self,
        path: Path,
        query: str,
        params: tuple[Any, ...],
    ) -> tuple[sqlite3.Row, ...]:
        if not path.exists():
            return ()
        with sqlite_connection(path) as connection:
            rows = connection.execute(query, params).fetchall()
        return cast(tuple[sqlite3.Row, ...], tuple(rows))

    def _query_one(self, path: Path, query: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        if not path.exists():
            return None
        with sqlite_connection(path) as connection:
            row = connection.execute(query, params).fetchone()
        return cast(sqlite3.Row | None, row)

    def _query_records(
        self,
        path: Path,
        query: str,
        params: tuple[Any, ...],
        reader: Callable[[sqlite3.Row], Any],
    ) -> tuple[Any, ...]:
        return tuple(reader(row) for row in self._query_rows(path, query, params))


class SQLiteStorageWriter:
    """Write metadata rows and keep FTS projections in sync."""

    def __init__(
        self,
        *,
        baseline_metadata_path: Path,
        overlay_metadata_path: Path,
        jobs_path: Path | None,
        request: StorageWriteRequest,
    ) -> None:
        self._baseline_metadata_path = baseline_metadata_path
        self._overlay_metadata_path = overlay_metadata_path
        self._jobs_path = jobs_path
        self._request = request

    @property
    def request(self) -> StorageWriteRequest:
        return self._request

    def upsert_source(self, record: SourceRecord) -> None:
        self._upsert_metadata("source", source_record_values(record))

    def upsert_snapshot(self, record: SnapshotRecord) -> None:
        self._upsert_metadata("snapshot", snapshot_record_values(record))

    def upsert_profile(self, record: ProfileRecord) -> None:
        self._upsert_metadata("profile", profile_record_values(record))

    def upsert_file(self, record: FileRecord) -> None:
        self._upsert_metadata("file", file_record_values(record))

    def upsert_chunk(self, record: ChunkRecord) -> None:
        path = self._metadata_path
        with sqlite_connection(path) as connection:
            upsert_row(connection, "chunk", chunk_record_values(record))
            file_record = fetch_file_record(connection, record.file_id)
            sync_chunk_fts(connection, record, file_record)
            connection.commit()

    def upsert_entity(self, record: EntityRecord) -> None:
        path = self._metadata_path
        with sqlite_connection(path) as connection:
            upsert_row(connection, "entity", entity_record_values(record))
            file_record = fetch_file_record(connection, record.file_id)
            sync_entity_fts(connection, record, file_record)
            connection.commit()

    def upsert_relation(self, record: RelationRecord) -> None:
        self._upsert_metadata("relation", relation_record_values(record))

    def upsert_evidence(self, record: EvidenceRecord) -> None:
        self._upsert_metadata("evidence", evidence_record_values(record))

    def upsert_vector_ref(self, record: VectorRefRecord) -> None:
        path = self._metadata_path
        with sqlite_connection(path) as connection:
            upsert_row(connection, "vector_ref", vector_ref_record_values(record))
            sync_chunk_embedding_ref(connection, record)
            connection.commit()

    def upsert_job(self, record: JobRecord) -> None:
        if self._jobs_path is None:
            raise SQLiteMigrationError("jobs SQLite path is not configured.")
        self._upsert_path(self._jobs_path, "job", job_record_values(record))

    def upsert_tombstone(self, record: TombstoneRecord) -> None:
        self._require_overlay_control_write("tombstone")
        self._upsert_metadata("tombstone", tombstone_record_values(record))

    def upsert_replacement(self, record: ReplacementRecord) -> None:
        self._require_overlay_control_write("replacement")
        self._upsert_metadata("replacement", replacement_record_values(record))

    def tombstone_file(
        self,
        file_id: str,
        *,
        scope: QueryScope,
        reason: str,
        created_by_job: str,
    ) -> tuple[TombstoneRecord, ...]:
        self._require_overlay_control_write("tombstone")
        records = collect_file_tombstones(
            baseline_metadata_path=self._baseline_metadata_path,
            overlay_metadata_path=self._overlay_metadata_path,
            file_id=file_id,
            scope=scope,
            reason=reason,
            created_by_job=created_by_job,
        )
        self._upsert_tombstones(records)
        return records

    def tombstone_chunk(
        self,
        chunk_id: str,
        *,
        scope: QueryScope,
        reason: str,
        created_by_job: str,
    ) -> tuple[TombstoneRecord, ...]:
        self._require_overlay_control_write("tombstone")
        records = collect_chunk_tombstones(
            baseline_metadata_path=self._baseline_metadata_path,
            overlay_metadata_path=self._overlay_metadata_path,
            chunk_id=chunk_id,
            scope=scope,
            reason=reason,
            created_by_job=created_by_job,
        )
        self._upsert_tombstones(records)
        return records

    def replace_object(
        self,
        object_type: StorageObjectType,
        old_object_id: str,
        new_object_id: str,
        *,
        scope: QueryScope,
        reason: str,
        created_by_job: str,
        baseline_id: str | None = None,
        metadata: StorageMetadata | None = None,
    ) -> ReplacementRecord:
        self._require_overlay_control_write("replacement")
        record = ReplacementRecord(
            replacement_id=make_replacement_id(
                object_type,
                old_object_id,
                new_object_id,
                scope=scope,
                reason=reason,
            ),
            object_type=object_type,
            old_object_id=old_object_id,
            new_object_id=new_object_id,
            baseline_id=baseline_id,
            reason=reason,
            created_by_job=created_by_job,
            created_at=utc_now(),
            scope=scope,
            metadata={} if metadata is None else metadata,
        )
        self.upsert_replacement(record)
        return record

    def flush(self) -> None:
        return None

    @property
    def _metadata_path(self) -> Path:
        return (
            self._overlay_metadata_path
            if self._request.target == "overlay"
            else self._baseline_metadata_path
        )

    def _upsert_metadata(self, table: str, values: dict[str, Any]) -> None:
        self._upsert_path(self._metadata_path, table, values)

    def _upsert_path(self, path: Path, table: str, values: dict[str, Any]) -> None:
        with sqlite_connection(path) as connection:
            upsert_row(connection, table, values)
            connection.commit()

    def _upsert_tombstones(self, records: tuple[TombstoneRecord, ...]) -> None:
        if not records:
            return
        with sqlite_connection(self._overlay_metadata_path) as connection:
            for record in records:
                upsert_row(connection, "tombstone", tombstone_record_values(record))
            connection.commit()

    def _require_overlay_control_write(self, table: str) -> None:
        if self._request.target != "overlay":
            raise StorageAccessError(f"{table} records must be written to the overlay store.")


def sqlite_connection(path: Path) -> sqlite3.Connection:
    """Open one SQLite connection with row access by column name."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def collect_file_tombstones(
    *,
    baseline_metadata_path: Path,
    overlay_metadata_path: Path,
    file_id: str,
    scope: QueryScope,
    reason: str,
    created_by_job: str,
) -> tuple[TombstoneRecord, ...]:
    """Build file-level cascade tombstones without mutating physical baseline rows."""

    collector = _TombstoneCollector(reason=reason, created_by_job=created_by_job)
    chunk_ids: set[str] = set()
    entity_ids: set[str] = set()
    evidence_ids: set[str] = set()
    for source_index, path in source_paths_with_baseline_first(
        baseline_metadata_path,
        overlay_metadata_path,
    ):
        baseline = source_index == "baseline"
        files = query_scoped_rows_by_column(path, "file", "file_id", file_id, scope)
        for row in files:
            file_record = row_to_file_record(row)
            collector.add(
                "file",
                file_record.file_id,
                scope_for_record(
                    file_record.snapshot_id,
                    file_record.profile_id,
                    file_record.source_scope,
                    scope,
                ),
                baseline=baseline,
            )
        chunks = query_scoped_rows_by_column(path, "chunk", "file_id", file_id, scope)
        for row in chunks:
            chunk = row_to_chunk_record(row)
            chunk_ids.add(chunk.chunk_id)
            collector.add(
                "chunk",
                chunk.chunk_id,
                scope_for_record(chunk.snapshot_id, chunk.profile_id, chunk.source_scope, scope),
                baseline=baseline,
            )
        entities = query_scoped_rows_by_column(path, "entity", "file_id", file_id, scope)
        for row in entities:
            entity = row_to_entity_record(row)
            entity_ids.add(entity.entity_id)
            collector.add(
                "entity",
                entity.entity_id,
                scope_for_record(entity.snapshot_id, entity.profile_id, entity.source_scope, scope),
                baseline=baseline,
            )
        evidence = query_scoped_rows_by_column(path, "evidence", "file_id", file_id, scope)
        for row in evidence:
            item = row_to_evidence_record(row)
            evidence_ids.add(item.evidence_id)
            collector.add(
                "evidence",
                item.evidence_id,
                scope_for_record(item.snapshot_id, item.profile_id, item.source_scope, scope),
                baseline=baseline,
            )
        for relation in query_relations_for_entities(path, entity_ids, scope):
            collector.add(
                "relation",
                relation.relation_id,
                scope_for_record(
                    relation.snapshot_id,
                    relation.profile_id,
                    relation.source_scope,
                    scope,
                ),
                baseline=baseline,
            )
        vector_refs = query_vector_refs_for_objects(
            path,
            chunk_ids=chunk_ids,
            entity_ids=entity_ids,
            evidence_ids=evidence_ids,
            scope=scope,
        )
        for vector_ref in vector_refs:
            collector.add(
                "vector_ref",
                vector_ref.vector_ref_id,
                scope_for_record(
                    scope.snapshot_id,
                    vector_ref.profile_id,
                    vector_ref.source_scope,
                    scope,
                ),
                baseline=baseline,
            )
    return collector.records


def collect_chunk_tombstones(
    *,
    baseline_metadata_path: Path,
    overlay_metadata_path: Path,
    chunk_id: str,
    scope: QueryScope,
    reason: str,
    created_by_job: str,
) -> tuple[TombstoneRecord, ...]:
    """Build chunk-level cascade tombstones for local rechunk/delete jobs."""

    collector = _TombstoneCollector(reason=reason, created_by_job=created_by_job)
    evidence_ids: set[str] = set()
    for source_index, path in source_paths_with_baseline_first(
        baseline_metadata_path,
        overlay_metadata_path,
    ):
        baseline = source_index == "baseline"
        chunks = query_scoped_rows_by_column(path, "chunk", "chunk_id", chunk_id, scope)
        for row in chunks:
            chunk = row_to_chunk_record(row)
            collector.add(
                "chunk",
                chunk.chunk_id,
                scope_for_record(chunk.snapshot_id, chunk.profile_id, chunk.source_scope, scope),
                baseline=baseline,
            )
        evidence = query_evidence_for_chunk(path, chunk_id, scope)
        for row in evidence:
            item = row_to_evidence_record(row)
            evidence_ids.add(item.evidence_id)
            collector.add(
                "evidence",
                item.evidence_id,
                scope_for_record(item.snapshot_id, item.profile_id, item.source_scope, scope),
                baseline=baseline,
            )
        vector_refs = query_vector_refs_for_objects(
            path,
            chunk_ids={chunk_id},
            entity_ids=set(),
            evidence_ids=evidence_ids,
            scope=scope,
        )
        for vector_ref in vector_refs:
            collector.add(
                "vector_ref",
                vector_ref.vector_ref_id,
                scope_for_record(
                    scope.snapshot_id,
                    vector_ref.profile_id,
                    vector_ref.source_scope,
                    scope,
                ),
                baseline=baseline,
            )
    return collector.records


@dataclass
class _TombstoneCollector:
    reason: str
    created_by_job: str

    def __post_init__(self) -> None:
        self._records: dict[str, TombstoneRecord] = {}

    @property
    def records(self) -> tuple[TombstoneRecord, ...]:
        return tuple(self._records.values())

    def add(
        self,
        object_type: StorageObjectType,
        object_id: str,
        scope: QueryScope,
        *,
        baseline: bool,
    ) -> None:
        baseline_id = object_id if baseline else None
        tombstone_id = make_tombstone_id(
            object_type,
            object_id,
            scope=scope,
            reason=self.reason,
            baseline_id=baseline_id,
        )
        if tombstone_id in self._records:
            return
        self._records[tombstone_id] = TombstoneRecord(
            tombstone_id=tombstone_id,
            object_type=object_type,
            object_id=object_id,
            baseline_id=baseline_id,
            snapshot_id=scope.snapshot_id,
            profile_id=scope.profile_id,
            source_scope=scope.source_scope,
            reason=self.reason,
            created_by_job=self.created_by_job,
            created_at=utc_now(),
        )


def source_paths_with_baseline_first(
    baseline_metadata_path: Path,
    overlay_metadata_path: Path,
) -> tuple[tuple[Literal["baseline", "overlay"], Path], ...]:
    return (("baseline", baseline_metadata_path), ("overlay", overlay_metadata_path))


def source_paths_for_filter(
    source_index: StorageSourceIndex | None,
) -> tuple[Literal["baseline", "overlay"], ...]:
    if source_index == "overlay":
        return ("overlay",)
    if source_index == "baseline":
        return ("baseline",)
    return ("overlay", "baseline")


def source_priority(source_index: StorageSourceIndex) -> int:
    if source_index == "overlay":
        return 3
    if source_index == "merged":
        return 2
    return 1


def prefer_match(current: FTSMatch, candidate: FTSMatch) -> FTSMatch:
    """Choose the preferred physical candidate for one logical object."""

    if source_priority(candidate.source_index) > source_priority(current.source_index):
        return candidate
    if source_priority(candidate.source_index) < source_priority(current.source_index):
        return current
    if bm25_score(candidate) < bm25_score(current):
        return candidate
    return current


def bm25_score(match: FTSMatch) -> float:
    value = match.metadata.get("bm25", 0.0)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def path_prefix_pattern(path_scope: str) -> str:
    normalized = normalize_path_scope(path_scope)
    return f"{normalized}%"


def normalize_path_scope(path_scope: str) -> str:
    return path_scope.strip("/").replace("\\", "/")


def scope_for_record(
    snapshot_id: str,
    profile_id: str,
    source_scope: str,
    query_scope: QueryScope,
) -> QueryScope:
    return QueryScope(
        snapshot_id=snapshot_id,
        profile_id=(
            query_scope.profile_id if query_scope.profile_id != ALL_SCOPE else profile_id
        ),
        source_scope=(
            query_scope.source_scope
            if query_scope.source_scope != ALL_SCOPE
            else source_scope
        ),
        path_scope=query_scope.path_scope,
        include_inactive=query_scope.include_inactive,
    )


def normalize_fts_query(raw_query: str) -> str:
    tokens = _FTS_QUERY_TOKEN_RE.findall(raw_query)
    return " ".join(token for token in tokens if token)


def has_scope_columns(table: str) -> bool:
    return table in {"file", "chunk", "entity", "relation", "evidence"}


def primary_key_for_table(table: str) -> str:
    mapping = {
        "source": "source_id",
        "snapshot": "snapshot_id",
        "profile": "profile_record_id",
        "file": "file_id",
        "chunk": "chunk_id",
        "entity": "entity_id",
        "relation": "relation_id",
        "evidence": "evidence_id",
        "vector_ref": "vector_ref_id",
        "job": "job_id",
        "tombstone": "tombstone_id",
        "replacement": "replacement_id",
    }
    return mapping[table]


def query_scoped_rows_by_column(
    path: Path,
    table: str,
    column: str,
    value: str,
    scope: QueryScope,
) -> tuple[sqlite3.Row, ...]:
    if not path.exists():
        return ()
    query = f"SELECT * FROM {table} WHERE {column} = ? AND snapshot_id = ?"
    params: list[Any] = [value, scope.snapshot_id]
    query_parts: list[str] = []
    add_scope_filters(query_parts, params, scope)
    query += "".join(query_parts)
    query += f" ORDER BY {primary_key_for_table(table)} ASC"
    with sqlite_connection(path) as connection:
        rows = connection.execute(query, tuple(params)).fetchall()
    return cast(tuple[sqlite3.Row, ...], tuple(rows))


def query_evidence_for_chunk(
    path: Path,
    chunk_id: str,
    scope: QueryScope,
) -> tuple[sqlite3.Row, ...]:
    if not path.exists():
        return ()
    query = """
        SELECT *
        FROM evidence
        WHERE snapshot_id = ?
          AND (chunk_id = ? OR (object_type = 'chunk' AND object_id = ?))
    """
    params: list[Any] = [scope.snapshot_id, chunk_id, chunk_id]
    query_parts: list[str] = []
    add_scope_filters(query_parts, params, scope)
    query += "".join(query_parts)
    query += " ORDER BY evidence_id ASC"
    with sqlite_connection(path) as connection:
        rows = connection.execute(query, tuple(params)).fetchall()
    return cast(tuple[sqlite3.Row, ...], tuple(rows))


def query_relations_for_entities(
    path: Path,
    entity_ids: set[str],
    scope: QueryScope,
) -> tuple[RelationRecord, ...]:
    if not path.exists() or not entity_ids:
        return ()
    placeholders = ", ".join("?" for _ in entity_ids)
    query = f"""
        SELECT *
        FROM relation
        WHERE snapshot_id = ?
          AND (src_entity_id IN ({placeholders}) OR dst_entity_id IN ({placeholders}))
    """
    ordered_ids = sorted(entity_ids)
    params: list[Any] = [scope.snapshot_id, *ordered_ids, *ordered_ids]
    query_parts: list[str] = []
    add_scope_filters(query_parts, params, scope)
    query += "".join(query_parts)
    query += " ORDER BY relation_id ASC"
    with sqlite_connection(path) as connection:
        rows = connection.execute(query, tuple(params)).fetchall()
    return tuple(row_to_relation_record(cast(sqlite3.Row, row)) for row in rows)


def query_vector_refs_for_objects(
    path: Path,
    *,
    chunk_ids: set[str],
    entity_ids: set[str],
    evidence_ids: set[str],
    scope: QueryScope,
) -> tuple[VectorRefRecord, ...]:
    if not path.exists():
        return ()
    clauses: list[str] = []
    params: list[Any] = []
    add_vector_ref_clause(clauses, params, "chunk", chunk_ids)
    add_vector_ref_clause(clauses, params, "entity", entity_ids)
    add_vector_ref_clause(clauses, params, "evidence", evidence_ids)
    if not clauses:
        return ()
    query = f"SELECT * FROM vector_ref WHERE ({' OR '.join(clauses)})"
    if scope.profile_id != ALL_SCOPE:
        query += " AND (profile_id = ? OR profile_id = ?)"
        params.extend([scope.profile_id, ALL_SCOPE])
    if scope.source_scope != ALL_SCOPE:
        query += " AND (source_scope = ? OR source_scope = ?)"
        params.extend([scope.source_scope, ALL_SCOPE])
    query += " ORDER BY vector_ref_id ASC"
    with sqlite_connection(path) as connection:
        rows = connection.execute(query, tuple(params)).fetchall()
    return tuple(row_to_vector_ref_record(cast(sqlite3.Row, row)) for row in rows)


def add_vector_ref_clause(
    clauses: list[str],
    params: list[Any],
    object_type: str,
    object_ids: set[str],
) -> None:
    if not object_ids:
        return
    placeholders = ", ".join("?" for _ in object_ids)
    clauses.append(f"(object_type = ? AND object_id IN ({placeholders}))")
    params.append(object_type)
    params.extend(sorted(object_ids))


def add_scope_filters(
    query_parts: list[str],
    params: list[Any],
    scope: QueryScope,
) -> None:
    if scope.profile_id != ALL_SCOPE:
        query_parts.append(" AND (profile_id = ? OR profile_id = ?)")
        params.extend([scope.profile_id, ALL_SCOPE])
    if scope.source_scope != ALL_SCOPE:
        query_parts.append(" AND (source_scope = ? OR source_scope = ?)")
        params.extend([scope.source_scope, ALL_SCOPE])


def encode_metadata(metadata: StorageMetadata) -> str:
    return json.dumps(metadata, ensure_ascii=True, sort_keys=True)


def decode_metadata(raw: Any) -> StorageMetadata:
    if raw in (None, ""):
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return cast(StorageMetadata, loaded)
    return {}


def optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def metadata_text(metadata: StorageMetadata, key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    if isinstance(value, dict):
        return None
    return str(value)


def metadata_text_list(metadata: StorageMetadata, key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    if isinstance(value, dict):
        return " ".join(f"{item_key}:{item_value}" for item_key, item_value in value.items())
    return str(value)


def row_to_source_record(row: sqlite3.Row) -> SourceRecord:
    return SourceRecord(
        source_id=str(row["source_id"]),
        source_type=str(row["source_type"]),
        display_name=str(row["display_name"]),
        root_path=str(row["root_path"]),
        revision=optional_text(row["revision"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def row_to_snapshot_record(row: sqlite3.Row) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=str(row["snapshot_id"]),
        workspace_revision=str(row["workspace_revision"]),
        baseline_id=optional_text(row["baseline_id"]),
        manifest_version=optional_text(row["manifest_version"]),
        created_at=optional_text(row["created_at"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def row_to_profile_record(row: sqlite3.Row) -> ProfileRecord:
    return ProfileRecord(
        profile_record_id=str(row["profile_record_id"]),
        snapshot_id=str(row["snapshot_id"]),
        profile_id=str(row["profile_id"]),
        defconfig_hash=optional_text(row["defconfig_hash"]),
        dotconfig_hash=optional_text(row["dotconfig_hash"]),
        defconfig_path=optional_text(row["defconfig_path"]),
        dotconfig_path=optional_text(row["dotconfig_path"]),
        app=optional_text(row["app"]),
        board=optional_text(row["board"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def row_to_file_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        file_id=str(row["file_id"]),
        snapshot_id=str(row["snapshot_id"]),
        source_id=str(row["source_id"]),
        relative_path=str(row["relative_path"]),
        content_hash=str(row["content_hash"]),
        source_scope=str(row["source_scope"]),
        profile_id=str(row["profile_id"]),
        language=optional_text(row["language"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def row_to_chunk_record(row: sqlite3.Row) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=str(row["chunk_id"]),
        snapshot_id=str(row["snapshot_id"]),
        file_id=str(row["file_id"]),
        content_hash=str(row["content_hash"]),
        chunk_type=str(row["chunk_type"]),
        ordinal=int(row["ordinal"]),
        text=str(row["text"]),
        source_scope=str(row["source_scope"]),
        profile_id=str(row["profile_id"]),
        start_line=cast(int | None, row["start_line"]),
        end_line=cast(int | None, row["end_line"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def row_to_entity_record(row: sqlite3.Row) -> EntityRecord:
    return EntityRecord(
        entity_id=str(row["entity_id"]),
        snapshot_id=str(row["snapshot_id"]),
        file_id=str(row["file_id"]),
        entity_type=str(row["entity_type"]),
        name=str(row["name"]),
        qualified_name=str(row["qualified_name"]),
        path=str(row["path"]),
        source_scope=str(row["source_scope"]),
        profile_id=str(row["profile_id"]),
        start_line=cast(int | None, row["start_line"]),
        end_line=cast(int | None, row["end_line"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def row_to_relation_record(row: sqlite3.Row) -> RelationRecord:
    return RelationRecord(
        relation_id=str(row["relation_id"]),
        snapshot_id=str(row["snapshot_id"]),
        relation_type=str(row["relation_type"]),
        src_entity_id=str(row["src_entity_id"]),
        dst_entity_id=str(row["dst_entity_id"]),
        source_scope=str(row["source_scope"]),
        profile_id=str(row["profile_id"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def row_to_evidence_record(row: sqlite3.Row) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=str(row["evidence_id"]),
        snapshot_id=str(row["snapshot_id"]),
        object_type=cast(StorageObjectType, str(row["object_type"])),
        object_id=str(row["object_id"]),
        file_id=str(row["file_id"]),
        source_scope=str(row["source_scope"]),
        profile_id=str(row["profile_id"]),
        chunk_id=optional_text(row["chunk_id"]),
        excerpt=optional_text(row["excerpt"]),
        citation_label=optional_text(row["citation_label"]),
        start_line=cast(int | None, row["start_line"]),
        end_line=cast(int | None, row["end_line"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def row_to_vector_ref_record(row: sqlite3.Row) -> VectorRefRecord:
    return VectorRefRecord(
        vector_ref_id=str(row["vector_ref_id"]),
        object_type=cast(
            Literal["chunk", "entity", "evidence"],
            str(row["object_type"]),
        ),
        object_id=str(row["object_id"]),
        chunk_id=optional_text(row["chunk_id"]),
        embedding_model_version=str(row["embedding_model_version"]),
        content_hash=str(row["content_hash"]),
        source_scope=str(row["source_scope"]),
        profile_id=str(row["profile_id"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def row_to_job_record(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=str(row["job_id"]),
        job_type=str(row["job_type"]),
        status=str(row["status"]),
        write_target=cast(Literal["overlay", "baseline"], str(row["write_target"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        snapshot_id=optional_text(row["snapshot_id"]),
        profile_id=optional_text(row["profile_id"]),
        error_summary=optional_text(row["error_summary"]),
        metadata=decode_metadata(row["metadata_json"]),
    )


def fetch_file_record(connection: sqlite3.Connection, file_id: str) -> FileRecord | None:
    row = connection.execute(
        "SELECT * FROM file WHERE file_id = ? LIMIT 1",
        (file_id,),
    ).fetchone()
    if row is None:
        return None
    return row_to_file_record(cast(sqlite3.Row, row))


def upsert_row(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    columns = tuple(values.keys())
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in columns
        if column != primary_key_for_table(table)
    )
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({primary_key_for_table(table)}) DO UPDATE SET {updates}"
    )
    connection.execute(sql, tuple(values[column] for column in columns))


def delete_fts_rows(connection: sqlite3.Connection, table: StorageFTSTable, object_id: str) -> None:
    connection.execute(f"DELETE FROM {table} WHERE object_id = ?", (object_id,))


def insert_fts_row(
    connection: sqlite3.Connection,
    table: StorageFTSTable,
    values: dict[str, Any],
) -> None:
    columns = tuple(values.keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    connection.execute(sql, tuple(values[column] for column in columns))


def sync_chunk_fts(
    connection: sqlite3.Connection,
    record: ChunkRecord,
    file_record: FileRecord | None,
) -> None:
    base_row = chunk_fts_values(record, file_record)
    for table in ("chunk_fts", "doc_fts", "code_fts"):
        delete_fts_rows(connection, table, record.chunk_id)
    insert_fts_row(connection, "chunk_fts", base_row)
    if is_doc_chunk(record, file_record):
        insert_fts_row(connection, "doc_fts", doc_fts_values(record, file_record, base_row))
    if is_code_chunk(record, file_record):
        insert_fts_row(connection, "code_fts", code_fts_values(record, file_record, base_row))


def sync_entity_fts(
    connection: sqlite3.Connection,
    record: EntityRecord,
    file_record: FileRecord | None,
) -> None:
    delete_fts_rows(connection, "entity_fts", record.entity_id)
    insert_fts_row(connection, "entity_fts", entity_fts_values(record, file_record))


def sync_chunk_embedding_ref(
    connection: sqlite3.Connection,
    record: VectorRefRecord,
) -> None:
    if record.object_type != "chunk":
        return
    chunk_id = record.chunk_id or record.object_id
    row = connection.execute(
        "SELECT metadata_json FROM chunk WHERE chunk_id = ? LIMIT 1",
        (chunk_id,),
    ).fetchone()
    if row is None:
        return
    metadata = decode_metadata(row["metadata_json"])
    metadata["embedding_ref"] = record.vector_ref_id
    metadata["embedding_model_version"] = record.embedding_model_version
    metadata["embedding_content_hash"] = record.content_hash
    connection.execute(
        "UPDATE chunk SET metadata_json = ? WHERE chunk_id = ?",
        (encode_metadata(metadata), chunk_id),
    )


def is_doc_chunk(record: ChunkRecord, file_record: FileRecord | None) -> bool:
    if metadata_text(record.metadata, "doc_type") is not None:
        return True
    if record.chunk_type.startswith("doc"):
        return True
    return file_record is not None and (file_record.language or "").lower() in {
        "md",
        "markdown",
        "rst",
        "txt",
        "html",
    }


def is_code_chunk(record: ChunkRecord, file_record: FileRecord | None) -> bool:
    if record.chunk_type.startswith("code"):
        return True
    if metadata_text_list(record.metadata, "symbol_names") is not None:
        return True
    if metadata_text(record.metadata, "code_text") is not None:
        return True
    return file_record is not None and (file_record.language or "").lower() in {
        "c",
        "cpp",
        "cc",
        "h",
        "hpp",
        "py",
        "ts",
        "js",
        "java",
        "go",
        "rs",
    }


def chunk_fts_values(record: ChunkRecord, file_record: FileRecord | None) -> dict[str, Any]:
    file_metadata = {} if file_record is None else file_record.metadata
    domain = metadata_text(record.metadata, "domain") or metadata_text(file_metadata, "domain")
    doc_type = metadata_text(record.metadata, "doc_type") or metadata_text(
        file_metadata,
        "doc_type",
    )
    title = (
        metadata_text(record.metadata, "title")
        or metadata_text(file_metadata, "title")
        or (file_record.relative_path if file_record is not None else None)
        or record.chunk_type
    )
    return {
        "object_id": record.chunk_id,
        "file_id": record.file_id,
        "snapshot_id": record.snapshot_id,
        "profile_id": record.profile_id,
        "source_scope": record.source_scope,
        "chunk_type": record.chunk_type,
        "rel_path": file_record.relative_path if file_record is not None else None,
        "domain": domain,
        "doc_type": doc_type,
        "title": title,
        "text": record.text,
        "symbols": metadata_text_list(record.metadata, "symbols")
        or metadata_text_list(record.metadata, "symbol_names")
        or metadata_text_list(record.metadata, "code_symbols"),
        "tags": metadata_text_list(record.metadata, "tags"),
    }


def doc_fts_values(
    record: ChunkRecord,
    file_record: FileRecord | None,
    base_row: dict[str, Any],
) -> dict[str, Any]:
    del record, file_record
    return {
        "object_id": str(base_row["object_id"]),
        "file_id": base_row["file_id"],
        "snapshot_id": str(base_row["snapshot_id"]),
        "profile_id": str(base_row["profile_id"]),
        "source_scope": str(base_row["source_scope"]),
        "rel_path": base_row["rel_path"],
        "domain": base_row["domain"],
        "doc_type": base_row["doc_type"] or "doc",
        "title": base_row["title"],
        "text": base_row["text"],
    }


def code_fts_values(
    record: ChunkRecord,
    file_record: FileRecord | None,
    base_row: dict[str, Any],
) -> dict[str, Any]:
    del file_record
    return {
        "object_id": str(base_row["object_id"]),
        "file_id": base_row["file_id"],
        "snapshot_id": str(base_row["snapshot_id"]),
        "profile_id": str(base_row["profile_id"]),
        "source_scope": str(base_row["source_scope"]),
        "chunk_type": record.chunk_type,
        "rel_path": base_row["rel_path"],
        "domain": base_row["domain"],
        "doc_type": base_row["doc_type"],
        "symbol_names": base_row["symbols"],
        "comments": metadata_text(record.metadata, "comments"),
        "code_text": metadata_text(record.metadata, "code_text") or record.text,
    }


def entity_fts_values(record: EntityRecord, file_record: FileRecord | None) -> dict[str, Any]:
    file_metadata = {} if file_record is None else file_record.metadata
    domain = metadata_text(record.metadata, "domain") or metadata_text(file_metadata, "domain")
    doc_type = metadata_text(record.metadata, "doc_type") or metadata_text(
        file_metadata,
        "doc_type",
    )
    return {
        "object_id": record.entity_id,
        "file_id": record.file_id,
        "snapshot_id": record.snapshot_id,
        "profile_id": record.profile_id,
        "source_scope": record.source_scope,
        "entity_type": record.entity_type,
        "rel_path": file_record.relative_path if file_record is not None else None,
        "domain": domain,
        "doc_type": doc_type,
        "name": record.name,
        "qualified_name": record.qualified_name,
        "aliases": metadata_text_list(record.metadata, "aliases"),
        "summary": metadata_text(record.metadata, "summary") or record.path,
    }


def source_record_values(record: SourceRecord) -> dict[str, Any]:
    return {
        "source_id": record.source_id,
        "source_type": record.source_type,
        "display_name": record.display_name,
        "root_path": record.root_path,
        "revision": record.revision,
        "metadata_json": encode_metadata(record.metadata),
    }


def snapshot_record_values(record: SnapshotRecord) -> dict[str, Any]:
    return {
        "snapshot_id": record.snapshot_id,
        "workspace_revision": record.workspace_revision,
        "baseline_id": record.baseline_id,
        "manifest_version": record.manifest_version,
        "created_at": record.created_at,
        "metadata_json": encode_metadata(record.metadata),
    }


def profile_record_values(record: ProfileRecord) -> dict[str, Any]:
    return {
        "profile_record_id": record.profile_record_id,
        "snapshot_id": record.snapshot_id,
        "profile_id": record.profile_id,
        "defconfig_hash": record.defconfig_hash,
        "dotconfig_hash": record.dotconfig_hash,
        "defconfig_path": record.defconfig_path,
        "dotconfig_path": record.dotconfig_path,
        "app": record.app,
        "board": record.board,
        "metadata_json": encode_metadata(record.metadata),
    }


def file_record_values(record: FileRecord) -> dict[str, Any]:
    return {
        "file_id": record.file_id,
        "snapshot_id": record.snapshot_id,
        "source_id": record.source_id,
        "relative_path": record.relative_path,
        "content_hash": record.content_hash,
        "source_scope": record.source_scope,
        "profile_id": record.profile_id,
        "language": record.language,
        "metadata_json": encode_metadata(record.metadata),
    }


def chunk_record_values(record: ChunkRecord) -> dict[str, Any]:
    return {
        "chunk_id": record.chunk_id,
        "snapshot_id": record.snapshot_id,
        "file_id": record.file_id,
        "content_hash": record.content_hash,
        "chunk_type": record.chunk_type,
        "ordinal": record.ordinal,
        "text": record.text,
        "source_scope": record.source_scope,
        "profile_id": record.profile_id,
        "start_line": record.start_line,
        "end_line": record.end_line,
        "metadata_json": encode_metadata(record.metadata),
    }


def entity_record_values(record: EntityRecord) -> dict[str, Any]:
    return {
        "entity_id": record.entity_id,
        "snapshot_id": record.snapshot_id,
        "file_id": record.file_id,
        "entity_type": record.entity_type,
        "name": record.name,
        "qualified_name": record.qualified_name,
        "path": record.path,
        "source_scope": record.source_scope,
        "profile_id": record.profile_id,
        "start_line": record.start_line,
        "end_line": record.end_line,
        "metadata_json": encode_metadata(record.metadata),
    }


def relation_record_values(record: RelationRecord) -> dict[str, Any]:
    return {
        "relation_id": record.relation_id,
        "snapshot_id": record.snapshot_id,
        "relation_type": record.relation_type,
        "src_entity_id": record.src_entity_id,
        "dst_entity_id": record.dst_entity_id,
        "source_scope": record.source_scope,
        "profile_id": record.profile_id,
        "metadata_json": encode_metadata(record.metadata),
    }


def evidence_record_values(record: EvidenceRecord) -> dict[str, Any]:
    return {
        "evidence_id": record.evidence_id,
        "snapshot_id": record.snapshot_id,
        "object_type": record.object_type,
        "object_id": record.object_id,
        "file_id": record.file_id,
        "source_scope": record.source_scope,
        "profile_id": record.profile_id,
        "chunk_id": record.chunk_id,
        "excerpt": record.excerpt,
        "citation_label": record.citation_label,
        "start_line": record.start_line,
        "end_line": record.end_line,
        "metadata_json": encode_metadata(record.metadata),
    }


def vector_ref_record_values(record: VectorRefRecord) -> dict[str, Any]:
    return {
        "vector_ref_id": record.vector_ref_id,
        "object_type": record.object_type,
        "object_id": record.object_id,
        "chunk_id": record.chunk_id,
        "embedding_model_version": record.embedding_model_version,
        "content_hash": record.content_hash,
        "source_scope": record.source_scope,
        "profile_id": record.profile_id,
        "metadata_json": encode_metadata(record.metadata),
    }


def job_record_values(record: JobRecord) -> dict[str, Any]:
    return {
        "job_id": record.job_id,
        "job_type": record.job_type,
        "status": record.status,
        "write_target": record.write_target,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "snapshot_id": record.snapshot_id,
        "profile_id": record.profile_id,
        "error_summary": record.error_summary,
        "metadata_json": encode_metadata(record.metadata),
    }


def tombstone_record_values(record: TombstoneRecord) -> dict[str, Any]:
    return {
        "tombstone_id": record.tombstone_id,
        "object_type": record.object_type,
        "object_id": record.object_id,
        "baseline_id": record.baseline_id,
        "snapshot_id": record.snapshot_id,
        "profile_id": record.profile_id,
        "source_scope": record.source_scope,
        "reason": record.reason,
        "created_by_job": record.created_by_job,
        "created_at": record.created_at,
        "active": 1 if record.active else 0,
        "metadata_json": encode_metadata(record.metadata),
    }


def replacement_record_values(record: ReplacementRecord) -> dict[str, Any]:
    return {
        "replacement_id": record.replacement_id,
        "object_type": record.object_type,
        "old_object_id": record.old_object_id,
        "new_object_id": record.new_object_id,
        "baseline_id": record.baseline_id,
        "snapshot_id": record.scope.snapshot_id,
        "profile_id": record.scope.profile_id,
        "source_scope": record.scope.source_scope,
        "path_scope": record.scope.path_scope,
        "reason": record.reason,
        "created_by_job": record.created_by_job,
        "created_at": record.created_at,
        "active": 1 if record.active else 0,
        "metadata_json": encode_metadata(record.metadata),
    }
