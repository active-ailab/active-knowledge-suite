from __future__ import annotations

import sqlite3
from pathlib import Path

from active_knowledge_server.storage import (
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    FTSQuery,
    QueryScope,
    RelationRecord,
    ReplacementRecord,
    StorageWriteRequest,
    TombstoneRecord,
    VectorRefRecord,
)
from active_knowledge_server.storage.sqlite_store import (
    SQLitePragmaProfile,
    SQLiteStorageAdapter,
    migrate_sqlite_store,
)


def build_adapter(tmp_path: Path) -> tuple[SQLiteStorageAdapter, Path, Path]:
    baseline_path = tmp_path / "baseline.db"
    overlay_path = tmp_path / "overlay.db"
    migrate_sqlite_store(baseline_path, target="baseline_metadata")
    migrate_sqlite_store(overlay_path, target="overlay_metadata")
    return (
        SQLiteStorageAdapter(
            baseline_metadata_path=baseline_path,
            overlay_metadata_path=overlay_path,
        ),
        baseline_path,
        overlay_path,
    )


def table_count(path: Path, table: str) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def seed_file(
    adapter: SQLiteStorageAdapter,
    *,
    target: str,
    file_id: str,
    relative_path: str,
    profile_id: str = "all",
    language: str = "md",
) -> None:
    writer = adapter.writer(StorageWriteRequest(target=target))
    writer.upsert_file(
        FileRecord(
            file_id=file_id,
            snapshot_id="current",
            source_id="knowledge-api",
            relative_path=relative_path,
            content_hash=f"hash:{file_id}",
            profile_id=profile_id,
            language=language,
        )
    )


def test_chunk_and_entity_upserts_sync_fts_and_support_filters(tmp_path: Path) -> None:
    adapter, baseline_path, _overlay_path = build_adapter(tmp_path)
    seed_file(
        adapter,
        target="baseline",
        file_id="file-api",
        relative_path="knowledge-sources/api/sensor.md",
        profile_id="watch",
        language="md",
    )

    writer = adapter.writer(StorageWriteRequest(target="baseline"))
    writer.upsert_chunk(
        ChunkRecord(
            chunk_id="chunk-doc",
            snapshot_id="current",
            file_id="file-api",
            content_hash="hash:chunk-doc",
            chunk_type="doc_section",
            ordinal=0,
            text="The sensor register API configures one register at a time.",
            profile_id="watch",
            metadata={
                "title": "Sensor Register API",
                "domain": "engineering",
                "doc_type": "api",
                "tags": ["sensor", "register"],
            },
        )
    )
    writer.upsert_entity(
        EntityRecord(
            entity_id="entity-sensor-register",
            snapshot_id="current",
            file_id="file-api",
            entity_type="API",
            name="sensor_register",
            qualified_name="active.sensor_register",
            path="knowledge-sources/api/sensor.md#sensor_register",
            profile_id="watch",
            metadata={
                "domain": "engineering",
                "doc_type": "api",
                "aliases": ["sensor register"],
                "summary": "Registers the sensor and returns a handle.",
            },
        )
    )

    assert table_count(baseline_path, "chunk_fts") == 1
    assert table_count(baseline_path, "doc_fts") == 1
    assert table_count(baseline_path, "code_fts") == 0
    assert table_count(baseline_path, "entity_fts") == 1

    reader = adapter.reader()
    matches = reader.search_fts(
        FTSQuery(
            index_name="doc_fts",
            query="sensor register",
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            domain="engineering",
            doc_type="api",
        )
    )

    assert [match.logical_object_id for match in matches] == ["chunk-doc"]
    assert matches[0].source_index == "baseline"
    assert matches[0].doc_type == "api"
    assert matches[0].domain == "engineering"

    no_profile_match = reader.search_fts(
        FTSQuery(
            index_name="doc_fts",
            query="sensor register",
            scope=QueryScope(snapshot_id="current", profile_id="sensorhub"),
            domain="engineering",
            doc_type="api",
        )
    )
    assert no_profile_match == ()

    entity_matches = reader.search_fts(
        FTSQuery(
            index_name="entity_fts",
            query="sensor_register",
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            domain="engineering",
            doc_type="api",
        )
    )
    assert [match.logical_object_id for match in entity_matches] == ["entity-sensor-register"]


def test_writer_transaction_rolls_back_metadata_and_fts(tmp_path: Path) -> None:
    adapter, baseline_path, _overlay_path = build_adapter(tmp_path)
    writer = adapter.writer(StorageWriteRequest(target="baseline"))

    try:
        with writer.transaction():
            writer.upsert_file(
                FileRecord(
                    file_id="file-rollback",
                    snapshot_id="current",
                    source_id="knowledge-api",
                    relative_path="knowledge-sources/api/rollback.md",
                    content_hash="hash:file-rollback",
                    profile_id="watch",
                    language="md",
                )
            )
            writer.upsert_chunk(
                ChunkRecord(
                    chunk_id="chunk-rollback",
                    snapshot_id="current",
                    file_id="file-rollback",
                    content_hash="hash:chunk-rollback",
                    chunk_type="doc_section",
                    ordinal=0,
                    text="Rollback should remove this FTS row.",
                    profile_id="watch",
                    metadata={"doc_type": "api", "domain": "engineering"},
                )
            )
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    assert table_count(baseline_path, "file") == 0
    assert table_count(baseline_path, "chunk") == 0
    assert table_count(baseline_path, "chunk_fts") == 0
    assert table_count(baseline_path, "doc_fts") == 0


def test_writer_applies_configured_sqlite_pragmas(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.db"
    overlay_path = tmp_path / "overlay.db"
    migrate_sqlite_store(baseline_path, target="baseline_metadata")
    migrate_sqlite_store(overlay_path, target="overlay_metadata")
    adapter = SQLiteStorageAdapter(
        baseline_metadata_path=baseline_path,
        overlay_metadata_path=overlay_path,
        pragma_profile=SQLitePragmaProfile(
            journal_mode="wal",
            synchronous="normal",
            wal_autocheckpoint_pages=32,
        ),
    )

    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    with writer.transaction():
        writer.upsert_file(
            FileRecord(
                file_id="file-wal",
                snapshot_id="current",
                source_id="knowledge-api",
                relative_path="knowledge-sources/api/wal.md",
                content_hash="hash:file-wal",
                profile_id="watch",
                language="md",
            )
        )
        transaction_connection = writer._transaction_connection
        assert transaction_connection is not None
        synchronous = transaction_connection.execute("PRAGMA synchronous").fetchone()
        wal_autocheckpoint = transaction_connection.execute(
            "PRAGMA wal_autocheckpoint"
        ).fetchone()
        assert synchronous is not None
        assert int(synchronous[0]) == 1
        assert wal_autocheckpoint is not None
        assert int(wal_autocheckpoint[0]) == 32

    with sqlite3.connect(overlay_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()

    assert journal_mode is not None
    assert str(journal_mode[0]).lower() == "wal"


def test_overlay_candidate_overrides_baseline_for_same_logical_id(tmp_path: Path) -> None:
    adapter, _baseline_path, _overlay_path = build_adapter(tmp_path)
    seed_file(
        adapter,
        target="baseline",
        file_id="file-code",
        relative_path="drivers/sensor.c",
        profile_id="watch",
        language="c",
    )
    seed_file(
        adapter,
        target="overlay",
        file_id="file-code",
        relative_path="drivers/sensor.c",
        profile_id="watch",
        language="c",
    )

    baseline_writer = adapter.writer(StorageWriteRequest(target="baseline"))
    baseline_writer.upsert_chunk(
        ChunkRecord(
            chunk_id="chunk-code",
            snapshot_id="current",
            file_id="file-code",
            content_hash="hash:baseline",
            chunk_type="code_block",
            ordinal=0,
            text="void sensor_register(void) { legacy_register(); }",
            profile_id="watch",
            metadata={
                "domain": "engineering",
                "symbol_names": ["sensor_register"],
                "comments": "legacy path",
            },
        )
    )

    overlay_writer = adapter.writer(StorageWriteRequest(target="overlay"))
    overlay_writer.upsert_chunk(
        ChunkRecord(
            chunk_id="chunk-code",
            snapshot_id="current",
            file_id="file-code",
            content_hash="hash:overlay",
            chunk_type="code_block",
            ordinal=0,
            text="void sensor_register(void) { new_register(); }",
            profile_id="watch",
            metadata={
                "domain": "engineering",
                "symbol_names": ["sensor_register"],
                "comments": "overlay path",
            },
        )
    )

    reader = adapter.reader()
    merged = reader.search_fts(
        FTSQuery(
            index_name="code_fts",
            query="sensor_register",
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
        )
    )
    assert [match.logical_object_id for match in merged] == ["chunk-code"]
    assert merged[0].source_index == "overlay"
    assert merged[0].metadata["bm25"] <= 0.0

    baseline_only = reader.search_fts(
        FTSQuery(
            index_name="code_fts",
            query="sensor_register",
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            source_index="baseline",
        )
    )
    assert baseline_only == ()

    overlay_only = reader.search_fts(
        FTSQuery(
            index_name="code_fts",
            query="sensor_register",
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            source_index="overlay",
        )
    )
    assert [match.logical_object_id for match in overlay_only] == ["chunk-code"]


def test_tombstone_hides_baseline_fts_hit_from_logical_view(tmp_path: Path) -> None:
    adapter, _baseline_path, _overlay_path = build_adapter(tmp_path)
    seed_file(
        adapter,
        target="baseline",
        file_id="file-doc",
        relative_path="knowledge-sources/engineering/runtime.md",
        language="md",
    )

    baseline_writer = adapter.writer(StorageWriteRequest(target="baseline"))
    baseline_writer.upsert_chunk(
        ChunkRecord(
            chunk_id="chunk-runtime",
            snapshot_id="current",
            file_id="file-doc",
            content_hash="hash:runtime",
            chunk_type="doc_section",
            ordinal=0,
            text="Runtime sensor flow starts from the queue handler.",
            metadata={"domain": "engineering", "doc_type": "engineering"},
        )
    )

    overlay_writer = adapter.writer(StorageWriteRequest(target="overlay"))
    overlay_writer.upsert_tombstone(
        TombstoneRecord(
            tombstone_id="ts-runtime",
            object_type="chunk",
            object_id="chunk-runtime",
            reason="deleted",
            created_by_job="job-1",
            snapshot_id="current",
        )
    )

    reader = adapter.reader()
    matches = reader.search_fts(
        FTSQuery(
            index_name="doc_fts",
            query="queue handler",
            scope=QueryScope(snapshot_id="current"),
        )
    )

    assert matches == ()


def test_replacement_maps_old_entity_hit_to_merged_candidate(tmp_path: Path) -> None:
    adapter, _baseline_path, _overlay_path = build_adapter(tmp_path)
    seed_file(
        adapter,
        target="baseline",
        file_id="file-entity-old",
        relative_path="knowledge-sources/api/sensor.md",
        profile_id="watch",
        language="md",
    )
    seed_file(
        adapter,
        target="overlay",
        file_id="file-entity-new",
        relative_path="knowledge-sources/api/sensor_v2.md",
        profile_id="watch",
        language="md",
    )

    baseline_writer = adapter.writer(StorageWriteRequest(target="baseline"))
    baseline_writer.upsert_entity(
        EntityRecord(
            entity_id="entity-old",
            snapshot_id="current",
            file_id="file-entity-old",
            entity_type="API",
            name="sensor_register",
            qualified_name="active.sensor_register",
            path="knowledge-sources/api/sensor.md#sensor_register",
            profile_id="watch",
            metadata={"domain": "engineering", "doc_type": "api"},
        )
    )

    overlay_writer = adapter.writer(StorageWriteRequest(target="overlay"))
    overlay_writer.upsert_entity(
        EntityRecord(
            entity_id="entity-new",
            snapshot_id="current",
            file_id="file-entity-new",
            entity_type="API",
            name="sensor_attach",
            qualified_name="active.sensor_attach",
            path="knowledge-sources/api/sensor_v2.md#sensor_attach",
            profile_id="watch",
            metadata={"domain": "engineering", "doc_type": "api"},
        )
    )
    overlay_writer.upsert_replacement(
        ReplacementRecord(
            replacement_id="rep-entity",
            object_type="entity",
            old_object_id="entity-old",
            new_object_id="entity-new",
            reason="doc_section_rekeyed",
            created_by_job="job-2",
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
        )
    )

    reader = adapter.reader()
    matches = reader.search_fts(
        FTSQuery(
            index_name="entity_fts",
            query="sensor_register",
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            source_index="merged",
        )
    )

    assert len(matches) == 1
    assert matches[0].logical_object_id == "entity-new"
    assert matches[0].physical_object_id == "entity-old"
    assert matches[0].source_index == "merged"


def test_tombstone_file_cascades_and_hides_baseline_evidence(tmp_path: Path) -> None:
    adapter, baseline_path, overlay_path = build_adapter(tmp_path)
    seed_file(
        adapter,
        target="baseline",
        file_id="file-runtime",
        relative_path="knowledge-sources/engineering/runtime.md",
        language="md",
    )
    baseline_writer = adapter.writer(StorageWriteRequest(target="baseline"))
    baseline_writer.upsert_chunk(
        ChunkRecord(
            chunk_id="chunk-runtime",
            snapshot_id="current",
            file_id="file-runtime",
            content_hash="hash:runtime",
            chunk_type="doc_section",
            ordinal=0,
            text="Runtime queue handler evidence lives here.",
            metadata={"domain": "engineering", "doc_type": "engineering"},
        )
    )
    baseline_writer.upsert_entity(
        EntityRecord(
            entity_id="entity-runtime",
            snapshot_id="current",
            file_id="file-runtime",
            entity_type="symbol",
            name="runtime_queue_handler",
            qualified_name="runtime_queue_handler",
            path="knowledge-sources/engineering/runtime.md#runtime_queue_handler",
        )
    )
    baseline_writer.upsert_relation(
        RelationRecord(
            relation_id="rel-runtime-self",
            snapshot_id="current",
            relation_type="mentions",
            src_entity_id="entity-runtime",
            dst_entity_id="entity-runtime",
        )
    )
    baseline_writer.upsert_evidence(
        EvidenceRecord(
            evidence_id="ev-runtime",
            snapshot_id="current",
            object_type="chunk",
            object_id="chunk-runtime",
            file_id="file-runtime",
            chunk_id="chunk-runtime",
            excerpt="Runtime queue handler evidence lives here.",
        )
    )
    baseline_writer.upsert_vector_ref(
        VectorRefRecord(
            vector_ref_id="vec-runtime",
            object_type="chunk",
            object_id="chunk-runtime",
            chunk_id="chunk-runtime",
            embedding_model_version="bge-m3",
            content_hash="hash:runtime",
        )
    )

    records = adapter.writer(StorageWriteRequest(target="overlay")).tombstone_file(
        "file-runtime",
        scope=QueryScope(snapshot_id="current"),
        reason="deleted",
        created_by_job="job-delete-file",
    )

    assert {record.object_type for record in records} == {
        "file",
        "chunk",
        "entity",
        "relation",
        "evidence",
        "vector_ref",
    }
    assert table_count(overlay_path, "tombstone") == 6
    assert table_count(baseline_path, "chunk") == 1
    assert table_count(baseline_path, "evidence") == 1

    reader = adapter.reader()
    scope = QueryScope(snapshot_id="current")
    assert reader.logical_chunks(scope) == ()
    assert reader.logical_entities(scope) == ()
    assert reader.logical_relations(scope) == ()
    assert reader.logical_evidence(scope) == ()
    assert reader.iter_vector_refs(scope) == ()
    assert reader.search_fts(
        FTSQuery(index_name="doc_fts", query="queue handler", scope=scope)
    ) == ()


def test_replace_object_api_prefers_overlay_symbol_for_old_baseline_id(
    tmp_path: Path,
) -> None:
    adapter, _baseline_path, _overlay_path = build_adapter(tmp_path)
    seed_file(
        adapter,
        target="baseline",
        file_id="file-symbol-old",
        relative_path="drivers/sensor.c",
        language="c",
    )
    seed_file(
        adapter,
        target="overlay",
        file_id="file-symbol-new",
        relative_path="drivers/sensor_v2.c",
        language="c",
    )
    adapter.writer(StorageWriteRequest(target="baseline")).upsert_entity(
        EntityRecord(
            entity_id="entity-symbol-old",
            snapshot_id="current",
            file_id="file-symbol-old",
            entity_type="function",
            name="sensor_register",
            qualified_name="sensor_register",
            path="drivers/sensor.c#sensor_register",
            metadata={"domain": "engineering", "summary": "Old registration function."},
        )
    )
    overlay_writer = adapter.writer(StorageWriteRequest(target="overlay"))
    overlay_writer.upsert_entity(
        EntityRecord(
            entity_id="entity-symbol-new",
            snapshot_id="current",
            file_id="file-symbol-new",
            entity_type="function",
            name="sensor_attach",
            qualified_name="sensor_attach",
            path="drivers/sensor_v2.c#sensor_attach",
            metadata={"domain": "engineering", "summary": "New attach function."},
        )
    )
    replacement = overlay_writer.replace_object(
        "entity",
        "entity-symbol-old",
        "entity-symbol-new",
        scope=QueryScope(snapshot_id="current"),
        reason="symbol_moved",
        created_by_job="job-replace-symbol",
        baseline_id="entity-symbol-old",
    )

    reader = adapter.reader()
    resolution = reader.resolve_replacement(
        "entity",
        "entity-symbol-old",
        QueryScope(snapshot_id="current"),
    )
    logical_entities = reader.logical_entities(QueryScope(snapshot_id="current"))

    assert replacement.replacement_id.startswith("rp:")
    assert resolution.resolved_object_id == "entity-symbol-new"
    assert [entity.logical_object_id for entity in logical_entities] == ["entity-symbol-new"]
    assert logical_entities[0].source_index == "overlay"
