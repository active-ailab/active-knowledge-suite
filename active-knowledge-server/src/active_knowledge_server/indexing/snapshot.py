"""Snapshot indexing boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors import RepositoryInfo, WorkspaceConnector, WorkspaceInventory
from active_knowledge_server.storage import SnapshotRecord, StorageWriter

SNAPSHOT_COLLECTOR_SCHEMA_VERSION: Final = "snapshot_collector.v1"
CURRENT_SNAPSHOT_ID: Final = "current"
SNAPSHOT_ID_PREFIX: Final = "snapshot:"


@dataclass(frozen=True)
class CollectedSnapshot:
    """Stable snapshot materialized from one workspace inventory."""

    schema_version: str
    inventory: WorkspaceInventory
    snapshot_record: SnapshotRecord
    alias_records: tuple[SnapshotRecord, ...] = ()

    @property
    def snapshot_id(self) -> str:
        """Return the stable snapshot ID."""

        return self.snapshot_record.snapshot_id

    @property
    def current_snapshot_id(self) -> str:
        """Return the default current-snapshot alias ID."""

        for record in self.alias_records:
            if record.snapshot_id == CURRENT_SNAPSHOT_ID:
                return record.snapshot_id
        return CURRENT_SNAPSHOT_ID

    def all_records(self) -> tuple[SnapshotRecord, ...]:
        """Return the stable snapshot record plus any alias records."""

        return (self.snapshot_record, *self.alias_records)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable collected snapshot summary."""

        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "current_snapshot_id": self.current_snapshot_id,
            "workspace_revision": self.snapshot_record.workspace_revision,
            "baseline_id": self.snapshot_record.baseline_id,
            "manifest_version": self.snapshot_record.manifest_version,
            "created_at": self.snapshot_record.created_at,
            "inventory_hash": self.inventory.inventory_hash,
            "repo_manifest_hash": self.snapshot_record.metadata.get("repo_manifest_hash"),
            "git_head": self.snapshot_record.metadata.get("git_head"),
            "alias_ids": [record.snapshot_id for record in self.alias_records],
        }


class SnapshotCollector:
    """Collect reproducible snapshot records from the workspace inventory."""

    def __init__(
        self,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
        connector: WorkspaceConnector | None = None,
    ) -> None:
        self._config = config
        self._cwd = (cwd or Path.cwd()).expanduser()
        self._connector = connector or WorkspaceConnector.from_config(config, cwd=self._cwd)

    @classmethod
    def from_config(
        cls,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
        connector: WorkspaceConnector | None = None,
    ) -> SnapshotCollector:
        """Build a snapshot collector from validated config."""

        return cls(config, cwd=cwd, connector=connector)

    def collect(
        self,
        inventory: WorkspaceInventory | None = None,
        *,
        created_at: str | None = None,
    ) -> CollectedSnapshot:
        """Collect one reproducible snapshot from a workspace inventory."""

        workspace_inventory = inventory or self._connector.scan()
        baseline_id = _read_baseline_id(self._config, cwd=self._cwd)
        repo_manifest_hash = compute_repo_manifest_hash(workspace_inventory.repositories)
        workspace_revision = compute_workspace_revision(
            inventory_hash=workspace_inventory.inventory_hash,
            repo_manifest_hash=repo_manifest_hash,
        )
        snapshot_id = compute_snapshot_id(
            config=self._config,
            inventory=workspace_inventory,
            workspace_revision=workspace_revision,
            baseline_id=baseline_id,
        )
        created_at_value = created_at or _utc_now()
        git_head = root_git_head(workspace_inventory.repositories)

        metadata: dict[str, object] = {
            "status": "ready",
            "workspace_root": workspace_inventory.workspace_root,
            "workspace_display_path": workspace_inventory.workspace_display_path,
            "workspace_inventory_hash": workspace_inventory.inventory_hash,
            "workspace_inventory_schema_version": workspace_inventory.schema_version,
            "repo_manifest_hash": repo_manifest_hash,
            "git_head": git_head,
            "baseline_branch": self._config.project.baseline_branch,
            "file_count": len(workspace_inventory.files),
            "repository_count": len(workspace_inventory.repositories),
            "commit_map": workspace_inventory.commit_map,
        }
        if workspace_inventory.warnings:
            metadata["warnings"] = [warning.to_dict() for warning in workspace_inventory.warnings]

        snapshot_record = SnapshotRecord(
            snapshot_id=snapshot_id,
            workspace_revision=workspace_revision,
            baseline_id=baseline_id,
            manifest_version=SNAPSHOT_COLLECTOR_SCHEMA_VERSION,
            created_at=created_at_value,
            metadata=metadata,
        )
        alias_records = tuple(
            _make_alias_record(
                alias_id=alias_id,
                stable_record=snapshot_record,
                stable_snapshot_id=snapshot_id,
            )
            for alias_id in snapshot_aliases(self._config, stable_snapshot_id=snapshot_id)
        )
        return CollectedSnapshot(
            schema_version=SNAPSHOT_COLLECTOR_SCHEMA_VERSION,
            inventory=workspace_inventory,
            snapshot_record=snapshot_record,
            alias_records=alias_records,
        )

    def collect_and_store(
        self,
        writer: StorageWriter,
        inventory: WorkspaceInventory | None = None,
        *,
        created_at: str | None = None,
    ) -> CollectedSnapshot:
        """Collect one snapshot and persist the stable plus alias records."""

        collected = self.collect(inventory=inventory, created_at=created_at)
        for record in collected.all_records():
            writer.upsert_snapshot(record)
        return collected


def compute_repo_manifest_hash(repositories: tuple[RepositoryInfo, ...]) -> str:
    """Return a stable hash of repository commit, branch, and dirty metadata."""

    payload = [
        {
            "relative_path": repository.relative_path,
            "commit": repository.commit,
            "branch": repository.branch,
            "dirty": repository.dirty,
            "boundary_kind": repository.boundary_kind,
            "is_workspace_root": repository.is_workspace_root,
            "error": repository.error,
        }
        for repository in sorted(repositories, key=lambda item: item.relative_path)
    ]
    return _stable_hash(payload)


def compute_workspace_revision(*, inventory_hash: str, repo_manifest_hash: str) -> str:
    """Return one stable workspace revision hash spanning files and repository state."""

    return _stable_hash(
        {
            "inventory_hash": inventory_hash,
            "repo_manifest_hash": repo_manifest_hash,
        }
    )


def compute_snapshot_id(
    *,
    config: ActiveKnowledgeConfig,
    inventory: WorkspaceInventory,
    workspace_revision: str,
    baseline_id: str | None,
) -> str:
    """Return a reproducible snapshot ID for one unchanged workspace state."""

    digest = _stable_hash(
        {
            "collector_schema_version": SNAPSHOT_COLLECTOR_SCHEMA_VERSION,
            "project_id": config.project.id,
            "workspace_root": inventory.workspace_root,
            "workspace_revision": workspace_revision,
            "baseline_id": baseline_id,
            "baseline_branch": config.project.baseline_branch,
        }
    )
    return f"{SNAPSHOT_ID_PREFIX}{digest[:20]}"


def root_git_head(repositories: tuple[RepositoryInfo, ...]) -> str | None:
    """Return the workspace-root git head when available."""

    for repository in repositories:
        if repository.is_workspace_root or repository.relative_path == ".":
            return repository.commit
    return None


def snapshot_aliases(config: ActiveKnowledgeConfig, *, stable_snapshot_id: str) -> tuple[str, ...]:
    """Return current-snapshot alias IDs that should resolve to the stable snapshot."""

    aliases: list[str] = []
    for alias in (CURRENT_SNAPSHOT_ID, config.project.default_snapshot):
        if not alias or alias == stable_snapshot_id or alias in aliases:
            continue
        aliases.append(alias)
    return tuple(aliases)


def _make_alias_record(
    *,
    alias_id: str,
    stable_record: SnapshotRecord,
    stable_snapshot_id: str,
) -> SnapshotRecord:
    metadata = dict(stable_record.metadata)
    metadata.update(
        {
            "status": "current" if alias_id == CURRENT_SNAPSHOT_ID else "alias",
            "resolved_snapshot_id": stable_snapshot_id,
            "alias_id": alias_id,
        }
    )
    return SnapshotRecord(
        snapshot_id=alias_id,
        workspace_revision=stable_record.workspace_revision,
        baseline_id=stable_record.baseline_id,
        manifest_version=stable_record.manifest_version,
        created_at=stable_record.created_at,
        metadata=metadata,
    )


def _read_baseline_id(config: ActiveKnowledgeConfig, *, cwd: Path) -> str | None:
    manifest = resolve_runtime_path(config.storage.baseline.manifest, cwd)
    if not manifest.exists():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        value = payload.get("baseline_id")
        return str(value) if value is not None else None
    return None


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
