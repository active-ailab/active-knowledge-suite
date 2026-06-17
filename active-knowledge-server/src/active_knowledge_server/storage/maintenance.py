"""Safe local clean and overlay compact operations."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage.base import StorageMetadata
from active_knowledge_server.storage.sqlite_store import (
    configured_sqlite_paths,
    fetch_file_record,
    row_to_chunk_record,
    row_to_entity_record,
    sqlite_connection,
    sync_chunk_fts,
    sync_entity_fts,
    table_exists,
)
from active_knowledge_server.storage.validation import (
    StorageValidationReport,
    validate_storage_consistency,
)

TERMINAL_JOB_STATUSES = ("ready", "failed", "partial_ready")


@dataclass(frozen=True)
class CleanReport:
    """Machine-readable report for one clean/compact run."""

    schema_version: str
    cleaned_paths: tuple[str, ...] = ()
    deleted_files: int = 0
    deleted_dirs: int = 0
    deleted_jobs: int = 0
    deleted_snapshots: int = 0
    compact: StorageMetadata = field(default_factory=dict)
    validation_before: StorageMetadata | None = None
    validation_after: StorageMetadata | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "cleaned_paths": list(self.cleaned_paths),
            "deleted_files": self.deleted_files,
            "deleted_dirs": self.deleted_dirs,
            "deleted_jobs": self.deleted_jobs,
            "deleted_snapshots": self.deleted_snapshots,
            "compact": self.compact,
            "validation_before": self.validation_before,
            "validation_after": self.validation_after,
        }


@dataclass
class _CleanAccumulator:
    cleaned_paths: list[str] = field(default_factory=list)
    deleted_files: int = 0
    deleted_dirs: int = 0
    deleted_jobs: int = 0
    deleted_snapshots: int = 0
    compact: StorageMetadata = field(default_factory=dict)
    validation_before: StorageMetadata | None = None
    validation_after: StorageMetadata | None = None

    def report(self) -> CleanReport:
        return CleanReport(
            schema_version="clean_report.v1",
            cleaned_paths=tuple(self.cleaned_paths),
            deleted_files=self.deleted_files,
            deleted_dirs=self.deleted_dirs,
            deleted_jobs=self.deleted_jobs,
            deleted_snapshots=self.deleted_snapshots,
            compact=self.compact,
            validation_before=self.validation_before,
            validation_after=self.validation_after,
        )


def clean_local_state(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
    clean_cache: bool = False,
    clean_tmp: bool = False,
    old_jobs_keep: int | None = None,
    old_snapshots_keep: int | None = None,
    compact_overlay: bool = False,
) -> CleanReport:
    """Clean local runtime state without deleting baseline assets."""

    acc = _CleanAccumulator()
    baseline_dir = resolve_runtime_path(config.runtime.baseline_dir, cwd)
    local_dir = resolve_runtime_path(config.runtime.local_dir, cwd)
    local_artifacts_root = resolve_runtime_path(config.storage.local_artifacts_root, cwd)
    sqlite_paths = configured_sqlite_paths(config, cwd=cwd)

    if clean_cache:
        cache_path = resolve_runtime_path(config.storage.cache_root, cwd)
        result = clean_directory_contents(
            cache_path,
            local_dir=local_dir,
            baseline_dir=baseline_dir,
        )
        acc.cleaned_paths.append(str(cache_path))
        acc.deleted_files += result.deleted_files
        acc.deleted_dirs += result.deleted_dirs

    if clean_tmp:
        tmp_path = local_dir / "tmp"
        result = clean_directory_contents(
            tmp_path,
            local_dir=local_dir,
            baseline_dir=baseline_dir,
        )
        acc.cleaned_paths.append(str(tmp_path))
        acc.deleted_files += result.deleted_files
        acc.deleted_dirs += result.deleted_dirs

    if old_jobs_keep is not None:
        acc.deleted_jobs += clean_old_jobs(
            sqlite_paths["jobs"],
            keep=old_jobs_keep,
            artifacts_root=local_artifacts_root / "index-jobs",
        )

    if old_snapshots_keep is not None:
        acc.deleted_snapshots += clean_old_overlay_snapshots(
            sqlite_paths["overlay_metadata"],
            keep=old_snapshots_keep,
        )

    if compact_overlay:
        before = validate_storage_consistency(config, cwd=cwd)
        acc.validation_before = validation_summary(before)
        acc.compact = compact_overlay_store(sqlite_paths["overlay_metadata"])
        after = validate_storage_consistency(config, cwd=cwd)
        acc.validation_after = validation_summary(after)

    return acc.report()


@dataclass(frozen=True)
class DeleteResult:
    deleted_files: int = 0
    deleted_dirs: int = 0


def clean_directory_contents(path: Path, *, local_dir: Path, baseline_dir: Path) -> DeleteResult:
    """Delete children of a local runtime directory while preserving the directory itself."""

    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return DeleteResult()
    assert_clean_path(path, local_dir=local_dir, baseline_dir=baseline_dir)
    deleted_files = 0
    deleted_dirs = 0
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
            deleted_dirs += 1
        else:
            child.unlink()
            deleted_files += 1
    return DeleteResult(deleted_files=deleted_files, deleted_dirs=deleted_dirs)


def assert_clean_path(path: Path, *, local_dir: Path, baseline_dir: Path) -> None:
    resolved = path.resolve()
    resolved_local = local_dir.resolve()
    resolved_baseline = baseline_dir.resolve()
    if resolved == resolved_baseline or resolved_baseline in resolved.parents:
        raise ValueError(f"clean refuses to delete baseline path: {path}")
    if resolved != resolved_local and resolved_local not in resolved.parents:
        raise ValueError(f"clean path must be under local runtime dir: {path}")


def clean_old_jobs(
    jobs_path: Path,
    *,
    keep: int,
    artifacts_root: Path | None = None,
) -> int:
    """Delete old terminal jobs and their checkpoints, preserving active jobs."""

    if keep < 0:
        raise ValueError("keep must be >= 0")
    if not jobs_path.exists():
        return 0
    terminal_statuses = tuple(TERMINAL_JOB_STATUSES)
    placeholders = ", ".join("?" for _ in terminal_statuses)
    with sqlite_connection(jobs_path) as connection:
        if not table_exists(connection, "job"):
            return 0
        rows = connection.execute(
            f"""
            SELECT job_id
            FROM job
            WHERE status IN ({placeholders})
            ORDER BY updated_at DESC, job_id DESC
            """,
            terminal_statuses,
        ).fetchall()
        job_ids = [str(row["job_id"]) for row in rows]
        delete_ids = job_ids[keep:]
        for job_id in delete_ids:
            connection.execute("DELETE FROM job_checkpoint WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM job WHERE job_id = ?", (job_id,))
        connection.commit()
    if artifacts_root is not None:
        for job_id in delete_ids:
            artifact_path = artifacts_root / job_id
            if artifact_path.is_dir():
                shutil.rmtree(artifact_path)
            elif artifact_path.exists():
                artifact_path.unlink()
    return len(delete_ids)


def clean_old_overlay_snapshots(overlay_path: Path, *, keep: int) -> int:
    """Delete old overlay snapshot-scoped rows while leaving baseline untouched."""

    if keep < 0:
        raise ValueError("keep must be >= 0")
    if not overlay_path.exists():
        return 0
    with sqlite_connection(overlay_path) as connection:
        if not table_exists(connection, "snapshot"):
            return 0
        rows = connection.execute(
            """
            SELECT snapshot_id
            FROM snapshot
            ORDER BY COALESCE(created_at, snapshot_id) DESC, snapshot_id DESC
            """
        ).fetchall()
        snapshot_ids = [str(row["snapshot_id"]) for row in rows]
        delete_ids = snapshot_ids[keep:]
        for snapshot_id in delete_ids:
            delete_overlay_snapshot(connection, snapshot_id)
        connection.commit()
    return len(delete_ids)


def delete_overlay_snapshot(connection: sqlite3.Connection, snapshot_id: str) -> None:
    chunk_ids = ids_for_snapshot(connection, "chunk", "chunk_id", snapshot_id)
    entity_ids = ids_for_snapshot(connection, "entity", "entity_id", snapshot_id)
    evidence_ids = ids_for_snapshot(connection, "evidence", "evidence_id", snapshot_id)
    relation_ids = ids_for_snapshot(connection, "relation", "relation_id", snapshot_id)
    object_ids = chunk_ids | entity_ids | evidence_ids

    for table in ("chunk_fts", "doc_fts", "code_fts", "entity_fts"):
        if table_exists(connection, table):
            connection.execute(f"DELETE FROM {table} WHERE snapshot_id = ?", (snapshot_id,))

    if object_ids and table_exists(connection, "vector_ref"):
        placeholders = ", ".join("?" for _ in object_ids)
        connection.execute(
            f"DELETE FROM vector_ref WHERE object_id IN ({placeholders})",
            tuple(sorted(object_ids)),
        )
    if chunk_ids and table_exists(connection, "vector_ref"):
        placeholders = ", ".join("?" for _ in chunk_ids)
        connection.execute(
            f"DELETE FROM vector_ref WHERE chunk_id IN ({placeholders})",
            tuple(sorted(chunk_ids)),
        )

    for table in (
        "profile",
        "file",
        "chunk",
        "entity",
        "relation",
        "evidence",
        "tombstone",
        "replacement",
        "snapshot",
    ):
        if table_exists(connection, table):
            connection.execute(f"DELETE FROM {table} WHERE snapshot_id = ?", (snapshot_id,))

    if relation_ids and table_exists(connection, "tombstone"):
        placeholders = ", ".join("?" for _ in relation_ids)
        connection.execute(
            "DELETE FROM tombstone "
            f"WHERE object_type = 'relation' AND object_id IN ({placeholders})",
            tuple(sorted(relation_ids)),
        )


def ids_for_snapshot(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    snapshot_id: str,
) -> set[str]:
    if not table_exists(connection, table):
        return set()
    rows = connection.execute(
        f"SELECT {id_column} FROM {table} WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchall()
    return {str(row[id_column]) for row in rows}


def compact_overlay_store(overlay_path: Path) -> StorageMetadata:
    """Compact overlay metadata by pruning inactive control rows and rebuilding overlay FTS."""

    if not overlay_path.exists():
        return {
            "deleted_inactive_tombstones": 0,
            "deleted_inactive_replacements": 0,
            "rebuilt_fts_rows": 0,
            "vacuumed": False,
        }

    with sqlite_connection(overlay_path) as connection:
        deleted_tombstones = delete_inactive_rows(connection, "tombstone")
        deleted_replacements = delete_inactive_rows(connection, "replacement")
        rebuilt_fts_rows = rebuild_overlay_fts(connection)
        connection.commit()
    with sqlite_connection(overlay_path) as connection:
        connection.execute("VACUUM")

    return {
        "deleted_inactive_tombstones": deleted_tombstones,
        "deleted_inactive_replacements": deleted_replacements,
        "rebuilt_fts_rows": rebuilt_fts_rows,
        "vacuumed": True,
    }


def delete_inactive_rows(connection: sqlite3.Connection, table: str) -> int:
    if not table_exists(connection, table):
        return 0
    cursor = connection.execute(f"DELETE FROM {table} WHERE active = 0")
    return int(cursor.rowcount)


def rebuild_overlay_fts(connection: sqlite3.Connection) -> int:
    for table in ("chunk_fts", "doc_fts", "code_fts", "entity_fts"):
        if table_exists(connection, table):
            connection.execute(f"DELETE FROM {table}")

    rebuilt = 0
    if table_exists(connection, "chunk"):
        rows = connection.execute("SELECT * FROM chunk ORDER BY chunk_id ASC").fetchall()
        for row in rows:
            chunk = row_to_chunk_record(row)
            sync_chunk_fts(connection, chunk, fetch_file_record(connection, chunk.file_id))
            rebuilt += 1

    if table_exists(connection, "entity"):
        rows = connection.execute("SELECT * FROM entity ORDER BY entity_id ASC").fetchall()
        for row in rows:
            entity = row_to_entity_record(row)
            sync_entity_fts(connection, entity, fetch_file_record(connection, entity.file_id))
            rebuilt += 1
    return rebuilt


def validation_summary(report: StorageValidationReport) -> StorageMetadata:
    return cast(
        StorageMetadata,
        {
            "schema_version": report.schema_version,
            "status": report.status,
            "check_count": len(report.checks),
            "check_codes": sorted({check.check_code for check in report.checks}),
        },
    )
