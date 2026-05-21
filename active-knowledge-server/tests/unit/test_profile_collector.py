from __future__ import annotations

import os
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing.profile import ProfileCollector
from active_knowledge_server.storage import StorageWriteRequest
from active_knowledge_server.storage.sqlite_store import SQLiteStorageAdapter, migrate_sqlite_store


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


def test_profile_collector_resolves_single_dotconfig_candidate(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    write_profile_fixture(
        Path(config.project.workspace_root),
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
    )

    collector = ProfileCollector.from_config(config, cwd=tmp_path)
    collected = collector.collect(snapshot_id="snapshot:demo")

    assert len(collected.profile_records) == 1
    assert collected.profile_records[0].profile_id == "mhs003_watch"
    assert collected.profile_records[0].metadata["source"] == "dotconfig_scan"
    assert collected.profile_records[0].metadata["macro_assignments"]["CONFIG_RUNTIME_READY"] == {
        "value": "y",
        "value_type": "bool",
        "enabled": True,
        "source_kind": "dotconfig",
    }
    assert "CONFIG_RUNTIME_READY" in collected.profile_records[0].metadata["macro_summary"]["enabled_macros"]
    assert collected.resolution.status == "resolved"
    assert collected.resolution.resolved_profile_id == "mhs003_watch"
    assert collected.resolution.source == "dotconfig_scan"


def test_profile_collector_returns_multiple_candidates_for_two_trusted_dotconfigs(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
    )
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_sensorhub_defconfig",
        dotconfig_rel="build/out_hub/.config",
        app="sensorhub",
        board="mhs003",
    )
    os.utime(workspace_root / "build" / ".config", (100.0, 100.0))
    os.utime(workspace_root / "build" / "out_hub" / ".config", (90.0, 90.0))

    collector = ProfileCollector.from_config(config, cwd=tmp_path)
    collected = collector.collect(snapshot_id="snapshot:demo")

    assert collected.resolution.status == "multiple_candidates"
    assert [candidate.profile_id for candidate in collected.resolution.candidates] == [
        "mhs003_watch",
        "mhs003_sensorhub",
    ]
    assert collected.resolution.warnings[0].code == "profile.multiple_candidates"


def test_profile_collector_configured_default_profile_overrides_auto_and_persists(tmp_path: Path) -> None:
    config = resolve_model(
        tmp_path,
        overrides={"project": {"default_profile": "mhs003_sensorhub"}},
    )
    workspace_root = Path(config.project.workspace_root)
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
    )
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_sensorhub_defconfig",
        dotconfig_rel="build/out_hub/.config",
        app="sensorhub",
        board="mhs003",
    )

    adapter = build_adapter(config)
    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    collector = ProfileCollector.from_config(config, cwd=tmp_path)
    collected = collector.collect_and_store(writer, snapshot_id="snapshot:demo")
    reader = adapter.reader()
    stored = reader.iter_profiles(snapshot_id="snapshot:demo")

    assert collected.resolution.status == "resolved"
    assert collected.resolution.resolved_profile_id == "mhs003_sensorhub"
    assert collected.resolution.source == "local_config"
    assert {record.profile_id for record in stored} == {"mhs003_watch", "mhs003_sensorhub"}
    assert all(record.metadata["profile_manifest_hash"] == collected.manifest_hash for record in stored)


def write_profile_fixture(
    workspace_root: Path,
    *,
    defconfig_rel: str,
    dotconfig_rel: str,
    app: str,
    board: str,
) -> None:
    defconfig_path = workspace_root / defconfig_rel
    dotconfig_path = workspace_root / dotconfig_rel
    defconfig_path.parent.mkdir(parents=True, exist_ok=True)
    dotconfig_path.parent.mkdir(parents=True, exist_ok=True)
    defconfig_path.write_text(
        f'CONFIG_APP="{app}"\nCONFIG_BOARD="{board}"\nCONFIG_FEATURE_{app.upper()}=y\n',
        encoding="utf-8",
    )
    dotconfig_path.write_text(
        f'CONFIG_APP="{app}"\nCONFIG_BOARD="{board}"\nCONFIG_RUNTIME_READY=y\n',
        encoding="utf-8",
    )


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
