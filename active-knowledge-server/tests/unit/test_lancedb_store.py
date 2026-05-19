from __future__ import annotations

import json
from pathlib import Path

from active_knowledge_server.storage import (
    ChunkRecord,
    FileRecord,
    QueryScope,
    ReplacementRecord,
    StorageWriteRequest,
    TombstoneRecord,
    VectorQuery,
    VectorRefRecord,
)
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
)


def build_adapters(tmp_path: Path) -> tuple[SQLiteStorageAdapter, LanceDBVectorAdapter, Path, Path]:
    baseline_metadata = tmp_path / "baseline.db"
    overlay_metadata = tmp_path / "overlay.db"
    migrate_sqlite_store(baseline_metadata, target="baseline_metadata")
    migrate_sqlite_store(overlay_metadata, target="overlay_metadata")

    metadata_adapter = SQLiteStorageAdapter(
        baseline_metadata_path=baseline_metadata,
        overlay_metadata_path=overlay_metadata,
    )
    baseline_vectors = tmp_path / "baseline-vectors"
    delta_vectors = tmp_path / "delta-vectors"
    vector_adapter = LanceDBVectorAdapter(
        baseline_vector_path=baseline_vectors,
        delta_vector_path=delta_vectors,
        metadata_adapter=metadata_adapter,
    )
    return metadata_adapter, vector_adapter, baseline_vectors, delta_vectors


def seed_file(
    adapter: SQLiteStorageAdapter,
    *,
    target: str,
    file_id: str,
    relative_path: str,
    profile_id: str = "watch",
) -> None:
    adapter.writer(StorageWriteRequest(target=target)).upsert_file(
        FileRecord(
            file_id=file_id,
            snapshot_id="current",
            source_id="knowledge-api",
            relative_path=relative_path,
            content_hash=f"hash:{file_id}",
            profile_id=profile_id,
            language="md",
        )
    )


def seed_chunk(
    adapter: SQLiteStorageAdapter,
    *,
    target: str,
    chunk_id: str,
    file_id: str,
    content_hash: str,
    profile_id: str = "watch",
    text: str | None = None,
) -> None:
    adapter.writer(StorageWriteRequest(target=target)).upsert_chunk(
        ChunkRecord(
            chunk_id=chunk_id,
            snapshot_id="current",
            file_id=file_id,
            content_hash=content_hash,
            chunk_type="doc_section",
            ordinal=0,
            text=text or f"Chunk {chunk_id}",
            profile_id=profile_id,
            metadata={"doc_type": "api", "domain": "engineering"},
        )
    )


def read_collection(path: Path, object_type: str = "chunk") -> list[dict[str, object]]:
    collection = path / f"{object_type}.json"
    if not collection.exists():
        return []
    payload = json.loads(collection.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return payload


def test_vector_upsert_syncs_chunk_metadata_and_searches_across_baseline_and_delta(
    tmp_path: Path,
) -> None:
    metadata_adapter, vector_adapter, baseline_vectors, delta_vectors = build_adapters(tmp_path)
    seed_file(
        metadata_adapter,
        target="baseline",
        file_id="file-baseline",
        relative_path="knowledge-sources/api/baseline.md",
    )
    seed_chunk(
        metadata_adapter,
        target="baseline",
        chunk_id="chunk-baseline",
        file_id="file-baseline",
        content_hash="hash:baseline",
    )
    seed_file(
        metadata_adapter,
        target="overlay",
        file_id="file-overlay",
        relative_path="knowledge-sources/api/overlay.md",
    )
    seed_chunk(
        metadata_adapter,
        target="overlay",
        chunk_id="chunk-overlay",
        file_id="file-overlay",
        content_hash="hash:overlay",
    )

    baseline_writer = vector_adapter.writer(
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
    )
    baseline_writer.upsert_vector(
        VectorRefRecord(
            vector_ref_id="vec-baseline",
            object_type="chunk",
            object_id="chunk-baseline",
            chunk_id="chunk-baseline",
            embedding_model_version="bge-m3",
            content_hash="hash:baseline",
            profile_id="watch",
        ),
        embedding=(1.0, 0.0),
    )
    overlay_writer = vector_adapter.writer(StorageWriteRequest(target="overlay"))
    overlay_writer.upsert_vector(
        VectorRefRecord(
            vector_ref_id="vec-overlay",
            object_type="chunk",
            object_id="chunk-overlay",
            chunk_id="chunk-overlay",
            embedding_model_version="bge-m3",
            content_hash="hash:overlay",
            profile_id="watch",
        ),
        embedding=(0.8, 0.2),
    )

    overlay_chunk = metadata_adapter.reader().get_chunk("chunk-overlay")
    assert overlay_chunk is not None
    assert overlay_chunk.metadata["embedding_ref"] == "vec-overlay"

    assert len(read_collection(baseline_vectors)) == 1
    assert len(read_collection(delta_vectors)) == 1

    result = vector_adapter.reader().search(
        VectorQuery(
            embedding=(1.0, 0.0),
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            embedding_model_version="bge-m3",
        )
    )

    assert [match.logical_object_id for match in result.matches] == [
        "chunk-baseline",
        "chunk-overlay",
    ]
    assert result.matches[0].source_index == "baseline"
    assert result.matches[1].source_index == "overlay"
    assert result.warnings == ()


def test_overlay_vector_overrides_baseline_candidate_for_same_logical_chunk(tmp_path: Path) -> None:
    metadata_adapter, vector_adapter, _baseline_vectors, _delta_vectors = build_adapters(tmp_path)
    seed_file(
        metadata_adapter,
        target="baseline",
        file_id="file-code",
        relative_path="knowledge-sources/api/sensor.md",
    )
    seed_file(
        metadata_adapter,
        target="overlay",
        file_id="file-code",
        relative_path="knowledge-sources/api/sensor.md",
    )
    seed_chunk(
        metadata_adapter,
        target="baseline",
        chunk_id="chunk-shared",
        file_id="file-code",
        content_hash="hash:baseline",
        text="legacy sensor register docs",
    )
    seed_chunk(
        metadata_adapter,
        target="overlay",
        chunk_id="chunk-shared",
        file_id="file-code",
        content_hash="hash:overlay",
        text="new sensor register docs",
    )

    vector_adapter.writer(
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
    ).upsert_vector(
        VectorRefRecord(
            vector_ref_id="vec-old",
            object_type="chunk",
            object_id="chunk-shared",
            chunk_id="chunk-shared",
            embedding_model_version="bge-m3",
            content_hash="hash:baseline",
            profile_id="watch",
        ),
        embedding=(1.0, 0.0),
    )
    vector_adapter.writer(StorageWriteRequest(target="overlay")).upsert_vector(
        VectorRefRecord(
            vector_ref_id="vec-new",
            object_type="chunk",
            object_id="chunk-shared",
            chunk_id="chunk-shared",
            embedding_model_version="bge-m3",
            content_hash="hash:overlay",
            profile_id="watch",
        ),
        embedding=(0.9, 0.1),
    )

    result = vector_adapter.reader().search(
        VectorQuery(
            embedding=(1.0, 0.0),
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            embedding_model_version="bge-m3",
        )
    )

    assert [match.logical_object_id for match in result.matches] == ["chunk-shared"]
    assert result.matches[0].source_index == "overlay"


def test_tombstone_and_replacement_hide_stale_baseline_vectors(tmp_path: Path) -> None:
    metadata_adapter, vector_adapter, _baseline_vectors, _delta_vectors = build_adapters(tmp_path)
    seed_file(
        metadata_adapter,
        target="baseline",
        file_id="file-doc",
        relative_path="knowledge-sources/api/runtime.md",
    )
    seed_chunk(
        metadata_adapter,
        target="baseline",
        chunk_id="chunk-old",
        file_id="file-doc",
        content_hash="hash:old",
    )
    vector_adapter.writer(
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
    ).upsert_vector(
        VectorRefRecord(
            vector_ref_id="vec-old",
            object_type="chunk",
            object_id="chunk-old",
            chunk_id="chunk-old",
            embedding_model_version="bge-m3",
            content_hash="hash:old",
            profile_id="watch",
        ),
        embedding=(1.0, 0.0),
    )

    metadata_adapter.writer(StorageWriteRequest(target="overlay")).upsert_tombstone(
        TombstoneRecord(
            tombstone_id="ts-old",
            object_type="chunk",
            object_id="chunk-old",
            reason="local delete",
            created_by_job="job-1",
            snapshot_id="current",
            profile_id="watch",
        )
    )

    tombstoned = vector_adapter.reader().search(
        VectorQuery(
            embedding=(1.0, 0.0),
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            embedding_model_version="bge-m3",
        )
    )
    assert tombstoned.matches == ()

    seed_chunk(
        metadata_adapter,
        target="overlay",
        chunk_id="chunk-new",
        file_id="file-doc",
        content_hash="hash:new",
    )
    metadata_adapter.writer(StorageWriteRequest(target="overlay")).upsert_replacement(
        ReplacementRecord(
            replacement_id="rp-old-to-new",
            object_type="chunk",
            old_object_id="chunk-old",
            new_object_id="chunk-new",
            reason="rechunk",
            created_by_job="job-2",
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
        )
    )
    vector_adapter.writer(StorageWriteRequest(target="overlay")).upsert_vector(
        VectorRefRecord(
            vector_ref_id="vec-new",
            object_type="chunk",
            object_id="chunk-new",
            chunk_id="chunk-new",
            embedding_model_version="bge-m3",
            content_hash="hash:new",
            profile_id="watch",
        ),
        embedding=(0.9, 0.1),
    )

    replaced = vector_adapter.reader().search(
        VectorQuery(
            embedding=(1.0, 0.0),
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            embedding_model_version="bge-m3",
        )
    )
    assert [match.logical_object_id for match in replaced.matches] == ["chunk-new"]
    assert replaced.matches[0].source_index == "overlay"


def test_embedding_model_version_mismatch_returns_warning(tmp_path: Path) -> None:
    metadata_adapter, vector_adapter, _baseline_vectors, _delta_vectors = build_adapters(tmp_path)
    seed_file(
        metadata_adapter,
        target="baseline",
        file_id="file-api",
        relative_path="knowledge-sources/api/mismatch.md",
    )
    seed_chunk(
        metadata_adapter,
        target="baseline",
        chunk_id="chunk-mismatch",
        file_id="file-api",
        content_hash="hash:mismatch",
    )
    vector_adapter.writer(
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
    ).upsert_vector(
        VectorRefRecord(
            vector_ref_id="vec-mismatch",
            object_type="chunk",
            object_id="chunk-mismatch",
            chunk_id="chunk-mismatch",
            embedding_model_version="bge-small",
            content_hash="hash:mismatch",
            profile_id="watch",
        ),
        embedding=(1.0, 0.0),
    )

    result = vector_adapter.reader().search(
        VectorQuery(
            embedding=(1.0, 0.0),
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            embedding_model_version="bge-m3",
        )
    )

    assert result.matches == ()
    assert [warning.code for warning in result.warnings] == ["embedding.version_mismatch"]


def test_deleting_delta_vectors_does_not_modify_baseline_payloads(tmp_path: Path) -> None:
    metadata_adapter, vector_adapter, baseline_vectors, delta_vectors = build_adapters(tmp_path)
    seed_file(
        metadata_adapter,
        target="baseline",
        file_id="file-baseline",
        relative_path="knowledge-sources/api/base.md",
    )
    seed_chunk(
        metadata_adapter,
        target="baseline",
        chunk_id="chunk-baseline",
        file_id="file-baseline",
        content_hash="hash:baseline",
    )
    seed_file(
        metadata_adapter,
        target="overlay",
        file_id="file-overlay",
        relative_path="knowledge-sources/api/local.md",
    )
    seed_chunk(
        metadata_adapter,
        target="overlay",
        chunk_id="chunk-overlay",
        file_id="file-overlay",
        content_hash="hash:overlay",
    )

    vector_adapter.writer(
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
    ).upsert_vector(
        VectorRefRecord(
            vector_ref_id="vec-baseline",
            object_type="chunk",
            object_id="chunk-baseline",
            chunk_id="chunk-baseline",
            embedding_model_version="bge-m3",
            content_hash="hash:baseline",
            profile_id="watch",
        ),
        embedding=(1.0, 0.0),
    )
    overlay_writer = vector_adapter.writer(StorageWriteRequest(target="overlay"))
    overlay_writer.upsert_vector(
        VectorRefRecord(
            vector_ref_id="vec-overlay",
            object_type="chunk",
            object_id="chunk-overlay",
            chunk_id="chunk-overlay",
            embedding_model_version="bge-m3",
            content_hash="hash:overlay",
            profile_id="watch",
        ),
        embedding=(0.9, 0.1),
    )

    baseline_before = (baseline_vectors / "chunk.json").read_text(encoding="utf-8")
    deleted = overlay_writer.delete_object_vectors("chunk", ["chunk-overlay"])

    assert deleted == 1
    assert (baseline_vectors / "chunk.json").read_text(encoding="utf-8") == baseline_before
    assert read_collection(delta_vectors) == []
