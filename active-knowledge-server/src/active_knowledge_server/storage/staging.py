"""Deterministic staging-path resolution for full index builds."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage.base import StorageMetadata, StorageWriteTarget
from active_knowledge_server.storage.lancedb_store import configured_lancedb_paths
from active_knowledge_server.storage.sqlite_store import configured_sqlite_paths

STAGING_STORAGE_SCHEMA_VERSION = "index_staging_storage.v1"
_MAX_JOB_TOKEN_PREFIX_LENGTH = 48
_JOB_TOKEN_INVALID_CHARS = re.compile(r"[^a-z0-9._-]+")


@dataclass(frozen=True)
class ResolvedStoragePaths:
    """One pair of metadata/vector paths for a live or staging target."""

    metadata_path: Path
    vector_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "metadata_path": str(self.metadata_path),
            "vector_path": str(self.vector_path),
        }


@dataclass(frozen=True)
class StagingStoragePaths:
    """Resolved live and staging paths for one full-index job."""

    schema_version: str
    target: StorageWriteTarget
    job_id: str
    job_token: str
    live: ResolvedStoragePaths
    staging: ResolvedStoragePaths

    def to_dict(self) -> StorageMetadata:
        return {
            "schema_version": self.schema_version,
            "target": self.target,
            "job_id": self.job_id,
            "job_token": self.job_token,
            "live": self.live.to_dict(),
            "staging": self.staging.to_dict(),
        }


def resolve_live_storage_paths(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
    target: StorageWriteTarget,
) -> ResolvedStoragePaths:
    """Return the configured live metadata/vector paths for one write target."""

    sqlite_paths = configured_sqlite_paths(config, cwd=cwd, follow_publish_pointer=False)
    vector_paths = configured_lancedb_paths(config, cwd=cwd, follow_publish_pointer=False)
    if target == "baseline":
        return ResolvedStoragePaths(
            metadata_path=sqlite_paths["baseline_metadata"],
            vector_path=vector_paths["baseline"],
        )
    return ResolvedStoragePaths(
        metadata_path=sqlite_paths["overlay_metadata"],
        vector_path=vector_paths["overlay"],
    )


def resolve_staging_storage_paths(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
    target: StorageWriteTarget,
    job_id: str,
) -> StagingStoragePaths:
    """Derive stable staging paths for one full-index job."""

    live = resolve_live_storage_paths(config, cwd=cwd, target=target)
    job_token = staging_job_token(job_id)
    return StagingStoragePaths(
        schema_version=STAGING_STORAGE_SCHEMA_VERSION,
        target=target,
        job_id=job_id,
        job_token=job_token,
        live=live,
        staging=ResolvedStoragePaths(
            metadata_path=_with_staging_file_suffix(live.metadata_path, job_token),
            vector_path=_with_staging_dir_suffix(live.vector_path, job_token),
        ),
    )


def staging_job_token(job_id: str) -> str:
    """Return a filesystem-safe, deterministic token for one raw job id."""

    raw = job_id.strip().lower()
    if not raw:
        raise ValueError("job_id must not be empty")
    normalized = _JOB_TOKEN_INVALID_CHARS.sub("-", raw).strip(".-")
    if not normalized:
        normalized = "job"
    prefix = normalized[:_MAX_JOB_TOKEN_PREFIX_LENGTH]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _with_staging_file_suffix(path: Path, job_token: str) -> Path:
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    return path.with_name(f"{stem}.staging.{job_token}{suffix}")


def _with_staging_dir_suffix(path: Path, job_token: str) -> Path:
    return path.with_name(f"{path.name}.staging.{job_token}")
