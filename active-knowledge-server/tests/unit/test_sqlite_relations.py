from __future__ import annotations

from pathlib import Path

from active_knowledge_server.query.graph import traverse_entity_graph
from active_knowledge_server.storage import (
    EntityRecord,
    FileRecord,
    QueryScope,
    RelationRecord,
    StorageWriteRequest,
    TombstoneRecord,
)
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
)


def build_adapter(tmp_path: Path) -> SQLiteStorageAdapter:
    baseline_path = tmp_path / "baseline.db"
    overlay_path = tmp_path / "overlay.db"
    migrate_sqlite_store(baseline_path, target="baseline_metadata")
    migrate_sqlite_store(overlay_path, target="overlay_metadata")
    return SQLiteStorageAdapter(
        baseline_metadata_path=baseline_path,
        overlay_metadata_path=overlay_path,
    )


def seed_file(
    adapter: SQLiteStorageAdapter,
    *,
    target: str,
    file_id: str,
    relative_path: str,
    profile_id: str = "all",
) -> None:
    adapter.writer(StorageWriteRequest(target=target)).upsert_file(
        FileRecord(
            file_id=file_id,
            snapshot_id="current",
            source_id="workspace",
            relative_path=relative_path,
            content_hash=f"hash:{file_id}",
            profile_id=profile_id,
            language="c",
        )
    )


def seed_entity(
    adapter: SQLiteStorageAdapter,
    *,
    target: str,
    entity_id: str,
    file_id: str,
    name: str,
    profile_id: str = "all",
) -> None:
    adapter.writer(StorageWriteRequest(target=target)).upsert_entity(
        EntityRecord(
            entity_id=entity_id,
            snapshot_id="current",
            file_id=file_id,
            entity_type="function",
            name=name,
            qualified_name=name,
            path=f"{file_id}#{name}",
            profile_id=profile_id,
        )
    )


def seed_relation(
    adapter: SQLiteStorageAdapter,
    *,
    target: str,
    relation_id: str,
    src_entity_id: str,
    dst_entity_id: str,
    profile_id: str = "all",
) -> None:
    adapter.writer(StorageWriteRequest(target=target)).upsert_relation(
        RelationRecord(
            relation_id=relation_id,
            snapshot_id="current",
            relation_type="calls",
            src_entity_id=src_entity_id,
            dst_entity_id=dst_entity_id,
            profile_id=profile_id,
        )
    )


def test_baseline_relation_can_point_to_overlay_entity(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)
    seed_file(adapter, target="baseline", file_id="file-a", relative_path="src/a.c")
    seed_file(adapter, target="overlay", file_id="file-b", relative_path="src/b.c")
    seed_entity(adapter, target="baseline", entity_id="entity-a", file_id="file-a", name="a")
    seed_entity(adapter, target="overlay", entity_id="entity-b", file_id="file-b", name="b")
    seed_relation(
        adapter,
        target="baseline",
        relation_id="rel-a-b",
        src_entity_id="entity-a",
        dst_entity_id="entity-b",
    )

    relations = adapter.reader().logical_relations(QueryScope(snapshot_id="current"))

    assert len(relations) == 1
    assert relations[0].record.src_entity_id == "entity-a"
    assert relations[0].record.dst_entity_id == "entity-b"
    assert relations[0].source_index == "merged"


def test_relation_endpoint_replacement_points_to_overlay_entity(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)
    seed_file(adapter, target="baseline", file_id="file-old", relative_path="src/old.c")
    seed_file(adapter, target="baseline", file_id="file-peer", relative_path="src/peer.c")
    seed_file(adapter, target="overlay", file_id="file-new", relative_path="src/new.c")
    seed_entity(
        adapter,
        target="baseline",
        entity_id="entity-old",
        file_id="file-old",
        name="old_handler",
    )
    seed_entity(
        adapter,
        target="baseline",
        entity_id="entity-peer",
        file_id="file-peer",
        name="peer_handler",
    )
    seed_entity(
        adapter,
        target="overlay",
        entity_id="entity-new",
        file_id="file-new",
        name="new_handler",
    )
    seed_relation(
        adapter,
        target="baseline",
        relation_id="rel-old-peer",
        src_entity_id="entity-old",
        dst_entity_id="entity-peer",
    )
    adapter.writer(StorageWriteRequest(target="overlay")).replace_object(
        "entity",
        "entity-old",
        "entity-new",
        scope=QueryScope(snapshot_id="current"),
        reason="symbol_moved",
        created_by_job="job-relation-replacement",
        baseline_id="entity-old",
    )

    relations = adapter.reader().logical_relations(QueryScope(snapshot_id="current"))

    assert len(relations) == 1
    assert relations[0].record.src_entity_id == "entity-new"
    assert relations[0].record.dst_entity_id == "entity-peer"
    assert relations[0].source_index == "merged"
    assert relations[0].replaced_from == ("entity-new",)


def test_validate_relations_reports_orphan_relation(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)
    seed_file(adapter, target="baseline", file_id="file-peer", relative_path="src/peer.c")
    seed_entity(
        adapter,
        target="baseline",
        entity_id="entity-peer",
        file_id="file-peer",
        name="peer_handler",
    )
    seed_relation(
        adapter,
        target="baseline",
        relation_id="rel-missing-peer",
        src_entity_id="entity-missing",
        dst_entity_id="entity-peer",
    )

    reader = adapter.reader()
    scope = QueryScope(snapshot_id="current")
    issues = reader.validate_relations(scope)

    assert reader.logical_relations(scope) == ()
    assert len(issues) == 1
    assert issues[0].issue_code == "storage.orphan_relation"
    assert issues[0].relation_id == "rel-missing-peer"
    assert issues[0].metadata["missing_src"] is True
    assert issues[0].metadata["missing_dst"] is False


def test_graph_traversal_does_not_return_tombstoned_nodes(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)
    seed_file(adapter, target="baseline", file_id="file-a", relative_path="src/a.c")
    seed_file(adapter, target="baseline", file_id="file-b", relative_path="src/b.c")
    seed_entity(adapter, target="baseline", entity_id="entity-a", file_id="file-a", name="a")
    seed_entity(adapter, target="baseline", entity_id="entity-b", file_id="file-b", name="b")
    seed_relation(
        adapter,
        target="baseline",
        relation_id="rel-a-b",
        src_entity_id="entity-a",
        dst_entity_id="entity-b",
    )
    adapter.writer(StorageWriteRequest(target="overlay")).upsert_tombstone(
        TombstoneRecord(
            tombstone_id="ts-entity-b",
            object_type="entity",
            object_id="entity-b",
            reason="deleted",
            created_by_job="job-delete-b",
            snapshot_id="current",
        )
    )

    result = traverse_entity_graph(
        adapter.reader(),
        ("entity-a",),
        scope=QueryScope(snapshot_id="current"),
        max_depth=1,
    )

    assert result.entity_ids == ("entity-a",)
    assert result.relations == ()
