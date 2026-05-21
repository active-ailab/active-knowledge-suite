from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
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
    summarize_entity_profile_states_from_reader,
)
from active_knowledge_server.storage import QueryScope, StorageWriteRequest
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
)


class BrokenDocumentIndexer:
    def collect(self, *, snapshot_id: str, source_docs_manifest: object) -> object:
        raise RuntimeError(f"synthetic doc failure for {snapshot_id}")


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
            row[0]
            for row in connection.execute("SELECT relative_path FROM file").fetchall()
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

    result = pipeline.run(snapshot_id=CURRENT_SNAPSHOT_ID, source="code")

    assert result.result_status == "ready"
    assert result.plan.rebuild_profile_conditioned_relations is True
    assert result.plan.changed_profile_ids == ("mhs003_watch",)

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

    result = pipeline.run(snapshot_id=CURRENT_SNAPSHOT_ID, source="docs")

    assert result.result_status == "ready"
    assert result.plan.changed_doc_paths == ()
    assert result.plan.rebuild_vectors is True

    stored_vectors = read_vector_collection(delta_vectors)
    assert stored_vectors
    assert {
        row["embedding_model_version"] for row in stored_vectors
    } == {"text-embedding-local-v2"}


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
    assert any(
        warning.code == "index.doc_incremental_failed" for warning in result.warnings
    )
    assert failing_pipeline.load_state() == previous_state


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
