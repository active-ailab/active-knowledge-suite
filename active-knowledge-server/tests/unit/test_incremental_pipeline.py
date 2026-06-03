from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.source_docs import SourceDocsScanProgress
from active_knowledge_server.connectors.workspace import WorkspaceScanProgress
from active_knowledge_server.indexing import (
    CURRENT_SNAPSHOT_ID,
    CodeIndexer,
    CollectedProfiles,
    DocumentIndexer,
    IncrementalIndexPipeline,
    IndexedCode,
    IndexedDocuments,
    ProfileCollector,
    ProfileConditionedRelationExtractor,
    make_index_plan_signature,
    make_index_task_list,
    summarize_entity_profile_states_from_reader,
)
from active_knowledge_server.indexing.jobs import (
    SQLiteJobStore,
    decode_task_checkpoint,
    record_task_applied_checkpoint,
    task_checkpoint_key,
)
from active_knowledge_server.indexing.pipeline import (
    IndexRunContext,
    _format_discover_target,
    _format_source_docs_discover_message,
    _format_workspace_discover_message,
)
from active_knowledge_server.indexing.progress import IndexProgressEvent
from active_knowledge_server.storage import QueryScope, StorageWriteRequest
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
)


class BrokenDocumentIndexer:
    def collect(self, *, snapshot_id: str, source_docs_manifest: object) -> object:
        raise RuntimeError(f"synthetic doc failure for {snapshot_id}")


class SpyCodeIndexer:
    def __init__(self, inner: CodeIndexer) -> None:
        self.inner = inner
        self.include_paths_calls: list[tuple[str, ...] | None] = []

    def collect(
        self,
        *,
        snapshot_id: str,
        workspace_inventory: Any,
        include_paths: tuple[str, ...] | None = None,
        progress_callback: Any | None = None,
    ) -> IndexedCode:
        self.include_paths_calls.append(include_paths)
        return self.inner.collect(
            snapshot_id=snapshot_id,
            workspace_inventory=workspace_inventory,
            include_paths=include_paths,
            progress_callback=progress_callback,
        )


class FailingCodeApplyPipeline(IncrementalIndexPipeline):
    def _apply_code_bundle(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic code apply failure")


class InterruptBeforeCodeCheckpointPipeline(IncrementalIndexPipeline):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.interrupted_paths: list[str] = []

    def _apply_code_bundle(
        self,
        reader: Any,
        writer: Any,
        *,
        relative_path: str,
        new_bundle: Any,
        snapshot_id: str,
    ) -> None:
        super()._apply_code_bundle(
            reader,
            writer,
            relative_path=relative_path,
            new_bundle=new_bundle,
            snapshot_id=snapshot_id,
        )
        self.interrupted_paths.append(relative_path)
        if len(self.interrupted_paths) == 1:
            raise KeyboardInterrupt


class SpyVectorWriter:
    def __init__(self, inner: Any, upsert_batches: list[tuple[object, ...]]) -> None:
        self._inner = inner
        self._upsert_batches = upsert_batches

    @property
    def request(self) -> StorageWriteRequest:
        return self._inner.request

    def upsert_vector(self, record: object, embedding: object) -> object:
        self._upsert_batches.append(((record, embedding),))
        return self._inner.upsert_vector(record, embedding)

    def upsert_vectors(self, records: object) -> object:
        batch = tuple(records)
        self._upsert_batches.append(batch)
        return self._inner.upsert_vectors(batch)

    def delete_object_vectors(self, object_type: object, object_ids: object) -> object:
        return self._inner.delete_object_vectors(object_type, object_ids)

    def flush(self) -> None:
        self._inner.flush()


class SpyVectorAdapter:
    def __init__(self, inner: LanceDBVectorAdapter) -> None:
        self._inner = inner
        self.upsert_batches: list[tuple[object, ...]] = []

    def writer(self, request: StorageWriteRequest) -> SpyVectorWriter:
        return SpyVectorWriter(self._inner.writer(request), self.upsert_batches)

    def reader(self) -> object:
        return self._inner.reader()

    def close(self) -> None:
        self._inner.close()


class FailingVectorWriter:
    @property
    def request(self) -> StorageWriteRequest:
        return StorageWriteRequest(target="overlay")

    def upsert_vector(self, record: object, embedding: object) -> object:
        raise RuntimeError("vector payload validation failed")

    def upsert_vectors(self, records: object) -> object:
        raise RuntimeError("vector payload validation failed")

    def delete_object_vectors(self, object_type: object, object_ids: object) -> int:
        return 0

    def flush(self) -> None:
        return None


class FailingVectorAdapter:
    def writer(self, request: StorageWriteRequest) -> FailingVectorWriter:
        return FailingVectorWriter()

    def reader(self) -> object:
        raise AssertionError("reader is not used by this test")

    def close(self) -> None:
        return None


def resolve_model(tmp_path: Path, overrides: ConfigDict | None = None) -> ActiveKnowledgeConfig:
    workspace = tmp_path / "workspace"
    docs = tmp_path / "knowledge-sources"
    workspace.mkdir(exist_ok=True)
    docs.mkdir(exist_ok=True)
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
        "profiles": {
            "discovery": {
                "defconfig_roots": ["configs"],
                "dotconfig_candidates": [
                    "build/.config",
                    "build/out_hub/.config",
                    "build/out_lite/.config",
                ],
            }
        },
        "storage": {
            "baseline": {"manifest": str(tmp_path / ".active-kb" / "baseline" / "manifest.json")},
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
) -> tuple[SQLiteStorageAdapter, LanceDBVectorAdapter, Path, Path]:
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
    baseline_vectors = Path(config.storage.vector.path)
    delta_vectors = Path(config.storage.vector_delta.path)
    vector_adapter = LanceDBVectorAdapter(
        baseline_vector_path=baseline_vectors,
        delta_vector_path=delta_vectors,
        metadata_adapter=metadata_adapter,
    )
    return metadata_adapter, vector_adapter, baseline_vectors, delta_vectors


def seed_baseline(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
    include_docs: bool = False,
    include_profiles: bool = False,
    include_profile_relations: bool = False,
) -> tuple[
    SQLiteStorageAdapter,
    LanceDBVectorAdapter,
    IncrementalIndexPipeline,
    IndexedCode,
    IndexedDocuments | None,
    CollectedProfiles | None,
]:
    metadata_adapter, vector_adapter, _baseline_vectors, _delta_vectors = build_adapters(config)
    metadata_writer = metadata_adapter.writer(
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
    )
    vector_writer = vector_adapter.writer(
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
    )
    indexed_code = CodeIndexer.from_config(config, cwd=cwd).collect_and_store(
        metadata_writer,
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )
    indexed_docs = None
    if include_docs:
        indexed_docs = DocumentIndexer.from_config(config, cwd=cwd).collect_and_store(
            metadata_writer,
            vector_writer=vector_writer,
            snapshot_id=CURRENT_SNAPSHOT_ID,
        )
    collected_profiles = None
    if include_profiles:
        collected_profiles = ProfileCollector.from_config(config, cwd=cwd).collect_and_store(
            metadata_writer,
            snapshot_id=CURRENT_SNAPSHOT_ID,
        )
    if include_profile_relations:
        assert collected_profiles is not None
        ProfileConditionedRelationExtractor().collect_and_store(
            metadata_writer,
            snapshot_id=CURRENT_SNAPSHOT_ID,
            profiles=collected_profiles.profile_records,
            entities=indexed_code.entity_records,
            relations=indexed_code.relation_records,
        )
    metadata_writer.flush()
    vector_writer.flush()

    pipeline = IncrementalIndexPipeline(
        config,
        cwd=cwd,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    state, *_ = pipeline.capture_state(snapshot_id=CURRENT_SNAPSHOT_ID)
    pipeline.save_state(state)
    return (
        metadata_adapter,
        vector_adapter,
        pipeline,
        indexed_code,
        indexed_docs,
        collected_profiles,
    )


def read_vector_collection(path: Path, object_type: str = "chunk") -> list[dict[str, object]]:
    collection = path / f"{object_type}.json"
    if not collection.exists():
        return []
    payload = json.loads(collection.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return payload


def overlay_count(path: Path, query: str, params: tuple[object, ...]) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute(query, params).fetchone()
    assert row is not None
    return int(row[0])


def test_incremental_pipeline_plans_rebuilds_for_schema_and_embedding_changes(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    write_workspace_fixture(workspace_root)
    write_doc_fixture(docs_root)
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
        extra_config="CONFIG_HEALTH_BT=y\n",
    )

    pipeline = IncrementalIndexPipeline(config, cwd=tmp_path)
    current_state, *_ = pipeline.capture_state(snapshot_id=CURRENT_SNAPSHOT_ID)
    pipeline.save_state(
        replace(
            current_state,
            code_indexer_schema_version="code_indexer.v0",
            doc_indexer_schema_version="doc_indexer.v0",
            profile_collector_schema_version="profile_collector.v0",
            profile_conditioned_relation_schema_version="profile_relation.v0",
            embedding_model_version="text-embedding-legacy",
        )
    )

    plan = pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="all")

    assert plan.reindex_all_code is True
    assert plan.reindex_all_docs is True
    assert plan.rebuild_vectors is True
    assert plan.rebuild_profile_conditioned_relations is True
    assert {warning.code for warning in plan.warnings} >= {
        "index.code_schema_changed",
        "index.doc_schema_changed",
        "index.embedding_model_changed",
    }


def test_incremental_pipeline_migrates_local_sqlite_stores_before_run(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)

    overlay_path = Path(config.storage.overlay.path)
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(overlay_path).close()

    pipeline = IncrementalIndexPipeline(config, cwd=tmp_path)

    result = pipeline.run(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")

    assert result.result_status == "ready"
    with sqlite3.connect(overlay_path) as connection:
        overlay_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "chunk" in overlay_tables
    assert "schema_version" in overlay_tables

    jobs_path = Path(config.storage.jobs.path)
    with sqlite3.connect(jobs_path) as connection:
        jobs_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "job" in jobs_tables


def test_incremental_pipeline_replaces_changed_baseline_code_without_rebuilding_docs(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    write_workspace_fixture(workspace_root)
    write_doc_fixture(docs_root)

    metadata_adapter, _vector_adapter, pipeline, indexed_code, _indexed_docs, _profiles = (
        seed_baseline(
            config,
            cwd=tmp_path,
            include_docs=True,
        )
    )
    (workspace_root / "components" / "health" / "main.c").write_text(
        """/* Health subsystem runtime entrypoints. */
#include "health.h"

#define HEALTH_BOOTSTRAP 7

int health_bootstrap(void)
{
    return HEALTH_BOOTSTRAP;
}
""",
        encoding="utf-8",
    )

    result = pipeline.run(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")

    assert result.result_status == "ready"
    assert result.plan.changed_code_paths == ("components/health/main.c",)
    assert result.plan.changed_doc_paths == ()

    reader = metadata_adapter.reader()
    scope = QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, source_scope="components")
    file_paths = {record.file_id: record.relative_path for record in reader.iter_files(scope)}
    main_names = {
        item.record.name
        for item in reader.logical_entities(scope)
        if file_paths.get(item.record.file_id) == "components/health/main.c"
    }
    assert "HEALTH_BOOTSTRAP" in main_names
    assert "HEALTH_DEFAULT" not in main_names

    overlay_path = Path(config.storage.overlay.path)
    replacement_count = overlay_count(
        overlay_path,
        "SELECT COUNT(*) FROM replacement WHERE object_type = ?",
        ("chunk",),
    )
    assert replacement_count >= 1

    with sqlite3.connect(overlay_path) as connection:
        overlay_files = {
            row[0] for row in connection.execute("SELECT relative_path FROM file").fetchall()
        }
    assert "knowledge-sources/api/sensor.md" not in overlay_files


def test_incremental_pipeline_tombstones_deleted_baseline_files(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)

    metadata_adapter, _vector_adapter, pipeline, indexed_code, _indexed_docs, _profiles = (
        seed_baseline(config, cwd=tmp_path)
    )
    deleted_file = next(
        record
        for record in indexed_code.file_records
        if record.relative_path == "components/health/bt.c"
    )
    (workspace_root / "components" / "health" / "bt.c").unlink()

    result = pipeline.run(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")

    assert result.result_status == "ready"
    assert result.plan.deleted_code_paths == ("components/health/bt.c",)

    reader = metadata_adapter.reader()
    scope = QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, source_scope="components")
    assert reader.is_tombstoned("file", deleted_file.file_id, scope) is True
    assert all(
        item.record.file_id != deleted_file.file_id
        for item in reader.logical_entities(QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID))
    )

    overlay_path = Path(config.storage.overlay.path)
    tombstone_count = overlay_count(
        overlay_path,
        "SELECT COUNT(*) FROM tombstone WHERE object_type = ? AND object_id = ?",
        ("file", deleted_file.file_id),
    )
    assert tombstone_count == 1


def test_incremental_pipeline_rebuilds_profile_relations_when_dotconfig_changes(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
        extra_config="CONFIG_HEALTH_BT=y\n",
    )

    metadata_adapter, _vector_adapter, pipeline, indexed_code, _indexed_docs, profiles = (
        seed_baseline(
            config,
            cwd=tmp_path,
            include_profiles=True,
            include_profile_relations=True,
        )
    )
    assert profiles is not None
    bt_symbol_entity_id = next(
        record.entity_id
        for record in indexed_code.entity_records
        if record.entity_type == "Macro" and record.name == "BT_READY"
    )
    (workspace_root / "build" / ".config").write_text(
        'CONFIG_APP="watch"\nCONFIG_BOARD="mhs003"\nCONFIG_RUNTIME_READY=y\n'
        "# CONFIG_HEALTH_BT is not set\n",
        encoding="utf-8",
    )

    events: list[IndexProgressEvent] = []
    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="code",
        progress_callback=events.append,
    )

    assert result.result_status == "ready"
    assert result.plan.rebuild_profile_conditioned_relations is True
    assert result.plan.changed_profile_ids == ("mhs003_watch",)
    profile_relation_events = [event for event in events if event.phase == "profile_relations"]
    assert profile_relation_events
    assert profile_relation_events[0].stage_done == 0
    assert any(
        event.message == "Flushing profile-conditioned relation updates"
        for event in profile_relation_events
    )
    assert profile_relation_events[-1].stage_done == 1

    states = summarize_entity_profile_states_from_reader(
        metadata_adapter.reader(),
        entity_id=bt_symbol_entity_id,
        profiles=result.plan.collected_profiles.profile_records,
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )
    states_by_profile = {state.profile_id: state for state in states}
    assert states_by_profile["mhs003_watch"].status == "disabled"

    workspace_map_path = (
        Path(config.storage.local_artifacts_root) / "workspace-maps" / "current.json"
    )
    payload = json.loads(workspace_map_path.read_text(encoding="utf-8"))
    profile_items = {item["name"]: item for item in payload["views"]["profile"]["items"]}
    assert profile_items["mhs003_watch"]["metadata"]["counts"]["disabled"] > 0


def test_incremental_pipeline_rebuilds_doc_vectors_on_embedding_model_change(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    write_workspace_fixture(workspace_root)
    write_doc_fixture(docs_root)

    seed_baseline(
        config,
        cwd=tmp_path,
        include_docs=True,
    )
    updated_config = resolve_model(
        tmp_path,
        overrides={
            "indexing": {
                "embeddings": {
                    "model": "text-embedding-local-v2",
                }
            }
        },
    )
    metadata_adapter, vector_adapter, _baseline_vectors, delta_vectors = build_adapters(
        updated_config
    )
    pipeline = IncrementalIndexPipeline(
        updated_config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )

    events: list[IndexProgressEvent] = []
    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="docs",
        progress_callback=events.append,
    )

    assert result.result_status == "ready"
    assert result.plan.changed_doc_paths == ()
    assert result.plan.rebuild_vectors is True
    assert any(
        event.phase == "vectors_apply"
        and event.message == "Flushing document vectors to overlay store"
        for event in events
    )

    stored_vectors = read_vector_collection(delta_vectors)
    assert stored_vectors
    assert {row["embedding_model_version"] for row in stored_vectors} == {"text-embedding-local-v2"}


def test_incremental_pipeline_checkpoints_successful_doc_vector_tasks(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    write_workspace_fixture(workspace_root)
    write_doc_fixture(docs_root)
    seed_baseline(config, cwd=tmp_path, include_docs=True)
    updated_config = resolve_model(
        tmp_path,
        overrides={"indexing": {"embeddings": {"model": "text-embedding-local-v2"}}},
    )
    metadata_adapter, vector_adapter, _baseline_vectors, _delta_vectors = build_adapters(
        updated_config
    )
    pipeline = IncrementalIndexPipeline(
        updated_config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    plan = pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="docs")
    signature = make_index_plan_signature(
        plan,
        config=updated_config,
        mode="incremental",
        target="local",
    )
    store = SQLiteJobStore(Path(updated_config.storage.jobs.path))
    job = store.create_job(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        metadata={"plan_signature": signature.digest},
    )

    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="docs",
        plan=plan,
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            plan_signature=signature,
            resume_policy={"mode": "auto"},
        ),
    )

    assert result.result_status == "ready"
    assert result.plan.rebuild_vectors is True
    tasks = {task.task_key: task for task in make_index_task_list(plan)}
    checkpoint = decode_task_checkpoint(
        store.get_checkpoint(
            job.job_id,
            task_checkpoint_key(tasks["vector:doc:api/sensor.md"]),
        )
    )

    assert checkpoint is not None
    assert checkpoint.status == "applied"
    assert checkpoint.metadata["job_id"] == job.job_id
    assert checkpoint.metadata["plan_signature"] == signature.digest
    assert checkpoint.metadata["operation"] == "doc"
    assert checkpoint.metadata["source_kind"] == "vector"
    assert checkpoint.metadata["relative_path"] == "api/sensor.md"
    assert checkpoint.metadata["storage_relative_path"] == "knowledge-sources/api/sensor.md"
    assert checkpoint.metadata["record_counts"]["vector_refs"] >= 1
    assert checkpoint.metadata["embedding_models"] == ["text-embedding-local-v2"]


def test_incremental_pipeline_skips_applied_doc_vector_task_without_payload_rewrite(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    write_workspace_fixture(workspace_root)
    write_doc_fixture(docs_root)
    seed_baseline(config, cwd=tmp_path, include_docs=True)
    updated_config = resolve_model(
        tmp_path,
        overrides={"indexing": {"embeddings": {"model": "text-embedding-local-v2"}}},
    )
    metadata_adapter, vector_adapter, _baseline_vectors, delta_vectors = build_adapters(
        updated_config
    )
    pipeline = IncrementalIndexPipeline(
        updated_config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    plan = pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="docs")
    signature = make_index_plan_signature(
        plan,
        config=updated_config,
        mode="incremental",
        target="local",
    )
    vector_task = next(
        task for task in make_index_task_list(plan) if task.task_key == "vector:doc:api/sensor.md"
    )
    store = SQLiteJobStore(Path(updated_config.storage.jobs.path))
    job = store.create_job(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        metadata={"plan_signature": signature.digest},
    )
    record_task_applied_checkpoint(
        store,
        job.job_id,
        vector_task,
        metadata={"plan_signature": signature.digest},
    )
    spy_vector_adapter = SpyVectorAdapter(vector_adapter)
    pipeline = IncrementalIndexPipeline(
        updated_config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=spy_vector_adapter,  # type: ignore[arg-type]
    )

    events: list[IndexProgressEvent] = []
    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="docs",
        plan=plan,
        progress_callback=events.append,
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            plan_signature=signature,
            resume_policy={"mode": "auto"},
        ),
    )

    assert result.result_status == "ready"
    assert result.metadata["tasks"]["skipped"] == 1
    assert spy_vector_adapter.upsert_batches == []
    assert read_vector_collection(delta_vectors) == []
    assert any(
        event.message == "Skipping previously applied task"
        and event.current_path == "api/sensor.md"
        for event in events
    )


def test_incremental_pipeline_does_not_checkpoint_failed_doc_vector_task(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    write_workspace_fixture(workspace_root)
    write_doc_fixture(docs_root)
    seed_baseline(config, cwd=tmp_path, include_docs=True)
    updated_config = resolve_model(
        tmp_path,
        overrides={"indexing": {"embeddings": {"model": "text-embedding-local-v2"}}},
    )
    metadata_adapter, _vector_adapter, _baseline_vectors, _delta_vectors = build_adapters(
        updated_config
    )
    healthy_pipeline = IncrementalIndexPipeline(
        updated_config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
    )
    plan = healthy_pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="docs")
    signature = make_index_plan_signature(
        plan,
        config=updated_config,
        mode="incremental",
        target="local",
    )
    vector_task = next(
        task for task in make_index_task_list(plan) if task.task_key == "vector:doc:api/sensor.md"
    )
    store = SQLiteJobStore(Path(updated_config.storage.jobs.path))
    job = store.create_job(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        metadata={"plan_signature": signature.digest},
    )
    failing_pipeline = IncrementalIndexPipeline(
        updated_config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=FailingVectorAdapter(),  # type: ignore[arg-type]
    )

    result = failing_pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="docs",
        plan=plan,
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            plan_signature=signature,
            resume_policy={"mode": "auto"},
        ),
    )

    assert result.result_status == "partial_ready"
    assert result.metadata["tasks"]["failed"] == 1
    assert store.get_checkpoint(job.job_id, task_checkpoint_key(vector_task)) is None


def test_incremental_pipeline_returns_partial_ready_when_doc_increment_fails(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    write_workspace_fixture(workspace_root)
    write_doc_fixture(docs_root)

    metadata_adapter, vector_adapter, _baseline_vectors, _delta_vectors = build_adapters(config)
    baseline_writer = metadata_adapter.writer(
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
    )
    baseline_vector_writer = vector_adapter.writer(
        StorageWriteRequest(target="baseline", operation_mode="baseline_publish")
    )
    DocumentIndexer.from_config(config, cwd=tmp_path).collect_and_store(
        baseline_writer,
        vector_writer=baseline_vector_writer,
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )
    baseline_writer.flush()
    baseline_vector_writer.flush()

    healthy_pipeline = IncrementalIndexPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    previous_state, *_ = healthy_pipeline.capture_state(snapshot_id=CURRENT_SNAPSHOT_ID)
    healthy_pipeline.save_state(previous_state)

    (docs_root / "api" / "sensor.md").write_text(
        """---
title: Sensor Register API
authority_level: official
version: 1.3.0
module: sensor
code_symbols:
  - sensor_open
tags:
  - sensor
---
# Sensor Register API

## sensor_open
This change should trip the synthetic failure path.
""",
        encoding="utf-8",
    )

    failing_pipeline = IncrementalIndexPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
        doc_indexer=BrokenDocumentIndexer(),
    )
    result = failing_pipeline.run(snapshot_id=CURRENT_SNAPSHOT_ID, source="docs")

    assert result.result_status == "partial_ready"
    assert any(warning.code == "index.doc_incremental_failed" for warning in result.warnings)
    assert failing_pipeline.load_state() == previous_state


def test_incremental_pipeline_emits_monotonic_progress_events(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    write_workspace_fixture(workspace_root)
    write_doc_fixture(docs_root)
    _metadata_adapter, _vector_adapter, pipeline, _indexed_code, _indexed_docs, _profiles = (
        seed_baseline(
            config,
            cwd=tmp_path,
            include_docs=True,
        )
    )
    (workspace_root / "components" / "health" / "main.c").write_text(
        """/* Health subsystem runtime entrypoints. */
#include "health.h"

int health_progress_probe(void)
{
    return 7;
}
""",
        encoding="utf-8",
    )

    events: list[IndexProgressEvent] = []
    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="code",
        progress_callback=events.append,
    )

    assert result.result_status == "ready"
    assert events
    assert events[0].phase == "discover"
    discover_events = [event for event in events if event.phase == "discover"]
    assert discover_events[0].stage_done == 0
    assert discover_events[-1].stage_done == 3
    assert [event.stage_done for event in discover_events] == sorted(
        event.stage_done for event in discover_events
    )
    assert any(
        event.message is not None and "workspace inventory:" in event.message
        for event in discover_events
    )
    code_collect_events = [event for event in events if event.phase == "code_collect"]
    assert code_collect_events
    assert code_collect_events[0].stage_done == 0
    assert "worker" in (code_collect_events[0].message or "")
    code_finalize_events = [event for event in events if event.phase == "code_finalize"]
    assert code_finalize_events
    assert any("assembling" in (event.message or "").lower() for event in code_finalize_events)
    assert any(
        event.phase == "code_apply" and event.message == "Flushing code changes to overlay metadata"
        for event in events
    )
    workspace_map_events = [event for event in events if event.phase == "workspace_map"]
    assert workspace_map_events
    assert workspace_map_events[0].stage_done == 0
    assert workspace_map_events[-1].stage_done == 1
    assert events[-1].phase == "done"
    assert events[-1].global_done == events[-1].global_total
    assert [event.global_done for event in events if event.global_done is not None] == sorted(
        event.global_done for event in events if event.global_done is not None
    )


def test_format_discover_target_summarizes_directory_and_file_tails() -> None:
    assert (
        _format_discover_target(
            ".repo/project-objects/1f/pack",
            root_label="workspace root",
            kind="directory",
        )
        == "1f/pack"
    )
    assert (
        _format_discover_target(
            "knowledge-sources/engineering/platform/sensor.md",
            root_label="source docs root",
            kind="file",
        )
        == "engineering/platform/sensor.md"
    )


def test_format_discover_messages_prefer_directory_summary() -> None:
    workspace_message = _format_workspace_discover_message(
        WorkspaceScanProgress(
            kind="directory",
            relative_path=".repo/project-objects/1f/pack",
            display_path=".repo/project-objects/1f/pack",
            files_scanned=0,
            directories_scanned=716,
        )
    )
    source_docs_message = _format_source_docs_discover_message(
        SourceDocsScanProgress(
            kind="directory",
            relative_path="engineering/platform/widgets",
            display_path="engineering/platform/widgets",
            category="engineering",
            files_scanned=12,
            directories_scanned=8,
        )
    )

    assert workspace_message == "Scanning workspace inventory: 1f/pack (0 files, 716 directories)"
    assert source_docs_message == (
        "Scanning source documents: platform/widgets (12 files, 8 directories)"
    )


def test_incremental_pipeline_collects_only_changed_code_bundle_inputs(tmp_path: Path) -> None:
    config = resolve_model(tmp_path, overrides={"indexing": {"workers": 2}})
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)
    metadata_adapter, vector_adapter, pipeline, _indexed_code, _indexed_docs, _profiles = (
        seed_baseline(config, cwd=tmp_path)
    )
    previous_state = pipeline.load_state()
    assert previous_state is not None

    (workspace_root / "components" / "health" / "main.c").write_text(
        """/* Health subsystem runtime entrypoints. */
#include "health.h"

int health_incremental_probe(void)
{
    return 42;
}
""",
        encoding="utf-8",
    )
    spy = SpyCodeIndexer(CodeIndexer.from_config(config, cwd=tmp_path))
    pipeline = IncrementalIndexPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
        code_indexer=spy,
    )

    result = pipeline.run(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")

    assert result.result_status == "ready"
    assert spy.include_paths_calls
    include_paths = spy.include_paths_calls[-1]
    assert include_paths is not None
    assert "components/health/main.c" in include_paths
    assert "components/health/module.mk" in include_paths
    assert "components/health/bt.c" not in include_paths
    assert len(include_paths) < len(previous_state.code_files)
    assert result.metadata["code_collect_workers"]["workers"] == 2


def test_incremental_pipeline_skips_applied_code_task_and_collects_only_unfinished_paths(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path, overrides={"indexing": {"workers": 2}})
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)
    metadata_adapter, vector_adapter, _pipeline, _indexed_code, _indexed_docs, _profiles = (
        seed_baseline(config, cwd=tmp_path)
    )
    (workspace_root / "components" / "health" / "main.c").write_text(
        """/* Health subsystem runtime entrypoints. */
#include "health.h"

#define HEALTH_SHOULD_NOT_APPLY 777

int health_skipped_probe(void)
{
    return HEALTH_SHOULD_NOT_APPLY;
}
""",
        encoding="utf-8",
    )
    (workspace_root / "components" / "health" / "bt.c").write_text(
        """#include "health.h"

#define BT_INCREMENTAL_READY 2

int health_bt_init(void)
{
    return BT_INCREMENTAL_READY;
}
""",
        encoding="utf-8",
    )
    spy = SpyCodeIndexer(CodeIndexer.from_config(config, cwd=tmp_path))
    pipeline = IncrementalIndexPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
        code_indexer=spy,
    )
    plan = pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")
    signature = make_index_plan_signature(
        plan,
        config=config,
        mode="incremental",
        target="local",
    )
    skipped_task = next(
        task
        for task in make_index_task_list(plan)
        if task.task_key == "code:apply:components/health/main.c"
    )
    store = SQLiteJobStore(Path(config.storage.jobs.path))
    job = store.create_job(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        metadata={"plan_signature": signature.digest},
    )
    record_task_applied_checkpoint(
        store,
        job.job_id,
        skipped_task,
        metadata={"plan_signature": signature.digest},
    )

    events: list[IndexProgressEvent] = []
    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="code",
        plan=plan,
        progress_callback=events.append,
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            resume_policy={"mode": "auto"},
            plan_signature=signature,
        ),
    )

    assert result.result_status == "ready"
    assert result.metadata["tasks"]["skipped"] == 1
    assert result.metadata["tasks"]["applied"] >= 1
    include_paths = spy.include_paths_calls[-1]
    assert include_paths == (
        "components/health/bt.c",
        "components/health/module.mk",
    )
    assert any(
        event.message == "Skipping previously applied task"
        and event.current_path == "components/health/main.c"
        for event in events
    )
    updated = store.get_job(job.job_id)
    assert updated is not None
    assert updated.metadata["tasks_skipped"] == 1

    reader = metadata_adapter.reader()
    scope = QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, source_scope="components")
    file_paths = {record.file_id: record.relative_path for record in reader.iter_files(scope)}
    names_by_path: dict[str, set[str]] = {}
    for item in reader.logical_entities(scope):
        path = file_paths.get(item.record.file_id)
        if path is not None:
            names_by_path.setdefault(path, set()).add(item.record.name)
    assert "HEALTH_SHOULD_NOT_APPLY" not in names_by_path["components/health/main.c"]
    assert "BT_INCREMENTAL_READY" in names_by_path["components/health/bt.c"]


def test_incremental_pipeline_resumes_after_checkpointed_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = resolve_model(tmp_path, overrides={"indexing": {"workers": 2}})
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)
    metadata_adapter, vector_adapter, _pipeline, _indexed_code, _indexed_docs, _profiles = (
        seed_baseline(config, cwd=tmp_path)
    )
    (workspace_root / "components" / "health" / "main.c").write_text(
        """#include "health.h"

#define HEALTH_INTERRUPT_MAIN 11

int health_interrupt_main(void)
{
    return HEALTH_INTERRUPT_MAIN;
}
""",
        encoding="utf-8",
    )
    (workspace_root / "components" / "health" / "bt.c").write_text(
        """#include "health.h"

#define HEALTH_INTERRUPT_BT 22

int health_interrupt_bt(void)
{
    return HEALTH_INTERRUPT_BT;
}
""",
        encoding="utf-8",
    )
    pipeline = IncrementalIndexPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    plan = pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")
    signature = make_index_plan_signature(
        plan,
        config=config,
        mode="incremental",
        target="local",
    )
    store = SQLiteJobStore(Path(config.storage.jobs.path))
    job = store.create_job(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        metadata={"plan_signature": signature.digest},
    )
    tasks = {task.task_key: task for task in make_index_task_list(plan)}
    interrupted_task_keys: list[str] = []
    original_record = record_task_applied_checkpoint

    def interrupt_after_second_code_checkpoint(
        store_arg: SQLiteJobStore,
        job_id: str,
        task,
        *,
        metadata: dict[str, object] | None = None,
    ) -> None:
        original_record(store_arg, job_id, task, metadata=metadata)
        if not task.task_key.startswith("code:apply:"):
            return
        interrupted_task_keys.append(task.task_key)
        if len(interrupted_task_keys) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "active_knowledge_server.indexing.pipeline.record_task_applied_checkpoint",
        interrupt_after_second_code_checkpoint,
    )

    with pytest.raises(KeyboardInterrupt):
        pipeline.run(
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source="code",
            plan=plan,
            run_context=IndexRunContext(
                job_store=store,
                job_id=job.job_id,
                plan_signature=signature,
                resume_policy={"mode": "auto"},
            ),
        )

    assert interrupted_task_keys == [
        "code:apply:components/health/bt.c",
        "code:apply:components/health/main.c",
    ]
    for task_key in interrupted_task_keys:
        assert store.get_checkpoint(job.job_id, task_checkpoint_key(tasks[task_key])) is not None

    resumed_pipeline = IncrementalIndexPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    events: list[IndexProgressEvent] = []
    result = resumed_pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="code",
        plan=plan,
        progress_callback=events.append,
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            plan_signature=signature,
            resume_policy={"mode": "auto"},
        ),
    )

    assert result.result_status == "ready"
    assert result.metadata["tasks"]["skipped"] == 2
    skipped_paths = {
        event.current_path
        for event in events
        if event.message == "Skipping previously applied task"
    }
    assert skipped_paths == {
        "components/health/bt.c",
        "components/health/main.c",
    }

    reader = metadata_adapter.reader()
    scope = QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, source_scope="components")
    file_paths = {record.file_id: record.relative_path for record in reader.iter_files(scope)}
    main_entities = [
        item.record.name
        for item in reader.logical_entities(scope)
        if file_paths.get(item.record.file_id) == "components/health/main.c"
    ]
    bt_entities = [
        item.record.name
        for item in reader.logical_entities(scope)
        if file_paths.get(item.record.file_id) == "components/health/bt.c"
    ]
    assert main_entities.count("HEALTH_INTERRUPT_MAIN") == 1
    assert bt_entities.count("HEALTH_INTERRUPT_BT") == 1


def test_incremental_pipeline_replays_code_apply_after_interrupt_before_checkpoint(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)
    metadata_adapter, vector_adapter, healthy_pipeline, _indexed_code, _indexed_docs, _profiles = (
        seed_baseline(config, cwd=tmp_path)
    )
    (workspace_root / "components" / "health" / "main.c").write_text(
        """#include "health.h"

#define HEALTH_REPLAY_PROBE 33

int health_replay_probe(void)
{
    return HEALTH_REPLAY_PROBE;
}
""",
        encoding="utf-8",
    )
    interrupted_pipeline = InterruptBeforeCodeCheckpointPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    plan = healthy_pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")
    signature = make_index_plan_signature(
        plan,
        config=config,
        mode="incremental",
        target="local",
    )
    replayed_task = next(
        task
        for task in make_index_task_list(plan)
        if task.task_key == "code:apply:components/health/main.c"
    )
    store = SQLiteJobStore(Path(config.storage.jobs.path))
    job = store.create_job(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        metadata={"plan_signature": signature.digest},
    )

    with pytest.raises(KeyboardInterrupt):
        interrupted_pipeline.run(
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source="code",
            plan=plan,
            run_context=IndexRunContext(
                job_store=store,
                job_id=job.job_id,
                plan_signature=signature,
                resume_policy={"mode": "auto"},
            ),
        )

    assert interrupted_pipeline.interrupted_paths == ["components/health/main.c"]
    assert store.get_checkpoint(job.job_id, task_checkpoint_key(replayed_task)) is None

    resumed_pipeline = IncrementalIndexPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    result = resumed_pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="code",
        plan=plan,
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            plan_signature=signature,
            resume_policy={"mode": "auto"},
        ),
    )

    assert result.result_status == "ready"
    reader = metadata_adapter.reader()
    scope = QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, source_scope="components")
    file_records = [
        record
        for record in reader.iter_files(scope)
        if record.relative_path == "components/health/main.c"
    ]
    replay_entities = [
        item.record.name
        for item in reader.logical_entities(scope)
        if item.record.name == "HEALTH_REPLAY_PROBE"
    ]
    assert len(file_records) == 1
    assert replay_entities.count("HEALTH_REPLAY_PROBE") == 1
    assert store.get_checkpoint(job.job_id, task_checkpoint_key(replayed_task)) is not None


def test_incremental_pipeline_checkpoints_successful_code_apply_and_delete(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)
    metadata_adapter, vector_adapter, pipeline, _indexed_code, _indexed_docs, _profiles = (
        seed_baseline(config, cwd=tmp_path)
    )
    (workspace_root / "components" / "health" / "main.c").write_text(
        """#include "health.h"

#define HEALTH_CHECKPOINTED 3

int health_checkpointed(void)
{
    return HEALTH_CHECKPOINTED;
}
""",
        encoding="utf-8",
    )
    (workspace_root / "components" / "health" / "bt.c").unlink()

    plan = pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")
    signature = make_index_plan_signature(
        plan,
        config=config,
        mode="incremental",
        target="local",
    )
    store = SQLiteJobStore(Path(config.storage.jobs.path))
    job = store.create_job(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        metadata={"plan_signature": signature.digest},
    )

    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="code",
        plan=plan,
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            plan_signature=signature,
            resume_policy={"mode": "auto"},
        ),
    )

    assert result.result_status == "ready"
    tasks = {task.task_key: task for task in make_index_task_list(plan)}
    apply_checkpoint = decode_task_checkpoint(
        store.get_checkpoint(
            job.job_id,
            task_checkpoint_key(tasks["code:apply:components/health/main.c"]),
        )
    )
    delete_checkpoint = decode_task_checkpoint(
        store.get_checkpoint(
            job.job_id,
            task_checkpoint_key(tasks["code:delete:components/health/bt.c"]),
        )
    )

    assert apply_checkpoint is not None
    assert apply_checkpoint.status == "applied"
    assert apply_checkpoint.metadata["job_id"] == job.job_id
    assert apply_checkpoint.metadata["plan_signature"] == signature.digest
    assert isinstance(apply_checkpoint.metadata["applied_at"], str)
    assert apply_checkpoint.metadata["relative_path"] == "components/health/main.c"
    assert apply_checkpoint.metadata["operation"] == "apply"
    assert apply_checkpoint.metadata["record_counts"]["files"] == 1
    assert apply_checkpoint.metadata["record_counts"]["chunks"] >= 1
    assert "compile_db.missing" in apply_checkpoint.metadata["warning_codes"]

    assert delete_checkpoint is not None
    assert delete_checkpoint.status == "applied"
    assert delete_checkpoint.metadata["job_id"] == job.job_id
    assert delete_checkpoint.metadata["operation"] == "delete"
    assert delete_checkpoint.metadata["relative_path"] == "components/health/bt.c"
    assert delete_checkpoint.metadata["record_counts"]["files"] == 1


def test_incremental_pipeline_checkpoints_successful_doc_apply_and_delete(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    write_workspace_fixture(workspace_root)
    write_doc_fixture(docs_root)
    (docs_root / "api" / "actuator.md").write_text(
        """---
title: Actuator API
authority_level: official
module: actuator
---
# Actuator API

## actuator_open
Open actuator runtime support.
""",
        encoding="utf-8",
    )
    _metadata_adapter, _vector_adapter, pipeline, _indexed_code, _indexed_docs, _profiles = (
        seed_baseline(config, cwd=tmp_path, include_docs=True)
    )
    (docs_root / "api" / "sensor.md").write_text(
        """---
title: Sensor Register API
authority_level: official
version: 1.3.0
module: sensor
code_symbols:
  - sensor_open
tags:
  - sensor
---
# Sensor Register API

## sensor_open
This update should receive a task checkpoint.
""",
        encoding="utf-8",
    )
    (docs_root / "api" / "actuator.md").unlink()

    plan = pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="docs")
    signature = make_index_plan_signature(
        plan,
        config=config,
        mode="incremental",
        target="local",
    )
    store = SQLiteJobStore(Path(config.storage.jobs.path))
    job = store.create_job(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        metadata={"plan_signature": signature.digest},
    )

    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="docs",
        plan=plan,
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            plan_signature=signature,
            resume_policy={"mode": "auto"},
        ),
    )

    assert result.result_status == "ready"
    tasks = {task.task_key: task for task in make_index_task_list(plan)}
    apply_checkpoint = decode_task_checkpoint(
        store.get_checkpoint(
            job.job_id,
            task_checkpoint_key(tasks["doc:apply:api/sensor.md"]),
        )
    )
    delete_checkpoint = decode_task_checkpoint(
        store.get_checkpoint(
            job.job_id,
            task_checkpoint_key(tasks["doc:delete:api/actuator.md"]),
        )
    )

    assert apply_checkpoint is not None
    assert apply_checkpoint.status == "applied"
    assert apply_checkpoint.metadata["job_id"] == job.job_id
    assert apply_checkpoint.metadata["plan_signature"] == signature.digest
    assert apply_checkpoint.metadata["operation"] == "apply"
    assert apply_checkpoint.metadata["relative_path"] == "api/sensor.md"
    assert apply_checkpoint.metadata["storage_relative_path"] == "knowledge-sources/api/sensor.md"
    assert apply_checkpoint.metadata["record_counts"]["files"] == 1
    assert apply_checkpoint.metadata["record_counts"]["chunks"] >= 1
    assert apply_checkpoint.metadata["warning_codes"] == []

    assert delete_checkpoint is not None
    assert delete_checkpoint.status == "applied"
    assert delete_checkpoint.metadata["job_id"] == job.job_id
    assert delete_checkpoint.metadata["operation"] == "delete"
    assert delete_checkpoint.metadata["relative_path"] == "api/actuator.md"
    assert (
        delete_checkpoint.metadata["storage_relative_path"] == "knowledge-sources/api/actuator.md"
    )


def test_incremental_pipeline_does_not_checkpoint_failed_code_apply(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)
    metadata_adapter, vector_adapter, healthy_pipeline, _indexed_code, _indexed_docs, _profiles = (
        seed_baseline(config, cwd=tmp_path)
    )
    (workspace_root / "components" / "health" / "main.c").write_text(
        """#include "health.h"

int health_apply_failure_probe(void)
{
    return 5;
}
""",
        encoding="utf-8",
    )
    pipeline = FailingCodeApplyPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    plan = healthy_pipeline.plan(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")
    signature = make_index_plan_signature(
        plan,
        config=config,
        mode="incremental",
        target="local",
    )
    failed_task = next(
        task
        for task in make_index_task_list(plan)
        if task.task_key == "code:apply:components/health/main.c"
    )
    store = SQLiteJobStore(Path(config.storage.jobs.path))
    job = store.create_job(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        metadata={"plan_signature": signature.digest},
    )

    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="code",
        plan=plan,
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            plan_signature=signature,
            resume_policy={"mode": "auto"},
        ),
    )

    assert result.result_status == "partial_ready"
    assert result.metadata["tasks"]["failed"] >= 1
    assert store.get_checkpoint(job.job_id, task_checkpoint_key(failed_task)) is None


def test_incremental_pipeline_updates_job_context_metadata(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_workspace_fixture(workspace_root)
    metadata_adapter, vector_adapter, pipeline, _indexed_code, _indexed_docs, _profiles = (
        seed_baseline(config, cwd=tmp_path)
    )
    (workspace_root / "components" / "health" / "main.c").write_text(
        """/* Health subsystem runtime entrypoints. */
#include "health.h"

int health_job_context_probe(void)
{
    return 99;
}
""",
        encoding="utf-8",
    )
    pipeline = IncrementalIndexPipeline(
        config,
        cwd=tmp_path,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
    )
    store = SQLiteJobStore(Path(config.storage.jobs.path))
    job = store.create_job(snapshot_id=CURRENT_SNAPSHOT_ID)

    result = pipeline.run(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source="code",
        run_context=IndexRunContext(
            job_store=store,
            job_id=job.job_id,
            resume_policy={"mode": "auto"},
        ),
    )

    updated = store.get_job(job.job_id)
    assert updated is not None
    assert updated.status == "reporting"
    assert updated.metadata["plan_signature"].startswith("sha256:")
    assert (
        updated.metadata["plan_signature_payload"]["digest"] == updated.metadata["plan_signature"]
    )
    assert updated.metadata["plan_summary"]["changed_code_paths_count"] == 1
    assert updated.metadata["plan_summary"]["source"] == "code"
    assert updated.metadata["tasks_total"] == result.metadata["tasks"]["total"]
    assert updated.metadata["tasks_by_phase"]["code_apply"] >= 1
    assert updated.metadata["tasks_applied"] >= 1
    assert updated.metadata["last_phase"] == "done"
    assert isinstance(updated.metadata["last_task_key"], str)
    assert updated.metadata["resume_policy"] == {"mode": "auto"}


def write_workspace_fixture(workspace_root: Path) -> None:
    component_dir = workspace_root / "components" / "health"
    component_dir.mkdir(parents=True)
    (component_dir / "module.mk").write_text(
        """NAME = health_core
MODULE = health.logic
HEALTH_SOURCES = main.c health.h
ifdef CONFIG_HEALTH_BT
HEALTH_SOURCES += bt.c
endif
""",
        encoding="utf-8",
    )
    (component_dir / "main.c").write_text(
        """/* Health subsystem runtime entrypoints. */
#include "health.h"

#define HEALTH_DEFAULT 1

typedef struct HealthState {
    int ready;
} HealthState;

int health_init(void)
{
    return HEALTH_DEFAULT;
}
""",
        encoding="utf-8",
    )
    (component_dir / "health.h").write_text(
        """#ifndef HEALTH_H
#define HEALTH_H

int health_init(void);
int health_bt_init(void);

#endif
""",
        encoding="utf-8",
    )
    (component_dir / "bt.c").write_text(
        """#include "health.h"

#define BT_READY 1

int health_bt_init(void)
{
    return BT_READY;
}
""",
        encoding="utf-8",
    )


def write_doc_fixture(docs_root: Path) -> None:
    (docs_root / "api").mkdir(parents=True, exist_ok=True)
    (docs_root / "api" / "sensor.md").write_text(
        """---
title: Sensor Register API
authority_level: official
version: 1.2.0
module: sensor
code_symbols:
  - sensor_open
tags:
  - sensor
  - register
---
# Sensor Register API

## sensor_open
Open the sensor register and return a handle for runtime use.
""",
        encoding="utf-8",
    )


def write_profile_fixture(
    workspace_root: Path,
    *,
    defconfig_rel: str,
    dotconfig_rel: str,
    app: str,
    board: str,
    extra_config: str,
) -> None:
    defconfig_path = workspace_root / defconfig_rel
    dotconfig_path = workspace_root / dotconfig_rel
    defconfig_path.parent.mkdir(parents=True, exist_ok=True)
    dotconfig_path.parent.mkdir(parents=True, exist_ok=True)
    common = f'CONFIG_APP="{app}"\nCONFIG_BOARD="{board}"\n'
    defconfig_path.write_text(common + extra_config, encoding="utf-8")
    dotconfig_path.write_text(
        common + "CONFIG_RUNTIME_READY=y\n" + extra_config,
        encoding="utf-8",
    )


def deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = merged[key]
            assert isinstance(nested, dict)
            merged[key] = deep_merge(nested, value)
            continue
        merged[key] = value
    return merged
