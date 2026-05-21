from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing import (
    CURRENT_SNAPSHOT_ID,
    CodeIndexer,
    ProfileCollector,
    ProfileConditionedRelationExtractor,
    plan_profile_conditioned_relation_rebuild,
    summarize_entity_profile_states_from_reader,
)
from active_knowledge_server.storage import StorageWriteRequest
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


def test_profile_conditioned_relations_project_enabled_disabled_and_unknown_states(
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
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_sensorhub_defconfig",
        dotconfig_rel="build/out_hub/.config",
        app="sensorhub",
        board="mhs003",
        extra_config="# CONFIG_HEALTH_BT is not set\n",
    )
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_lite_defconfig",
        dotconfig_rel="build/out_lite/.config",
        app="lite",
        board="mhs003",
        extra_config="",
    )

    adapter = build_adapter(config)
    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    code_indexer = CodeIndexer.from_config(config, cwd=tmp_path)
    indexed_code = code_indexer.collect_and_store(writer, snapshot_id=CURRENT_SNAPSHOT_ID)
    collector = ProfileCollector.from_config(config, cwd=tmp_path)
    collected_profiles = collector.collect_and_store(writer, snapshot_id=CURRENT_SNAPSHOT_ID)
    extractor = ProfileConditionedRelationExtractor()

    indexed_relations = extractor.collect_and_store(
        writer,
        snapshot_id=CURRENT_SNAPSHOT_ID,
        profiles=collected_profiles.profile_records,
        entities=indexed_code.entity_records,
        relations=indexed_code.relation_records,
    )

    module_entity_id = next(
        record.entity_id
        for record in indexed_code.entity_records
        if record.entity_type == "Module" and record.name == "health_core"
    )
    bt_file_entity_id = next(
        record.entity_id
        for record in indexed_code.entity_records
        if record.entity_type == "File" and record.name == "bt.c"
    )
    bt_symbol_entity_id = next(
        record.entity_id
        for record in indexed_code.entity_records
        if record.entity_type == "Macro" and record.name == "BT_READY"
    )

    relation_types = {
        (record.profile_id, record.src_entity_id): record.relation_type
        for record in indexed_relations.relation_records
        if record.dst_entity_id.endswith("CONFIG_HEALTH_BT")
        or record.metadata.get("macro_name") == "CONFIG_HEALTH_BT"
    }
    assert relation_types[("mhs003_watch", module_entity_id)] == "enabled_by"
    assert relation_types[("mhs003_sensorhub", module_entity_id)] == "disabled_by"
    assert relation_types[("mhs003_lite", module_entity_id)] == "unknown_by"
    assert relation_types[("mhs003_watch", bt_file_entity_id)] == "enabled_by"
    assert relation_types[("mhs003_sensorhub", bt_file_entity_id)] == "disabled_by"
    assert relation_types[("mhs003_lite", bt_file_entity_id)] == "unknown_by"
    assert relation_types[("mhs003_watch", bt_symbol_entity_id)] == "enabled_by"
    assert relation_types[("mhs003_sensorhub", bt_symbol_entity_id)] == "disabled_by"
    assert relation_types[("mhs003_lite", bt_symbol_entity_id)] == "unknown_by"

    for record in indexed_relations.relation_records:
        assert record.profile_id != "all"
        assert record.metadata["extractor"] == "profile_conditioned_relation_extractor"
        assert "condition_expr" in record.metadata
        assert "confidence" in record.metadata
        assert "profile_record_id" in record.metadata

    states = summarize_entity_profile_states_from_reader(
        adapter.reader(),
        entity_id=bt_symbol_entity_id,
        profiles=collected_profiles.profile_records,
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )
    states_by_profile = {state.profile_id: state for state in states}
    assert states_by_profile["mhs003_watch"].status == "enabled"
    assert states_by_profile["mhs003_sensorhub"].status == "disabled"
    assert states_by_profile["mhs003_lite"].status == "unknown"
    assert states_by_profile["mhs003_lite"].unknown_macros == ("CONFIG_HEALTH_BT",)
    assert "CONFIG_HEALTH_BT" in states_by_profile["mhs003_watch"].condition_macros


def test_profile_conditioned_relation_rebuild_plan_only_marks_changed_profiles(
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
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_sensorhub_defconfig",
        dotconfig_rel="build/out_hub/.config",
        app="sensorhub",
        board="mhs003",
        extra_config="# CONFIG_HEALTH_BT is not set\n",
    )

    collector = ProfileCollector.from_config(config, cwd=tmp_path)
    previous = collector.collect(snapshot_id=CURRENT_SNAPSHOT_ID).profile_records
    watch_record = next(record for record in previous if record.profile_id == "mhs003_watch")
    updated_watch = replace(
        watch_record,
        profile_record_id="profile:changed",
        metadata={
            **watch_record.metadata,
            "config_hash": "sha256:changed",
        },
    )
    current = tuple(
        updated_watch if record.profile_id == "mhs003_watch" else record
        for record in previous
    )

    plan = plan_profile_conditioned_relation_rebuild(previous, current)

    assert plan.recompute_required is True
    assert plan.changed_profile_ids == ("mhs003_watch",)
    assert plan.added_profile_ids == ()
    assert plan.removed_profile_ids == ()
    assert "mhs003_sensorhub" in plan.unchanged_profile_ids


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
        """#include "health.h"

int health_init(void)
{
    return 0;
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
    dotconfig_path.write_text(common + "CONFIG_RUNTIME_READY=y\n" + extra_config, encoding="utf-8")


def deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = merged[key]
            assert isinstance(nested, dict)
            merged[key] = deep_merge(nested, value)
        else:
            merged[key] = value
    return merged
