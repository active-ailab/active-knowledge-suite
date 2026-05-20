from __future__ import annotations

import json
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors import (
    FileInventoryEntry,
    RepositoryInfo,
    WorkspaceArea,
    WorkspaceInventory,
)
from active_knowledge_server.indexing.snapshot import CURRENT_SNAPSHOT_ID, SnapshotCollector
from active_knowledge_server.storage import StorageWriteRequest
from active_knowledge_server.storage.sqlite_store import SQLiteStorageAdapter, migrate_sqlite_store


def resolve_model(tmp_path: Path, overrides: ConfigDict | None = None) -> ActiveKnowledgeConfig:
    workspace = tmp_path / "workspace"
    docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    docs.mkdir()
    merged: ConfigDict = {
        "runtime": {
            "workdir": str(tmp_path / ".active-kb"),
            "baseline_dir": str(tmp_path / ".active-kb" / "baseline"),
            "local_dir": str(tmp_path / ".active-kb" / "local"),
            "source_docs_root": str(docs),
        },
        "project": {
            "workspace_root": str(workspace),
            "baseline_branch": "origin/main",
        },
        "storage": {
            "baseline": {
                "manifest": str(tmp_path / ".active-kb" / "baseline" / "manifest.json")
            },
            "metadata": {
                "path": str(tmp_path / ".active-kb" / "baseline" / "db" / "metadata.db"),
                "mode": "readwrite",
            },
            "overlay": {
                "path": str(tmp_path / ".active-kb" / "local" / "db" / "overlay.db"),
                "mode": "readwrite",
            },
            "jobs": {
                "path": str(tmp_path / ".active-kb" / "local" / "db" / "jobs.db"),
                "mode": "readwrite",
            },
            "vector": {
                "path": str(tmp_path / ".active-kb" / "baseline" / "vectors"),
                "mode": "readwrite",
            },
            "vector_delta": {
                "path": str(tmp_path / ".active-kb" / "local" / "vectors"),
                "mode": "readwrite",
            },
            "cache_root": str(tmp_path / ".active-kb" / "local" / "cache"),
        },
    }
    if overrides:
        merged = deep_merge(merged, overrides)
    return resolve_config(cli_overrides=merged, env={}, cwd=tmp_path).model


def build_adapter(config: ActiveKnowledgeConfig) -> SQLiteStorageAdapter:
    baseline_path = Path(config.storage.metadata.path)
    overlay_path = Path(config.storage.overlay.path)
    jobs_path = Path(config.storage.jobs.path)
    migrate_sqlite_store(baseline_path, target="baseline_metadata")
    migrate_sqlite_store(overlay_path, target="overlay_metadata")
    migrate_sqlite_store(jobs_path, target="jobs")
    return SQLiteStorageAdapter(
        baseline_metadata_path=baseline_path,
        overlay_metadata_path=overlay_path,
        jobs_path=jobs_path,
    )


def test_snapshot_id_is_stable_for_unchanged_workspace(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    (workspace_root / "src").mkdir()
    (workspace_root / "src" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (workspace_root / "README.md").write_text("# Demo\n", encoding="utf-8")

    collector = SnapshotCollector.from_config(config, cwd=tmp_path)
    first = collector.collect(created_at="2026-05-20T10:00:00Z")
    second = collector.collect(created_at="2026-05-20T11:00:00Z")

    assert first.snapshot_id == second.snapshot_id
    assert first.snapshot_record.created_at == "2026-05-20T10:00:00Z"
    assert second.snapshot_record.created_at == "2026-05-20T11:00:00Z"
    assert first.snapshot_record.workspace_revision == second.snapshot_record.workspace_revision


def test_snapshot_id_changes_when_workspace_inventory_changes(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    (workspace_root / "src").mkdir()
    source_file = workspace_root / "src" / "main.c"
    source_file.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    collector = SnapshotCollector.from_config(config, cwd=tmp_path)
    first = collector.collect(created_at="2026-05-20T10:00:00Z")
    source_file.write_text("int main(void) { return 1; }\n", encoding="utf-8")
    second = collector.collect(created_at="2026-05-20T10:05:00Z")

    assert first.snapshot_id != second.snapshot_id
    assert first.snapshot_record.metadata["workspace_inventory_hash"] != second.snapshot_record.metadata["workspace_inventory_hash"]


def test_snapshot_collector_persists_stable_record_and_current_alias(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    baseline_manifest = Path(config.storage.baseline.manifest)
    baseline_manifest.parent.mkdir(parents=True, exist_ok=True)
    baseline_manifest.write_text('{"baseline_id": "baseline-2026-05"}\n', encoding="utf-8")

    inventory = WorkspaceInventory(
        schema_version="workspace_inventory.v1",
        workspace_root=str(Path(config.project.workspace_root)),
        workspace_display_path="workspace",
        include=(),
        exclude=(),
        areas=(
            WorkspaceArea(
                name="src",
                relative_path="src",
                display_path="workspace/src",
                file_count=1,
                directory_count=1,
            ),
        ),
        repositories=(
            RepositoryInfo(
                relative_path=".",
                display_path="workspace",
                commit="abc123def456",
                branch="main",
                dirty=False,
                is_workspace_root=True,
            ),
        ),
        files=(
            FileInventoryEntry(
                relative_path="src/main.c",
                display_path="workspace/src/main.c",
                size_bytes=24,
                content_hash="file-hash-1",
                repo_relative_path=".",
                area="src",
                language="c",
            ),
        ),
        inventory_hash="inventory-hash-1",
        warnings=(),
    )

    adapter = build_adapter(config)
    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    collector = SnapshotCollector.from_config(config, cwd=tmp_path)
    collected = collector.collect_and_store(
        writer,
        inventory=inventory,
        created_at="2026-05-20T12:00:00Z",
    )
    reader = adapter.reader()
    stable = reader.get_snapshot(collected.snapshot_id)
    current = reader.get_snapshot(CURRENT_SNAPSHOT_ID)

    assert stable is not None
    assert current is not None
    assert stable.snapshot_id.startswith("snapshot:")
    assert stable.baseline_id == "baseline-2026-05"
    assert stable.metadata["baseline_branch"] == "origin/main"
    assert stable.metadata["git_head"] == "abc123def456"
    assert stable.metadata["repo_manifest_hash"]
    assert current.metadata["resolved_snapshot_id"] == stable.snapshot_id
    assert current.metadata["status"] == "current"
    assert current.created_at == stable.created_at
    payload = json.dumps(collected.to_dict(), ensure_ascii=True, sort_keys=True)
    assert "abc123def456" in payload


def deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = merged[key]
            assert isinstance(nested, dict)
            merged[key] = deep_merge(nested, value)
        else:
            merged[key] = value
    return merged
