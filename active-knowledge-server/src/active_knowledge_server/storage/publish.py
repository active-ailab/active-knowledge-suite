"""Atomic publish-pointer helpers for staged full-index artifacts."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from active_knowledge_server.storage.base import StorageMetadata, StorageWriteTarget

PUBLISH_POINTER_SCHEMA_VERSION = "index_publish_pointer.v1"


@dataclass(frozen=True)
class PublishedStoragePaths:
    """Published metadata/vector artifact locations for one full-index job."""

    schema_version: str
    target: StorageWriteTarget
    job_id: str
    publish_token: str
    manifest_path: Path
    metadata_anchor_path: Path
    vector_anchor_path: Path
    metadata_path: Path
    vector_path: Path

    def to_dict(self) -> StorageMetadata:
        return {
            "schema_version": self.schema_version,
            "target": self.target,
            "job_id": self.job_id,
            "publish_token": self.publish_token,
            "manifest_path": str(self.manifest_path),
            "metadata_anchor_path": str(self.metadata_anchor_path),
            "vector_anchor_path": str(self.vector_anchor_path),
            "metadata_path": str(self.metadata_path),
            "vector_path": str(self.vector_path),
        }


def publish_manifest_path(metadata_anchor_path: Path) -> Path:
    """Return the stable manifest path that atomically selects the live version."""

    return metadata_anchor_path.with_name(f"{metadata_anchor_path.name}.publish.json")


def resolve_published_storage_paths(
    *,
    metadata_anchor_path: Path,
    vector_anchor_path: Path,
) -> tuple[Path, Path]:
    """Resolve the current live metadata/vector paths via the publish manifest when present."""

    payload = read_publish_manifest(publish_manifest_path(metadata_anchor_path))
    if payload is None:
        return metadata_anchor_path, vector_anchor_path
    metadata_path = _published_path_from_payload(
        payload,
        key="metadata_path",
        default=metadata_anchor_path,
    )
    vector_path = _published_path_from_payload(
        payload,
        key="vector_path",
        default=vector_anchor_path,
    )
    return metadata_path, vector_path


def resolve_published_storage_for_job(
    *,
    target: StorageWriteTarget,
    job_id: str,
    metadata_anchor_path: Path,
    vector_anchor_path: Path,
    publish_token: str,
) -> PublishedStoragePaths:
    """Return the deterministic published artifact locations for one job."""

    metadata_suffix = "".join(metadata_anchor_path.suffixes)
    return PublishedStoragePaths(
        schema_version=PUBLISH_POINTER_SCHEMA_VERSION,
        target=target,
        job_id=job_id,
        publish_token=publish_token,
        manifest_path=publish_manifest_path(metadata_anchor_path),
        metadata_anchor_path=metadata_anchor_path,
        vector_anchor_path=vector_anchor_path,
        metadata_path=metadata_anchor_path.parent
        / f"{metadata_anchor_path.name}.versions"
        / f"{publish_token}{metadata_suffix}",
        vector_path=vector_anchor_path.parent / f"{vector_anchor_path.name}.versions" / publish_token,
    )


def materialize_published_storage(
    *,
    staging_metadata_path: Path,
    staging_vector_path: Path,
    published: PublishedStoragePaths,
) -> None:
    """Move staged artifacts into deterministic published version paths."""

    _materialize_metadata(
        staging_metadata_path=staging_metadata_path,
        published_metadata_path=published.metadata_path,
    )
    _materialize_vector_dir(
        staging_vector_path=staging_vector_path,
        published_vector_path=published.vector_path,
    )


def activate_published_storage(published: PublishedStoragePaths) -> None:
    """Atomically switch readers to the published metadata/vector artifacts."""

    payload = {
        "schema_version": published.schema_version,
        "target": published.target,
        "job_id": published.job_id,
        "publish_token": published.publish_token,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "metadata_anchor_path": str(published.metadata_anchor_path),
        "vector_anchor_path": str(published.vector_anchor_path),
        "metadata_path": os.path.relpath(
            published.metadata_path,
            start=published.manifest_path.parent,
        ),
        "vector_path": os.path.relpath(
            published.vector_path,
            start=published.manifest_path.parent,
        ),
    }
    _atomic_write_text(
        published.manifest_path,
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
    )


def read_publish_manifest(path: Path) -> dict[str, Any] | None:
    """Read one publish manifest when it exists and is well formed."""

    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _published_path_from_payload(
    payload: dict[str, Any],
    *,
    key: str,
    default: Path,
) -> Path:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw:
        return default
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return (publish_manifest_path(default).parent / candidate).resolve()


def _materialize_metadata(
    *,
    staging_metadata_path: Path,
    published_metadata_path: Path,
) -> None:
    published_metadata_path.parent.mkdir(parents=True, exist_ok=True)
    if staging_metadata_path.exists():
        os.replace(staging_metadata_path, published_metadata_path)
    elif not published_metadata_path.exists():
        raise FileNotFoundError(
            f"staging metadata {staging_metadata_path} is missing and "
            f"{published_metadata_path} was not already materialized"
        )


def _materialize_vector_dir(
    *,
    staging_vector_path: Path,
    published_vector_path: Path,
) -> None:
    published_vector_path.parent.mkdir(parents=True, exist_ok=True)
    if staging_vector_path.exists():
        if published_vector_path.exists():
            shutil.rmtree(published_vector_path)
        os.replace(staging_vector_path, published_vector_path)
        return
    if published_vector_path.exists():
        return


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
