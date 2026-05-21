from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID
from active_knowledge_server.indexing.embeddings import embed_text_locally
from active_knowledge_server.query import VectorRetriever, VectorSearchRequest
from active_knowledge_server.storage import (
    ChunkRecord,
    FileRecord,
    QueryScope,
    ReplacementRecord,
    StorageWriteRequest,
    TombstoneRecord,
    VectorRefRecord,
)
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
)


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
            "default_profile": "auto",
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


def build_adapters(
    config: ActiveKnowledgeConfig,
) -> tuple[SQLiteStorageAdapter, LanceDBVectorAdapter]:
    baseline_metadata = Path(config.storage.metadata.path)
    overlay_metadata = Path(config.storage.overlay.path)
    jobs_path = Path(config.storage.jobs.path)
    migrate_sqlite_store(baseline_metadata, target="baseline_metadata")
    migrate_sqlite_store(overlay_metadata, target="overlay_metadata")
    migrate_sqlite_store(jobs_path, target="jobs")

    metadata_adapter = SQLiteStorageAdapter(
        baseline_metadata_path=baseline_metadata,
        overlay_metadata_path=overlay_metadata,
        jobs_path=jobs_path,
    )
    vector_adapter = LanceDBVectorAdapter(
        baseline_vector_path=Path(config.storage.vector.path),
        delta_vector_path=Path(config.storage.vector_delta.path),
        metadata_adapter=metadata_adapter,
    )
    return metadata_adapter, vector_adapter


def test_vector_retriever_searches_baseline_and_delta_and_applies_source_index(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    metadata_adapter, vector_adapter = build_adapters(config)
    seed_chunk_with_vector(
        metadata_adapter,
        vector_adapter,
        target="baseline",
        file_id="file-baseline-sensor",
        chunk_id="chunk-baseline-sensor",
        relative_path="knowledge-sources/api/baseline_sensor.md",
        title="Baseline Sensor Guide",
        text="legacy sensor register documentation for runtime startup",
    )
    seed_chunk_with_vector(
        metadata_adapter,
        vector_adapter,
        target="overlay",
        file_id="file-overlay-sensor",
        chunk_id="chunk-overlay-sensor",
        relative_path="knowledge-sources/api/overlay_sensor.md",
        title="Overlay Sensor Guide",
        text="open the sensor register and return a runtime handle for sensor startup",
    )

    retriever = VectorRetriever.from_config(
        config,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )

    result = retriever.search(
        VectorSearchRequest(
            query="sensor register runtime handle",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
            top_k=4,
        )
    )

    assert result.retrieval_mode == "vector"
    assert result.fallback_matches == ()
    assert [item.source_index for item in result.matches] == ["overlay", "baseline"]
    assert all(item.match_reason.startswith("matched semantic similarity") for item in result.matches)
    assert {item.relative_path for item in result.matches} == {
        "knowledge-sources/api/baseline_sensor.md",
        "knowledge-sources/api/overlay_sensor.md",
    }

    baseline_only = retriever.search(
        VectorSearchRequest(
            query="sensor register runtime handle",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
            top_k=4,
            source_index="baseline",
        )
    )
    assert [item.source_index for item in baseline_only.matches] == ["baseline"]
    assert [item.relative_path for item in baseline_only.matches] == [
        "knowledge-sources/api/baseline_sensor.md"
    ]


def test_vector_retriever_filters_tombstone_and_replacement(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    metadata_adapter, vector_adapter = build_adapters(config)
    seed_chunk_with_vector(
        metadata_adapter,
        vector_adapter,
        target="baseline",
        file_id="file-runtime-baseline",
        chunk_id="chunk-runtime-old",
        relative_path="knowledge-sources/api/runtime_old.md",
        title="Old Runtime Notes",
        text="runtime sensor pipeline old behavior",
    )

    retriever = VectorRetriever.from_config(
        config,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    initial = retriever.search(
        VectorSearchRequest(
            query="runtime sensor pipeline",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
        )
    )
    assert [item.logical_object_id for item in initial.matches] == ["chunk-runtime-old"]

    metadata_adapter.writer(StorageWriteRequest(target="overlay")).upsert_tombstone(
        TombstoneRecord(
            tombstone_id="ts-runtime-old",
            object_type="chunk",
            object_id="chunk-runtime-old",
            reason="local delete",
            created_by_job="job-runtime-delete",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            profile_id="watch",
        )
    )
    after_tombstone = retriever.search(
        VectorSearchRequest(
            query="runtime sensor pipeline",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
        )
    )
    assert after_tombstone.matches == ()
    assert after_tombstone.fallback_matches == ()

    seed_chunk_with_vector(
        metadata_adapter,
        vector_adapter,
        target="overlay",
        file_id="file-runtime-overlay",
        chunk_id="chunk-runtime-new",
        relative_path="knowledge-sources/api/runtime_new.md",
        title="New Runtime Notes",
        text="runtime sensor pipeline new behavior with safer handle flow",
    )
    metadata_adapter.writer(StorageWriteRequest(target="overlay")).upsert_replacement(
        ReplacementRecord(
            replacement_id="rp-runtime-old-new",
            object_type="chunk",
            old_object_id="chunk-runtime-old",
            new_object_id="chunk-runtime-new",
            reason="rewrite",
            created_by_job="job-runtime-rewrite",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
        )
    )
    replaced = retriever.search(
        VectorSearchRequest(
            query="runtime sensor pipeline safer handle flow",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
        )
    )
    assert [item.logical_object_id for item in replaced.matches] == ["chunk-runtime-new"]
    assert [item.source_index for item in replaced.matches] == ["overlay"]


def test_vector_retriever_falls_back_to_fts_when_embeddings_disabled(tmp_path: Path) -> None:
    config = resolve_model(tmp_path, overrides={"indexing": {"embeddings": {"enabled": False}}})
    metadata_adapter, vector_adapter = build_adapters(config)
    seed_chunk_only(
        metadata_adapter,
        target="overlay",
        file_id="file-heart-tile",
        chunk_id="chunk-heart-tile",
        relative_path="knowledge-sources/widgets/heart_tile.md",
        title="Heart Tile Widget",
        text="heart tile shows bpm status and warning indicators on the watch face",
        doc_type="widget",
    )

    retriever = VectorRetriever.from_config(
        config,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    result = retriever.search(
        VectorSearchRequest(
            query="heart tile bpm status",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
            doc_type="widget",
        )
    )

    assert result.retrieval_mode == "fts_fallback"
    assert result.matches == ()
    assert result.fallback_matches
    assert result.fallback_matches[0].relative_path == "knowledge-sources/widgets/heart_tile.md"
    assert [warning.code for warning in result.warnings] == [
        "embedding.disabled",
        "retrieval.vector_fallback",
    ]


def test_vector_retriever_falls_back_to_fts_on_embedding_model_mismatch(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    metadata_adapter, vector_adapter = build_adapters(config)
    seed_chunk_with_vector(
        metadata_adapter,
        vector_adapter,
        target="baseline",
        file_id="file-mismatch",
        chunk_id="chunk-mismatch",
        relative_path="knowledge-sources/api/mismatch.md",
        title="Mismatch Sensor Guide",
        text="sensor semantic fallback should still find this guide",
        embedding_model_version="bge-small",
    )

    retriever = VectorRetriever.from_config(
        config,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    result = retriever.search(
        VectorSearchRequest(
            query="sensor semantic fallback guide",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
        )
    )

    assert result.retrieval_mode == "fts_fallback"
    assert result.matches == ()
    assert result.fallback_matches
    assert result.fallback_matches[0].relative_path == "knowledge-sources/api/mismatch.md"
    assert [warning.code for warning in result.warnings] == [
        "embedding.version_mismatch",
        "retrieval.vector_fallback",
    ]


def seed_chunk_with_vector(
    metadata_adapter: SQLiteStorageAdapter,
    vector_adapter: LanceDBVectorAdapter,
    *,
    target: str,
    file_id: str,
    chunk_id: str,
    relative_path: str,
    title: str,
    text: str,
    doc_type: str = "api",
    domain: str = "engineering",
    profile_id: str = "watch",
    embedding_model_version: str = "bge-m3",
) -> None:
    seed_chunk_only(
        metadata_adapter,
        target=target,
        file_id=file_id,
        chunk_id=chunk_id,
        relative_path=relative_path,
        title=title,
        text=text,
        doc_type=doc_type,
        domain=domain,
        profile_id=profile_id,
    )
    request = (
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
        if target == "baseline"
        else StorageWriteRequest(target="overlay")
    )
    vector_writer = vector_adapter.writer(request)
    vector_writer.upsert_vector(
        VectorRefRecord(
            vector_ref_id=f"vector:{chunk_id}:{embedding_model_version}",
            object_type="chunk",
            object_id=chunk_id,
            chunk_id=chunk_id,
            embedding_model_version=embedding_model_version,
            content_hash=f"hash:{chunk_id}",
            source_scope="api" if doc_type == "api" else "widgets",
            profile_id=profile_id,
            metadata={
                "provider": "local",
                "title": title,
                "domain": domain,
                "doc_type": doc_type,
            },
        ),
        embedding=embed_text_locally(text),
    )
    vector_writer.flush()


def seed_chunk_only(
    metadata_adapter: SQLiteStorageAdapter,
    *,
    target: str,
    file_id: str,
    chunk_id: str,
    relative_path: str,
    title: str,
    text: str,
    doc_type: str = "api",
    domain: str = "engineering",
    profile_id: str = "watch",
) -> None:
    writer = metadata_adapter.writer(StorageWriteRequest(target=target))
    writer.upsert_file(
        FileRecord(
            file_id=file_id,
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source_id="knowledge-widgets" if doc_type == "widget" else "knowledge-api",
            relative_path=relative_path,
            content_hash=f"hash:{file_id}",
            source_scope="widgets" if doc_type == "widget" else "api",
            profile_id=profile_id,
            language="md",
        )
    )
    writer.upsert_chunk(
        ChunkRecord(
            chunk_id=chunk_id,
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id=file_id,
            content_hash=f"hash:{chunk_id}",
            chunk_type="doc.section",
            ordinal=0,
            text=text,
            source_scope="widgets" if doc_type == "widget" else "api",
            profile_id=profile_id,
            metadata={
                "title": title,
                "domain": domain,
                "doc_type": doc_type,
            },
        )
    )
    writer.flush()


def deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    merged = dict(base)
    for key, value in overrides.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            nested = merged[key]
            assert isinstance(nested, dict)
            merged[key] = deep_merge(nested, value)
            continue
        merged[key] = value
    return merged