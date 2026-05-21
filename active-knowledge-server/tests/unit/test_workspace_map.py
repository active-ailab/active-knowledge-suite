from __future__ import annotations

import json
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing import (
    CURRENT_SNAPSHOT_ID,
    CodeIndexer,
    ProfileCollector,
    ProfileConditionedRelationExtractor,
    WorkspaceMapBuilder,
)
from active_knowledge_server.storage import StorageWriteRequest
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
)


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
                "dotconfig_candidates": ["build/.config"],
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


def test_workspace_map_builder_projects_active_paths_and_profiles(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_active_workspace_fixture(workspace_root)
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
        extra_config="CONFIG_NOTIFICATION_BT=y\n",
    )

    adapter = build_adapter(config)
    writer = adapter.writer(StorageWriteRequest(target="overlay"))

    indexed_code = CodeIndexer.from_config(config, cwd=tmp_path).collect_and_store(
        writer,
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )
    collected_profiles = ProfileCollector.from_config(config, cwd=tmp_path).collect_and_store(
        writer,
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )
    ProfileConditionedRelationExtractor().collect_and_store(
        writer,
        snapshot_id=CURRENT_SNAPSHOT_ID,
        profiles=collected_profiles.profile_records,
        entities=indexed_code.entity_records,
        relations=indexed_code.relation_records,
    )
    writer.flush()

    result = WorkspaceMapBuilder.from_config(config, cwd=tmp_path).collect_and_write(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        workspace_inventory=indexed_code.workspace_inventory,
        reader=adapter.reader(),
        profiles=collected_profiles.profile_records,
        profile_resolution=collected_profiles.resolution.to_dict(),
    )

    assert result.artifact.summary["view_names"] == [
        "domain",
        "feature",
        "layer",
        "profile",
        "workspace",
    ]
    artifact_path = result.artifact_paths[0]
    assert artifact_path.name == "current.json"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))

    root_children = {
        child["path"] for child in payload["workspace_tree"]["children"]
    }
    assert {"packages", "ui", "uiframework", "framework", "build", "configs"} <= root_children

    layer_items = {item["name"]: item for item in payload["views"]["layer"]["items"]}
    assert {"service", "app", "ui", "uiframework", "engine"} <= set(layer_items)

    domain_items = {item["item_id"]: item for item in payload["views"]["domain"]["items"]}
    notification_domain = domain_items["domain:notification"]
    assert "packages/services/notification" in notification_domain["metadata"]["service_paths"]
    assert "framework/engine/notificationEngine" in notification_domain["metadata"]["engine_paths"]
    assert "packages/apps/notification" in notification_domain["related_items"]

    feature_items = {item["item_id"]: item for item in payload["views"]["feature"]["items"]}
    notification_feature = feature_items["feature:notification"]
    assert "packages/apps/notification" in notification_feature["metadata"]["app_paths"]
    assert "ui/Notification" in notification_feature["metadata"]["ui_paths"]
    assert "packages/services/notification" in notification_feature["related_items"]

    profile_items = {item["name"]: item for item in payload["views"]["profile"]["items"]}
    watch_profile = profile_items["mhs003_watch"]
    assert watch_profile["metadata"]["counts"]["enabled"] > 0
    assert "notification.service" in watch_profile["module_names"]
    assert payload["metadata"]["profile_resolution"]["resolved_profile_id"] == "mhs003_watch"


def write_active_workspace_fixture(workspace_root: Path) -> None:
    write_module_fixture(
        workspace_root / "packages" / "services" / "notification",
        module_name="notification.service",
        source_var="NOTIFY_SOURCES",
        files={
            "service.c": """#include "service.h"

int notification_service_start(void)
{
    return 0;
}
""",
            "service.h": """#ifndef NOTIFICATION_SERVICE_H
#define NOTIFICATION_SERVICE_H

int notification_service_start(void);

#endif
""",
            "bt_service.c": """#define NOTIFY_BT_READY 1

int notification_bt_bridge(void)
{
    return NOTIFY_BT_READY;
}
""",
        },
        conditional_file="bt_service.c",
        conditional_macro="CONFIG_NOTIFICATION_BT",
    )
    write_module_fixture(
        workspace_root / "packages" / "apps" / "notification",
        module_name="notification.app",
        source_var="APP_SOURCES",
        files={
            "app.c": """int notification_app_entry(void)
{
    return 0;
}
""",
        },
    )
    write_module_fixture(
        workspace_root / "ui" / "Notification",
        module_name="notification.ui",
        source_var="UI_SOURCES",
        files={
            "screen.c": """int notification_screen_render(void)
{
    return 0;
}
""",
        },
    )
    write_module_fixture(
        workspace_root / "framework" / "engine" / "notificationEngine",
        module_name="notification.engine",
        source_var="ENGINE_SOURCES",
        files={
            "engine.c": """int notification_engine_boot(void)
{
    return 0;
}
""",
        },
    )
    widget_dir = workspace_root / "uiframework" / "widgets"
    widget_dir.mkdir(parents=True, exist_ok=True)
    (widget_dir / "button.c").write_text(
        """int widget_button_draw(void)
{
    return 0;
}
""",
        encoding="utf-8",
    )


def write_module_fixture(
    module_dir: Path,
    *,
    module_name: str,
    source_var: str,
    files: dict[str, str],
    conditional_file: str | None = None,
    conditional_macro: str | None = None,
) -> None:
    module_dir.mkdir(parents=True, exist_ok=True)
    mk_lines = [
        f"NAME = {module_name.replace('.', '_')}",
        f"MODULE = {module_name}",
    ]
    stable_files = sorted(name for name in files if name != conditional_file)
    if stable_files:
        mk_lines.append(f"{source_var} = {' '.join(stable_files)}")
    if conditional_file and conditional_macro:
        mk_lines.append(f"ifdef {conditional_macro}")
        mk_lines.append(f"{source_var} += {conditional_file}")
        mk_lines.append("endif")
    (module_dir / "module.mk").write_text("\n".join(mk_lines) + "\n", encoding="utf-8")
    for name, content in files.items():
        (module_dir / name).write_text(content, encoding="utf-8")


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
