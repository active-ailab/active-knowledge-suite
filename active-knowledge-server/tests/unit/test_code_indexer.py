from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID, CodeIndexer
from active_knowledge_server.storage import FTSQuery, QueryScope, StorageWriteRequest
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


def test_code_indexer_indexes_workspace_structure_and_relations(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    component_dir = workspace_root / "components" / "health"
    component_dir.mkdir(parents=True)
    (component_dir / "module.mk").write_text(
        """NAME = health
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

#endif
""",
        encoding="utf-8",
    )
    (component_dir / "bt.c").write_text(
        """#include "health.h"

int health_bt_init(void)
{
    return 0;
}
""",
        encoding="utf-8",
    )

    adapter = build_adapter(config)
    indexer = CodeIndexer.from_config(config, cwd=tmp_path)

    indexed = indexer.collect_and_store(
        adapter.writer(StorageWriteRequest(target="overlay")),
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )

    assert indexed.schema_version == "code_indexer.v1"
    assert {record.source_id for record in indexed.source_records} == {"workspace"}
    assert {record.entity_type for record in indexed.entity_records} >= {
        "Directory",
        "Module",
        "File",
        "Function",
        "Macro",
        "Type",
    }
    assert any(record.chunk_type == "code.file_header" for record in indexed.chunk_records)
    assert any(record.chunk_type == "code.function" for record in indexed.chunk_records)
    assert any(record.chunk_type == "code.macro" for record in indexed.chunk_records)
    assert any(record.chunk_type == "code.type" for record in indexed.chunk_records)
    assert any(warning.code == "compile_db.missing" for warning in indexed.warnings)

    relation_types = {record.relation_type for record in indexed.relation_records}
    assert relation_types >= {"contains", "defines", "belongs_to_module", "guarded_by_macro"}
    assert all("extractor" in record.metadata for record in indexed.relation_records)
    assert all("confidence" in record.metadata for record in indexed.relation_records)

    entity_matches = adapter.reader().search_fts(
        FTSQuery(
            index_name="entity_fts",
            query="health_init",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
        )
    )
    assert entity_matches
    assert any(match.title == "health_init" for match in entity_matches)

    macro_matches = adapter.reader().search_fts(
        FTSQuery(
            index_name="entity_fts",
            query="HEALTH_DEFAULT",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
        )
    )
    assert macro_matches
    assert any(match.title == "HEALTH_DEFAULT" for match in macro_matches)

    file_matches = adapter.reader().search_fts(
        FTSQuery(
            index_name="entity_fts",
            query="main",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
        )
    )
    assert file_matches
    assert any(match.title == "main.c" for match in file_matches)

    directory_matches = adapter.reader().search_fts(
        FTSQuery(
            index_name="entity_fts",
            query="components health",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
        )
    )
    assert directory_matches
    assert any(match.title == "health" for match in directory_matches)

    code_matches = adapter.reader().search_fts(
        FTSQuery(
            index_name="code_fts",
            query="Health subsystem runtime entrypoints",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
        )
    )
    assert code_matches
    assert code_matches[0].index_name == "code_fts"

    module_entity_id = next(
        record.entity_id for record in indexed.entity_records if record.entity_type == "Module"
    )
    main_file_entity_id = next(
        record.entity_id
        for record in indexed.entity_records
        if record.entity_type == "File" and record.name == "main.c"
    )
    guarded_macro_id = next(
        record.entity_id
        for record in indexed.entity_records
        if record.entity_type == "Macro" and record.name == "CONFIG_HEALTH_BT"
    )

    assert any(
        record.relation_type == "belongs_to_module"
        and record.src_entity_id == main_file_entity_id
        and record.dst_entity_id == module_entity_id
        for record in indexed.relation_records
    )
    assert any(
        record.relation_type == "guarded_by_macro" and record.dst_entity_id == guarded_macro_id
        for record in indexed.relation_records
    )


def test_code_indexer_parallel_collect_matches_serial_output(tmp_path: Path) -> None:
    config = resolve_model(tmp_path, overrides={"indexing": {"workers": 1}})
    parallel_config = config.model_copy(
        update={"indexing": config.indexing.model_copy(update={"workers": 4})}
    )
    workspace_root = Path(config.project.workspace_root)
    component_dir = workspace_root / "components" / "health"
    component_dir.mkdir(parents=True)
    (component_dir / "module.mk").write_text(
        """NAME = health
MODULE = health.logic
HEALTH_SOURCES = main.c health.h
""",
        encoding="utf-8",
    )
    (component_dir / "main.c").write_text(
        """/* Health subsystem runtime entrypoints. */
#include "health.h"

#define HEALTH_DEFAULT 1

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

#endif
""",
        encoding="utf-8",
    )

    serial = CodeIndexer.from_config(config, cwd=tmp_path).collect(snapshot_id=CURRENT_SNAPSHOT_ID)
    parallel = CodeIndexer.from_config(parallel_config, cwd=tmp_path).collect(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        workspace_inventory=serial.workspace_inventory,
    )

    assert _record_signature(serial.file_records) == _record_signature(parallel.file_records)
    assert _record_signature(serial.chunk_records) == _record_signature(parallel.chunk_records)
    assert _record_signature(serial.entity_records) == _record_signature(parallel.entity_records)
    assert _record_signature(serial.relation_records) == _record_signature(
        parallel.relation_records
    )
    assert _record_signature(serial.evidence_records) == _record_signature(
        parallel.evidence_records
    )
    assert serial.metadata["collect_workers"]["workers"] == 1
    assert parallel.metadata["collect_workers"]["workers"] == 3


def test_code_indexer_process_collect_matches_thread_output(tmp_path: Path) -> None:
    config = resolve_model(
        tmp_path,
        overrides={"indexing": {"workers": 1, "parallel": {"mode": "thread"}}},
    )
    process_config = config.model_copy(
        update={
            "indexing": config.indexing.model_copy(
                update={
                    "workers": 4,
                    "parallel": config.indexing.parallel.model_copy(update={"mode": "process"}),
                }
            )
        }
    )
    workspace_root = Path(config.project.workspace_root)
    component_dir = workspace_root / "components" / "health"
    component_dir.mkdir(parents=True)
    (component_dir / "module.mk").write_text(
        """NAME = health
MODULE = health.logic
HEALTH_SOURCES = main.c health.h
""",
        encoding="utf-8",
    )
    (component_dir / "main.c").write_text(
        """/* Health subsystem runtime entrypoints. */
#include "health.h"

#define HEALTH_DEFAULT 1

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

#endif
""",
        encoding="utf-8",
    )

    serial = CodeIndexer.from_config(config, cwd=tmp_path).collect(snapshot_id=CURRENT_SNAPSHOT_ID)
    process = CodeIndexer.from_config(process_config, cwd=tmp_path).collect(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        workspace_inventory=serial.workspace_inventory,
    )

    assert _record_signature(serial.file_records) == _record_signature(process.file_records)
    assert _record_signature(serial.chunk_records) == _record_signature(process.chunk_records)
    assert _record_signature(serial.entity_records) == _record_signature(process.entity_records)
    assert _record_signature(serial.relation_records) == _record_signature(process.relation_records)
    assert _record_signature(serial.evidence_records) == _record_signature(process.evidence_records)
    assert process.metadata["collect_workers"]["executor_kind"] == "process"
    assert process.metadata["timings"]["parser_seconds"] >= 0.0
    assert process.metadata["diagnostics"]["slowest_items"]


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


def _record_signature(records: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(json.dumps(asdict(record), ensure_ascii=True, sort_keys=True) for record in records)
    )
