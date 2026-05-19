"""SQLite storage schema and migration helpers."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage.base import StorageWriteRequest, validate_write_request

SQLiteTarget = Literal["baseline_metadata", "overlay_metadata", "jobs"]

LATEST_SQLITE_SCHEMA_VERSION: Final = "1.0.0"
_SCHEMA_VERSION_TABLE = "schema_version"
_MIGRATION_HISTORY_TABLE = "migration_history"


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

METADATA_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = COMMON_METADATA_STATEMENTS + (
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
        ),
    ),
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
