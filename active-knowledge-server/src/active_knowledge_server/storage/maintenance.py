"""Safe local clean and overlay compact operations."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage.base import StorageMetadata
from active_knowledge_server.storage.publish import (
    current_publish_token,
    published_metadata_versions_dir,
    published_vector_versions_dir,
)
from active_knowledge_server.storage.sqlite_store import (
    configured_sqlite_paths,
    decode_metadata,
    fetch_file_record,
    row_to_chunk_record,
    row_to_entity_record,
    sqlite_connection,
    sync_chunk_fts,
    sync_entity_fts,
    table_exists,
)
from active_knowledge_server.storage.staging import resolve_live_storage_paths
from active_knowledge_server.storage.validation import (
    StorageValidationReport,
    validate_storage_consistency,
)

TERMINAL_JOB_STATUSES = ("ready", "failed", "partial_ready")
STALE_STAGING_JOB_STATUSES = ("failed", "partial_ready")


@dataclass(frozen=True)
class CleanReport:
    """Machine-readable report for one clean/compact run."""

    schema_version: str
    cleaned_paths: tuple[str, ...] = ()
    deleted_files: int = 0
    deleted_dirs: int = 0
    deleted_jobs: int = 0
    deleted_snapshots: int = 0
    deleted_staging_artifacts: int = 0
    deleted_live_versions: int = 0
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
            "deleted_staging_artifacts": self.deleted_staging_artifacts,
            "deleted_live_versions": self.deleted_live_versions,
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
    deleted_staging_artifacts: int = 0
    deleted_live_versions: int = 0
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
            deleted_staging_artifacts=self.deleted_staging_artifacts,
            deleted_live_versions=self.deleted_live_versions,
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
    clean_staging_jobs: bool = False,
    live_versions_keep: int | None = None,
    compact_overlay: bool = False,
) -> CleanReport:
    """Clean selected runtime maintenance targets while preserving active publish versions."""

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

    if clean_staging_jobs:
        result = clean_stale_staging_jobs(sqlite_paths["jobs"])
        acc.deleted_files += result.deleted_files
        acc.deleted_dirs += result.deleted_dirs
        acc.deleted_staging_artifacts += result.deleted_files + result.deleted_dirs

    if live_versions_keep is not None:
        for target in ("baseline", "overlay"):
            live = resolve_live_storage_paths(config, cwd=cwd, target=target)
            result = clean_old_published_versions(
                metadata_anchor_path=live.metadata_path,
                vector_anchor_path=live.vector_path,
                keep=live_versions_keep,
            )
            acc.deleted_files += result.deleted_files
            acc.deleted_dirs += result.deleted_dirs
            acc.deleted_live_versions += result.deleted_versions

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
    deleted_versions: int = 0


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


def clean_stale_staging_jobs(jobs_path: Path) -> DeleteResult:
    """Delete staging artifacts for failed or partial full-index jobs."""

    if not jobs_path.exists():
        return DeleteResult()
    deleted_files = 0
    deleted_dirs = 0
    statuses = tuple(STALE_STAGING_JOB_STATUSES)
    placeholders = ", ".join("?" for _ in statuses)
    with sqlite_connection(jobs_path) as connection:
        if not table_exists(connection, "job"):
            return DeleteResult()
        rows = connection.execute(
            f"""
            SELECT metadata_json
            FROM job
            WHERE job_type = 'index'
              AND status IN ({placeholders})
            ORDER BY updated_at ASC, job_id ASC
            """,
            statuses,
        ).fetchall()

    for row in rows:
        metadata = decode_metadata(row["metadata_json"])
        staging_storage = _mapping_value(metadata, "staging_storage")
        if staging_storage is None:
            continue
        live = _mapping_value(staging_storage, "live")
        staging = _mapping_value(staging_storage, "staging")
        if live is None or staging is None:
            continue
        live_metadata = _path_value(live, "metadata_path")
        live_vector = _path_value(live, "vector_path")
        staging_metadata = _path_value(staging, "metadata_path")
        staging_vector = _path_value(staging, "vector_path")
        if (
            live_metadata is not None
            and staging_metadata is not None
            and _is_expected_staging_metadata_path(
                live_path=live_metadata,
                staging_path=staging_metadata,
            )
        ):
            result = delete_path(staging_metadata)
            deleted_files += result.deleted_files
            deleted_dirs += result.deleted_dirs
            for sidecar in _sqlite_sidecar_paths(staging_metadata):
                result = delete_path(sidecar)
                deleted_files += result.deleted_files
                deleted_dirs += result.deleted_dirs
        if (
            live_vector is not None
            and staging_vector is not None
            and _is_expected_staging_vector_path(
                live_path=live_vector,
                staging_path=staging_vector,
            )
        ):
            result = delete_path(staging_vector)
            deleted_files += result.deleted_files
            deleted_dirs += result.deleted_dirs
    return DeleteResult(deleted_files=deleted_files, deleted_dirs=deleted_dirs)


def clean_old_published_versions(
    *,
    metadata_anchor_path: Path,
    vector_anchor_path: Path,
    keep: int,
) -> DeleteResult:
    """Prune old published versions while always preserving the active pointer token."""

    if keep < 0:
        raise ValueError("keep must be >= 0")
    candidates = _published_version_candidates(
        metadata_anchor_path=metadata_anchor_path,
        vector_anchor_path=vector_anchor_path,
    )
    if not candidates:
        return DeleteResult()

    current = current_publish_token(metadata_anchor_path)
    ordered = sorted(
        candidates.items(),
        key=lambda item: (item[1].sort_mtime, item[0]),
        reverse=True,
    )
    retain_tokens = {token for token, _candidate in ordered[:keep]}
    if current is not None:
        retain_tokens.add(current)

    deleted_files = 0
    deleted_dirs = 0
    deleted_versions = 0
    for token, candidate in ordered:
        if token in retain_tokens:
            continue
        deleted_this_version = False
        for path in (candidate.metadata_path, candidate.vector_path):
            if path is None:
                continue
            result = delete_path(path)
            deleted_files += result.deleted_files
            deleted_dirs += result.deleted_dirs
            deleted_this_version = deleted_this_version or bool(
                result.deleted_files or result.deleted_dirs
            )
        if deleted_this_version:
            deleted_versions += 1
    return DeleteResult(
        deleted_files=deleted_files,
        deleted_dirs=deleted_dirs,
        deleted_versions=deleted_versions,
    )


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


@dataclass(frozen=True)
class _PublishedVersionCandidate:
    token: str
    sort_mtime: float
    metadata_path: Path | None = None
    vector_path: Path | None = None


def delete_path(path: Path) -> DeleteResult:
    """Delete one file or directory and report what was actually removed."""

    if not path.exists():
        return DeleteResult()
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return DeleteResult(deleted_dirs=1)
    path.unlink()
    return DeleteResult(deleted_files=1)


def _mapping_value(value: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        return None
    return cast(Mapping[str, Any], nested)


def _path_value(value: Mapping[str, Any], key: str) -> Path | None:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw)


def _sqlite_sidecar_paths(path: Path) -> tuple[Path, Path]:
    return Path(f"{path}-wal"), Path(f"{path}-shm")


def _is_expected_staging_metadata_path(*, live_path: Path, staging_path: Path) -> bool:
    suffix = "".join(live_path.suffixes)
    live_stem = live_path.name[: -len(suffix)] if suffix else live_path.name
    return (
        staging_path.parent.resolve() == live_path.parent.resolve()
        and staging_path.name.startswith(f"{live_stem}.staging.")
        and (not suffix or staging_path.name.endswith(suffix))
    )


def _is_expected_staging_vector_path(*, live_path: Path, staging_path: Path) -> bool:
    return (
        staging_path.parent.resolve() == live_path.parent.resolve()
        and staging_path.name.startswith(f"{live_path.name}.staging.")
    )


def _published_version_candidates(
    *,
    metadata_anchor_path: Path,
    vector_anchor_path: Path,
) -> dict[str, _PublishedVersionCandidate]:
    candidates: dict[str, _PublishedVersionCandidate] = {}
    metadata_dir = published_metadata_versions_dir(metadata_anchor_path)
    metadata_suffix = "".join(metadata_anchor_path.suffixes)
    if metadata_dir.is_dir():
        for child in metadata_dir.iterdir():
            if child.is_dir():
                continue
            token = _metadata_version_token(child, metadata_suffix=metadata_suffix)
            if token is None:
                continue
            candidates[token] = _merge_published_version_candidate(
                candidates.get(token),
                token=token,
                metadata_path=child,
            )

    vector_dir = published_vector_versions_dir(vector_anchor_path)
    if vector_dir.is_dir():
        for child in vector_dir.iterdir():
            if not child.is_dir():
                continue
            token = child.name
            candidates[token] = _merge_published_version_candidate(
                candidates.get(token),
                token=token,
                vector_path=child,
            )
    return candidates


def _metadata_version_token(path: Path, *, metadata_suffix: str) -> str | None:
    name = path.name
    if metadata_suffix:
        if not name.endswith(metadata_suffix):
            return None
        token = name[: -len(metadata_suffix)]
    else:
        token = name
    return token or None


def _merge_published_version_candidate(
    existing: _PublishedVersionCandidate | None,
    *,
    token: str,
    metadata_path: Path | None = None,
    vector_path: Path | None = None,
) -> _PublishedVersionCandidate:
    paths = tuple(path for path in (metadata_path, vector_path) if path is not None)
    sort_mtime = max((path.stat().st_mtime for path in paths if path.exists()), default=0.0)
    if existing is None:
        return _PublishedVersionCandidate(
            token=token,
            sort_mtime=sort_mtime,
            metadata_path=metadata_path,
            vector_path=vector_path,
        )
    return _PublishedVersionCandidate(
        token=token,
        sort_mtime=max(existing.sort_mtime, sort_mtime),
        metadata_path=metadata_path or existing.metadata_path,
        vector_path=vector_path or existing.vector_path,
    )


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
