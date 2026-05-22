from __future__ import annotations

import json
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.indexing.workspace_map import (
    WorkspaceMapArtifact,
    WorkspaceProjectionView,
    WorkspaceTreeNode,
    WorkspaceViewItem,
)
from active_knowledge_server.mcp import create_fastmcp_app
from active_knowledge_server.storage import (
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    ProfileRecord,
    SnapshotRecord,
    StorageWriteRequest,
)
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    configured_sqlite_paths,
    migrate_sqlite_store,
)


def _resolved_config(tmp_path: Path) -> object:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    source_docs.mkdir()

    overrides: ConfigDict = {
        "runtime": {
            "workdir": ".active-kb",
            "source_docs_root": "knowledge-sources",
        },
        "project": {
            "workspace_root": "workspace",
            "id": "active-test",
            "display_name": "Active Test",
        },
    }
    return resolve_config(cli_overrides=overrides, cwd=tmp_path)


def _prepare_store(resolved: object, tmp_path: Path) -> SQLiteStorageAdapter:
    model = resolved.model
    paths = configured_sqlite_paths(model, cwd=tmp_path)
    migrate_sqlite_store(paths["baseline_metadata"], target="baseline_metadata")
    migrate_sqlite_store(paths["overlay_metadata"], target="overlay_metadata")
    migrate_sqlite_store(paths["jobs"], target="jobs")
    return SQLiteStorageAdapter.from_config(model, cwd=tmp_path)


def test_snapshot_resource_does_not_trigger_sqlite_migration(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handler = {resource.uri: resource.handler for resource in runtime.inventory.resources}[
        "active://snapshot/current"
    ]
    sqlite_paths = configured_sqlite_paths(resolved.model, cwd=tmp_path)

    assert not sqlite_paths["baseline_metadata"].exists()
    assert not sqlite_paths["overlay_metadata"].exists()
    assert not sqlite_paths["jobs"].exists()

    payload = json.loads(handler())

    assert payload["status"] == "missing"
    assert not sqlite_paths["baseline_metadata"].exists()
    assert not sqlite_paths["overlay_metadata"].exists()
    assert not sqlite_paths["jobs"].exists()


def test_snapshot_profile_entity_and_evidence_resources_return_structured_payloads(
    tmp_path: Path,
) -> None:
    resolved = _resolved_config(tmp_path)
    adapter = _prepare_store(resolved, tmp_path)
    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    writer.upsert_snapshot(
        SnapshotRecord(
            snapshot_id="current",
            workspace_revision="rev-1",
            baseline_id="baseline:v1",
            manifest_version="manifest.v1",
            created_at="2026-05-22T00:00:00Z",
            metadata={"resolved_snapshot_id": "snapshot:stable"},
        )
    )
    writer.upsert_profile(
        ProfileRecord(
            profile_record_id="profile:watch",
            snapshot_id="snapshot:stable",
            profile_id="watch",
            defconfig_hash="def:1",
            dotconfig_hash="dot:1",
            defconfig_path="configs/watch_defconfig",
            dotconfig_path="build/.config",
            app="watch",
            board="mhs003",
            metadata={"profile_manifest_hash": "profile-manifest"},
        )
    )
    writer.upsert_file(
        FileRecord(
            file_id="file:core",
            snapshot_id="snapshot:stable",
            source_id="workspace",
            relative_path="src/core.c",
            content_hash="hash:file",
            language="c",
        )
    )
    writer.upsert_entity(
        EntityRecord(
            entity_id="entity:core",
            snapshot_id="snapshot:stable",
            file_id="file:core",
            entity_type="function",
            name="core_loop",
            qualified_name="core_loop",
            path="src/core.c",
            start_line=10,
            end_line=24,
            metadata={"summary": "Main core loop."},
        )
    )
    writer.upsert_evidence(
        EvidenceRecord(
            evidence_id="evidence:core",
            snapshot_id="snapshot:stable",
            object_type="entity",
            object_id="entity:core",
            file_id="file:core",
            excerpt='token = "abcdefghijklmnopqrstuvwxyz123456"',
            citation_label="core loop excerpt",
            start_line=10,
            end_line=12,
            metadata={"authority_level": "workspace_code"},
        )
    )

    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {resource.uri: resource.handler for resource in runtime.inventory.resources}

    snapshot_payload = json.loads(handlers["active://snapshot/current"]())
    profile_payload = json.loads(handlers["active://profile/{profile_id}"]("watch"))
    entity_payload = json.loads(handlers["active://entity/{entity_id}"]("entity:core"))
    evidence_payload = json.loads(handlers["active://evidence/{evidence_id}"]("evidence:core"))

    assert snapshot_payload["status"] == "ok"
    assert snapshot_payload["resolved_snapshot_id"] == "snapshot:stable"
    assert snapshot_payload["available_profile_ids"] == ["watch"]

    assert profile_payload["status"] == "ok"
    assert profile_payload["profile_id"] == "watch"
    assert profile_payload["snapshot_id"] == "snapshot:stable"

    assert entity_payload["status"] == "ok"
    assert entity_payload["entity_id"] == "entity:core"
    assert entity_payload["path"] == "src/core.c"
    assert entity_payload["source_index"] == "overlay"

    assert evidence_payload["status"] == "ok"
    assert evidence_payload["object_id"] == "entity:core"
    assert evidence_payload["evidence_ref"]["path"] == "src/core.c"
    assert "***REDACTED_SECRET***" in evidence_payload["evidence_ref"]["excerpt"]


def test_workspace_resources_return_summary_and_tree(tmp_path: Path, monkeypatch) -> None:
    resolved = _resolved_config(tmp_path)
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {resource.uri: resource.handler for resource in runtime.inventory.resources}
    artifact = WorkspaceMapArtifact(
        schema_version="workspace_map.v1",
        snapshot_id="current",
        workspace_root=str(tmp_path / "workspace"),
        inventory_hash="inv-1",
        generated_at="2026-05-22T00:00:00Z",
        summary={"areas": 1, "modules": 2},
        workspace_tree=WorkspaceTreeNode(
            node_id="root",
            name="workspace",
            path="",
            role="workspace",
            layer=None,
            domain=None,
            feature=None,
            summary="workspace root",
            direct_file_count=0,
            total_file_count=3,
            module_count=2,
            children=(),
        ),
        views={
            "workspace": WorkspaceProjectionView(
                view_name="workspace",
                summary="Workspace overview",
                items=(
                    WorkspaceViewItem(
                        item_id="module:core",
                        kind="module",
                        name="core",
                        summary="Core module",
                        source_paths=("src/core.c",),
                    ),
                ),
            ),
        },
        metadata={"builder": "test"},
    )

    monkeypatch.setattr(
        runtime.query_runtime,
        "collect_workspace_artifact_readonly",
        lambda **_: artifact,
    )

    summary_payload = json.loads(handlers["active://workspace/current/summary"]())
    tree_payload = json.loads(handlers["active://workspace/current/tree"]())

    assert summary_payload["status"] == "ok"
    assert summary_payload["view_names"] == ["workspace"]
    assert summary_payload["view_summaries"]["workspace"] == "Workspace overview"

    assert tree_payload["status"] == "ok"
    assert tree_payload["workspace_tree"]["node_id"] == "root"
    assert tree_payload["summary"]["modules"] == 2


def test_index_status_resource_reports_validation_without_jobs(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handler = {resource.uri: resource.handler for resource in runtime.inventory.resources}[
        "active://index/status"
    ]

    payload = json.loads(handler())

    assert payload["requested_uri"] == "active://index/status"
    assert payload["validation"]["schema_version"] == "validate_report.v1"
    assert isinstance(payload["recent_jobs"], list)