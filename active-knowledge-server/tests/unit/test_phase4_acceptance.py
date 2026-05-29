from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import yaml

from active_knowledge_server.cli import main
from active_knowledge_server.config.loader import ConfigDict, ResolvedConfig, resolve_config
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID
from active_knowledge_server.storage import QueryScope
from active_knowledge_server.storage.sqlite_store import SQLiteStorageAdapter


def test_phase4_serial_and_parallel_index_outputs_remain_equivalent(
    tmp_path: Path,
    capsys,
) -> None:
    workspace_root = tmp_path / "workspace"
    docs_root = tmp_path / "knowledge-sources"
    _write_workspace_fixture(workspace_root)
    _write_doc_fixture(docs_root)
    _write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
    )

    serial_config = _write_config(
        tmp_path,
        name="serial",
        workspace_root=workspace_root,
        docs_root=docs_root,
        workdir_root=tmp_path / "serial-workdir",
        workers=1,
    )
    parallel_config = _write_config(
        tmp_path,
        name="parallel",
        workspace_root=workspace_root,
        docs_root=docs_root,
        workdir_root=tmp_path / "parallel-workdir",
        workers="auto",
    )

    serial_payload, serial_stderr = _run_cli_json(
        [
            "index",
            "--config",
            str(serial_config.baseline_config_path),
            "--incremental",
            "--source",
            "all",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    parallel_payload, parallel_stderr = _run_cli_json(
        [
            "index",
            "--config",
            str(parallel_config.baseline_config_path),
            "--incremental",
            "--source",
            "all",
            "--format",
            "json",
        ],
        capsys=capsys,
    )

    assert serial_payload["result"]["result_status"] == "ready"
    assert parallel_payload["result"]["result_status"] == "ready"
    assert "\x1b[" not in serial_stderr
    assert "\x1b[" not in parallel_stderr

    serial_signatures = _collect_store_signatures(serial_config, cwd=tmp_path)
    parallel_signatures = _collect_store_signatures(parallel_config, cwd=tmp_path)

    assert serial_signatures == parallel_signatures

    for config in (serial_config, parallel_config):
        validate_payload, _ = _run_cli_json(
            [
                "validate",
                "--config",
                str(config.baseline_config_path),
                "--format",
                "json",
            ],
            capsys=capsys,
        )
        status_payload, _ = _run_cli_json(
            [
                "status",
                "--config",
                str(config.baseline_config_path),
                "--format",
                "json",
            ],
            capsys=capsys,
        )

        assert validate_payload["status"] == "ok"
        assert status_payload["status"] == "ok"
        assert validate_payload["index"]["result_status"] != "blocked"
        assert status_payload["index"]["result_status"] != "blocked"
        assert all(item["level"] != "blocked" for item in validate_payload["warnings"])
        assert all(item["level"] != "blocked" for item in status_payload["warnings"])


def _write_config(
    root: Path,
    *,
    name: str,
    workspace_root: Path,
    docs_root: Path,
    workdir_root: Path,
    workers: int | str,
) -> ResolvedConfig:
    config_data: ConfigDict = {
        "runtime": {
            "workdir": str(workdir_root),
            "baseline_dir": str(workdir_root / "baseline"),
            "local_dir": str(workdir_root / "local"),
            "source_docs_root": str(docs_root),
        },
        "project": {
            "workspace_root": str(workspace_root),
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
                "manifest": str(workdir_root / "baseline" / "manifest.json"),
            },
            "metadata": {
                "path": str(workdir_root / "baseline" / "db" / "metadata.db"),
                "mode": "readwrite",
            },
            "overlay": {
                "path": str(workdir_root / "local" / "db" / "overlay.db"),
                "mode": "readwrite",
            },
            "jobs": {
                "path": str(workdir_root / "local" / "db" / "jobs.db"),
                "mode": "readwrite",
            },
            "vector": {
                "path": str(workdir_root / "baseline" / "vectors"),
                "mode": "readwrite",
            },
            "vector_delta": {
                "path": str(workdir_root / "local" / "vectors"),
                "mode": "readwrite",
            },
            "cache_root": str(workdir_root / "local" / "cache"),
        },
        "indexing": {
            "workers": workers,
        },
    }
    config_path = root / f"{name}.yaml"
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    return resolve_config(config_path=config_path, env={}, cwd=root)


def _run_cli_json(argv: list[str], *, capsys) -> tuple[dict[str, object], str]:
    exit_code = main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    return payload, captured.err


def _collect_store_signatures(
    resolved: ResolvedConfig,
    *,
    cwd: Path,
) -> dict[str, tuple[str, ...]]:
    adapter = SQLiteStorageAdapter.from_config(resolved.model, cwd=cwd)
    reader = adapter.reader()
    scope = QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID)
    try:
        return {
            "files": _record_signature(reader.iter_files(scope)),
            "chunks": _record_signature(reader.iter_chunks(scope)),
            "entities": _record_signature(reader.iter_entities(scope)),
            "relations": _record_signature(reader.iter_relations(scope)),
            "evidence": _record_signature(reader.iter_evidence(scope)),
            "vector_refs": _record_signature(reader.iter_vector_refs(scope)),
        }
    finally:
        adapter.close()


def _record_signature(records: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(json.dumps(asdict(record), ensure_ascii=True, sort_keys=True) for record in records)
    )


def _write_workspace_fixture(workspace_root: Path) -> None:
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


def _write_doc_fixture(docs_root: Path) -> None:
    (docs_root / "api").mkdir(parents=True, exist_ok=True)
    (docs_root / "widgets").mkdir(parents=True, exist_ok=True)
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
    (docs_root / "widgets" / "heart_tile.md").write_text(
        """---
title: Heart Tile Widget
authority_level: official
widget: heart_tile
ui_framework: hmUI
code_paths:
  - ui/widgets/heart_tile.c
tags:
  - widget
  - heart
---
# Heart Tile Widget

## Properties
Heart tile shows bpm, status, and warning indicators on the watch face.
""",
        encoding="utf-8",
    )


def _write_profile_fixture(
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
    common = f'CONFIG_APP="{app}"\nCONFIG_BOARD="{board}"\n'
    defconfig_path.write_text(common + "CONFIG_HEALTH_BT=y\n", encoding="utf-8")
    dotconfig_path.write_text(
        common + "CONFIG_RUNTIME_READY=y\nCONFIG_HEALTH_BT=y\n",
        encoding="utf-8",
    )
