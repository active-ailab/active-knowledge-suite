from __future__ import annotations

import json
from pathlib import Path

import pytest

from active_knowledge_server.storage import (
    ChunkRecord,
    FileRecord,
    QueryScope,
    ReplacementRecord,
    StorageWriteRequest,
    TombstoneRecord,
    VectorQuery,
    VectorRefRecord,
    lancedb_store,
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


def read_manifest(path: Path) -> dict[str, object]:
    manifest = path / "manifest.json"
    assert manifest.exists()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def read_segment_rows(path: Path, object_type: str = "chunk") -> list[dict[str, object]]:
    directory = path / object_type
    rows: list[dict[str, object]] = []
    if not directory.exists():
        return rows
    for segment in sorted(directory.glob("*.jsonl")):
        for line in segment.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            assert isinstance(payload, dict)
            rows.append(payload)
    return rows


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
    assert [row["vector_ref_id"] for row in read_segment_rows(delta_vectors)] == ["vec-overlay"]
    manifest = read_manifest(delta_vectors)
    chunk_manifest = manifest["collections"]["chunk"]
    assert chunk_manifest["segment_count"] == 1

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


def test_vector_batch_upsert_writes_collection_and_metadata_refs(tmp_path: Path) -> None:
    metadata_adapter, vector_adapter, _baseline_vectors, delta_vectors = build_adapters(tmp_path)
    seed_file(
        metadata_adapter,
        target="overlay",
        file_id="file-overlay",
        relative_path="knowledge-sources/api/batch.md",
    )
    seed_chunk(
        metadata_adapter,
        target="overlay",
        chunk_id="chunk-a",
        file_id="file-overlay",
        content_hash="hash:a",
    )
    seed_chunk(
        metadata_adapter,
        target="overlay",
        chunk_id="chunk-b",
        file_id="file-overlay",
        content_hash="hash:b",
    )

    writer = vector_adapter.writer(StorageWriteRequest(target="overlay"))
    written = writer.upsert_vectors(
        (
            (
                VectorRefRecord(
                    vector_ref_id="vec-a",
                    object_type="chunk",
                    object_id="chunk-a",
                    chunk_id="chunk-a",
                    embedding_model_version="bge-m3",
                    content_hash="hash:a",
                    profile_id="watch",
                ),
                (1.0, 0.0),
            ),
            (
                VectorRefRecord(
                    vector_ref_id="vec-b",
                    object_type="chunk",
                    object_id="chunk-b",
                    chunk_id="chunk-b",
                    embedding_model_version="bge-m3",
                    content_hash="hash:b",
                    profile_id="watch",
                ),
                (0.0, 1.0),
            ),
        )
    )

    assert [record.vector_ref_id for record in written] == ["vec-a", "vec-b"]
    assert [row["vector_ref_id"] for row in read_segment_rows(delta_vectors)] == ["vec-a", "vec-b"]
    chunk_a = metadata_adapter.reader().get_chunk("chunk-a")
    chunk_b = metadata_adapter.reader().get_chunk("chunk-b")
    assert chunk_a is not None
    assert chunk_b is not None
    assert chunk_a.metadata["embedding_ref"] == "vec-a"
    assert chunk_b.metadata["embedding_ref"] == "vec-b"


def test_vector_upsert_validates_payload_before_metadata_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_adapter, vector_adapter, _baseline_vectors, delta_vectors = build_adapters(tmp_path)
    seed_file(
        metadata_adapter,
        target="overlay",
        file_id="file-overlay",
        relative_path="knowledge-sources/api/bad-vector.md",
    )
    seed_chunk(
        metadata_adapter,
        target="overlay",
        chunk_id="chunk-bad",
        file_id="file-overlay",
        content_hash="hash:bad",
    )
    def drop_collection_rows(
        root: Path,
        object_type: lancedb_store.VectorObjectType,
        rows: object,
        *,
        job_id: str | None = None,
    ) -> None:
        return None

    monkeypatch.setattr(lancedb_store, "append_collection_segment", drop_collection_rows)
    writer = vector_adapter.writer(StorageWriteRequest(target="overlay"))

    with pytest.raises(RuntimeError, match="vector payload validation failed"):
        writer.upsert_vector(
            VectorRefRecord(
                vector_ref_id="vec-bad",
                object_type="chunk",
                object_id="chunk-bad",
                chunk_id="chunk-bad",
                embedding_model_version="bge-m3",
                content_hash="hash:bad",
                profile_id="watch",
            ),
            embedding=(0.4, 0.6),
        )

    assert metadata_adapter.reader().get_vector_ref("vec-bad") is None
    assert read_collection(delta_vectors) == []


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


def test_overlay_vector_segments_compact_after_threshold_flush(tmp_path: Path) -> None:
    metadata_adapter, vector_adapter, _baseline_vectors, delta_vectors = build_adapters(tmp_path)
    seed_file(
        metadata_adapter,
        target="overlay",
        file_id="file-overlay",
        relative_path="knowledge-sources/api/compact.md",
    )
    writer = vector_adapter.writer(StorageWriteRequest(target="overlay", job_id="job-compact"))

    for index in range(8):
        chunk_id = f"chunk-{index}"
        seed_chunk(
            metadata_adapter,
            target="overlay",
            chunk_id=chunk_id,
            file_id="file-overlay",
            content_hash=f"hash:{index}",
        )
        writer.upsert_vector(
            VectorRefRecord(
                vector_ref_id=f"vec-{index}",
                object_type="chunk",
                object_id=chunk_id,
                chunk_id=chunk_id,
                embedding_model_version="bge-m3",
                content_hash=f"hash:{index}",
                profile_id="watch",
            ),
            embedding=(1.0, float(index)),
        )

    assert len(read_segment_rows(delta_vectors)) == 8
    writer.flush()

    compacted = read_collection(delta_vectors)
    assert len(compacted) == 8
    assert not (delta_vectors / "chunk").exists()
    manifest = read_manifest(delta_vectors)
    chunk_manifest = manifest["collections"]["chunk"]
    assert chunk_manifest["segment_count"] == 0
    assert chunk_manifest["last_compaction"]["requested_by_job_id"] == "job-compact"


def test_orphan_overlay_vector_segment_is_hidden_without_metadata_ref(tmp_path: Path) -> None:
    metadata_adapter, vector_adapter, _baseline_vectors, delta_vectors = build_adapters(tmp_path)
    seed_file(
        metadata_adapter,
        target="overlay",
        file_id="file-overlay",
        relative_path="knowledge-sources/api/orphan.md",
    )
    seed_chunk(
        metadata_adapter,
        target="overlay",
        chunk_id="chunk-orphan",
        file_id="file-overlay",
        content_hash="hash:orphan",
    )
    lancedb_store.append_collection_segment(
        delta_vectors,
        "chunk",
        (
            lancedb_store._VectorRow(  # type: ignore[attr-defined]
                vector_ref_id="vec-orphan",
                object_type="chunk",
                object_id="chunk-orphan",
                chunk_id="chunk-orphan",
                embedding_model_version="bge-m3",
                content_hash="hash:orphan",
                source_scope="all",
                profile_id="watch",
                embedding=(1.0, 0.0),
                metadata={},
            ),
        ),
        job_id="job-orphan",
    )

    result = vector_adapter.reader().search(
        VectorQuery(
            embedding=(1.0, 0.0),
            scope=QueryScope(snapshot_id="current", profile_id="watch"),
            embedding_model_version="bge-m3",
        )
    )

    assert result.matches == ()
