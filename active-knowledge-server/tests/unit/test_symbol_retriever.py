from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID, CodeIndexer, DocumentIndexer
from active_knowledge_server.query import SymbolResolver, SymbolRetriever, SymbolSearchRequest
from active_knowledge_server.storage import (
    ALL_SCOPE,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    QueryScope,
    StorageWriteRequest,
)
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


def test_symbol_retriever_resolves_exact_entity_types(tmp_path: Path) -> None:
    _config, adapter = build_indexed_fixture(tmp_path)
    retriever = SymbolRetriever.from_storage(adapter)

    cases = [
        ("health_init", "function", "Function", "health_init"),
        ("HEALTH_DEFAULT", "macro", "Macro", "HEALTH_DEFAULT"),
        ("HealthState", "type", "Type", "HealthState"),
        ("main.c", "file", "File", "main.c"),
        ("health.logic", "module", "Module", "health"),
    ]

    for query, entity_type, expected_type, expected_name in cases:
        result = retriever.search(
            SymbolSearchRequest(
                query=query,
                entity_type=entity_type,
                scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
            )
        )
        assert result.candidates
        candidate = result.candidates[0]
        assert candidate.entity_type == expected_type
        assert candidate.name == expected_name
        assert "exact" in candidate.match_kinds or "alias" in candidate.match_kinds


def test_symbol_retriever_marks_fuzzy_alias_and_doc_mentions(tmp_path: Path) -> None:
    _config, adapter = build_indexed_fixture(tmp_path)
    retriever = SymbolRetriever.from_storage(adapter)

    fuzzy = retriever.search(
        SymbolSearchRequest(
            query="health init",
            entity_type="function",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
        )
    )
    assert fuzzy.candidates
    assert fuzzy.candidates[0].name == "health_init"
    assert "fuzzy" in fuzzy.candidates[0].match_kinds

    alias = retriever.search(
        SymbolSearchRequest(
            query="health logic",
            entity_type="module",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
        )
    )
    assert alias.candidates
    assert alias.candidates[0].name == "health"
    assert "alias" in alias.candidates[0].match_kinds

    doc_mention = retriever.search(
        SymbolSearchRequest(
            query="boot entrypoint",
            entity_type="function",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
        )
    )
    assert doc_mention.candidates
    assert doc_mention.candidates[0].name == "health_init"
    assert "doc_mention" in doc_mention.candidates[0].match_kinds
    assert doc_mention.candidates[0].doc_mention_paths == (
        "knowledge-sources/engineering/health_boot.md",
    )


def test_symbol_resolver_returns_multi_result_for_ambiguous_symbols(tmp_path: Path) -> None:
    _config, adapter = build_indexed_fixture(tmp_path, duplicate_function=True)
    resolver = SymbolResolver.from_storage(adapter)

    resolution = resolver.resolve(
        "health_init",
        entity_type="function",
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )

    assert resolution.status == "multi_result"
    assert resolution.selected is None
    assert len(resolution.candidates) == 2
    assert len({item.disambiguation_key for item in resolution.candidates}) == 2


def test_symbol_retriever_applies_profile_scope(tmp_path: Path) -> None:
    config, adapter = build_indexed_fixture(tmp_path)
    seed_profile_specific_entities(adapter)
    retriever = SymbolRetriever.from_storage(adapter)

    watch = retriever.search(
        SymbolSearchRequest(
            query="profile_only",
            entity_type="function",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
        )
    )
    assert [item.profile_id for item in watch.candidates] == ["watch"]

    sensorhub = retriever.search(
        SymbolSearchRequest(
            query="profile_only",
            entity_type="function",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="sensorhub"),
        )
    )
    assert [item.profile_id for item in sensorhub.candidates] == ["sensorhub"]

    all_profiles = retriever.search(
        SymbolSearchRequest(
            query="profile_only",
            entity_type="function",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id=ALL_SCOPE),
        )
    )
    assert [item.profile_id for item in all_profiles.candidates] == ["sensorhub", "watch"]

    assert Path(config.project.workspace_root).exists()


def build_indexed_fixture(
    tmp_path: Path,
    *,
    duplicate_function: bool = False,
) -> tuple[ActiveKnowledgeConfig, SQLiteStorageAdapter]:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    docs_root = Path(config.runtime.source_docs_root)
    seed_workspace(workspace_root, duplicate_function=duplicate_function)
    seed_docs(docs_root)

    adapter = build_adapter(config)
    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    CodeIndexer.from_config(config, cwd=tmp_path).collect_and_store(
        writer,
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )
    DocumentIndexer.from_config(config, cwd=tmp_path).collect_and_store(
        writer,
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )
    return config, adapter


def seed_workspace(workspace_root: Path, *, duplicate_function: bool) -> None:
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
    if duplicate_function:
        fitness_dir = workspace_root / "components" / "fitness"
        fitness_dir.mkdir(parents=True)
        (fitness_dir / "fitness.c").write_text(
            """int health_init(void)
{
    return 0;
}
""",
            encoding="utf-8",
        )


def seed_docs(docs_root: Path) -> None:
    engineering_dir = docs_root / "engineering"
    engineering_dir.mkdir(parents=True)
    (engineering_dir / "health_boot.md").write_text(
        """---
title: Health Boot Notes
authority_level: official
code_symbols:
  - health_init
tags:
  - boot
  - entrypoint
---
# Boot Entry Point

The boot entrypoint initializes the health runtime before tasks start.
""",
        encoding="utf-8",
    )


def seed_profile_specific_entities(adapter: SQLiteStorageAdapter) -> None:
    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    watch_file = FileRecord(
        file_id="file-profile-watch",
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source_id="workspace",
        relative_path="profiles/watch_only.c",
        content_hash="hash:watch-profile",
        source_scope="components",
        profile_id="watch",
        language="c",
    )
    sensorhub_file = FileRecord(
        file_id="file-profile-sensorhub",
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source_id="workspace",
        relative_path="profiles/sensorhub_only.c",
        content_hash="hash:sensorhub-profile",
        source_scope="components",
        profile_id="sensorhub",
        language="c",
    )
    watch_entity = EntityRecord(
        entity_id="entity:profile_only:watch",
        snapshot_id=CURRENT_SNAPSHOT_ID,
        file_id=watch_file.file_id,
        entity_type="Function",
        name="profile_only",
        qualified_name="profiles/watch_only.c::profile_only",
        path="profiles/watch_only.c#profile_only",
        source_scope="components",
        profile_id="watch",
        start_line=1,
        end_line=3,
        metadata={"summary": "watch-only symbol"},
    )
    sensorhub_entity = EntityRecord(
        entity_id="entity:profile_only:sensorhub",
        snapshot_id=CURRENT_SNAPSHOT_ID,
        file_id=sensorhub_file.file_id,
        entity_type="Function",
        name="profile_only",
        qualified_name="profiles/sensorhub_only.c::profile_only",
        path="profiles/sensorhub_only.c#profile_only",
        source_scope="components",
        profile_id="sensorhub",
        start_line=1,
        end_line=3,
        metadata={"summary": "sensorhub-only symbol"},
    )
    watch_evidence = EvidenceRecord(
        evidence_id="evidence:profile_only:watch",
        snapshot_id=CURRENT_SNAPSHOT_ID,
        object_type="entity",
        object_id=watch_entity.entity_id,
        file_id=watch_file.file_id,
        source_scope="components",
        profile_id="watch",
        excerpt="watch-only symbol",
        citation_label="profiles/watch_only.c:1",
        start_line=1,
        end_line=3,
        metadata={"path": watch_file.relative_path},
    )
    sensorhub_evidence = EvidenceRecord(
        evidence_id="evidence:profile_only:sensorhub",
        snapshot_id=CURRENT_SNAPSHOT_ID,
        object_type="entity",
        object_id=sensorhub_entity.entity_id,
        file_id=sensorhub_file.file_id,
        source_scope="components",
        profile_id="sensorhub",
        excerpt="sensorhub-only symbol",
        citation_label="profiles/sensorhub_only.c:1",
        start_line=1,
        end_line=3,
        metadata={"path": sensorhub_file.relative_path},
    )
    for record in (
        watch_file,
        sensorhub_file,
        watch_entity,
        sensorhub_entity,
        watch_evidence,
        sensorhub_evidence,
    ):
        if isinstance(record, FileRecord):
            writer.upsert_file(record)
        elif isinstance(record, EntityRecord):
            writer.upsert_entity(record)
        else:
            writer.upsert_evidence(record)
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