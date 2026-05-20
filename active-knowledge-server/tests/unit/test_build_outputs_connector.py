from __future__ import annotations

import json
from pathlib import Path

import pytest

from active_knowledge_server.config.loader import resolve_config
from active_knowledge_server.connectors.build_outputs import BuildOutputsConnector


def build_connector(tmp_path: Path) -> tuple[BuildOutputsConnector, Path]:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir(exist_ok=True)
    source_docs.mkdir(exist_ok=True)
    workdir.mkdir(exist_ok=True)
    resolved = resolve_config(
        cli_overrides={
            "runtime": {
                "workdir": str(workdir),
                "source_docs_root": str(source_docs),
            },
            "project": {"workspace_root": str(workspace)},
            "profiles": {
                "discovery": {
                    "defconfig_roots": ["configs"],
                    "dotconfig_candidates": ["build/.config", "build/out_hub/.config"],
                }
            },
            "indexing": {
                "code": {
                    "compile_db_candidates": [
                        "build/compile_commands.json",
                        "build/out_hub/compile_commands.json",
                    ]
                }
            },
        },
        env={},
        cwd=tmp_path,
    )
    return BuildOutputsConnector.from_config(resolved.model, cwd=tmp_path), workspace


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_outputs_scan_discovers_defconfigs_dotconfigs_and_compile_db(
    tmp_path: Path,
) -> None:
    connector, workspace = build_connector(tmp_path)
    write_file(workspace / "configs" / "board_defconfig", "CONFIG_BOARD=y\n")
    write_file(workspace / "configs" / "apps" / "demo" / "demo_defconfig", "CONFIG_DEMO=y\n")
    write_file(workspace / "build" / ".config", "CONFIG_ACTIVE=y\n")
    write_file(workspace / "build" / "out_hub" / "compile_commands.json", "[]\n")

    first = connector.scan()
    second = connector.scan()

    assert [entry.relative_path for entry in first.defconfigs] == [
        "configs/apps/demo/demo_defconfig",
        "configs/board_defconfig",
    ]
    assert [entry.relative_path for entry in first.dotconfigs] == ["build/.config"]
    assert [entry.relative_path for entry in first.compile_dbs] == [
        "build/out_hub/compile_commands.json",
    ]
    assert all(entry.content_hash is not None for entry in first.defconfigs)
    assert first.manifest_hash == second.manifest_hash
    assert not any(warning.code == "compile_db.missing" for warning in first.warnings)
    assert json.loads(json.dumps(first.to_dict()))["defconfig_count"] == 2


def test_build_outputs_scan_warns_when_compile_db_is_missing_but_succeeds(
    tmp_path: Path,
) -> None:
    connector, workspace = build_connector(tmp_path)
    write_file(workspace / "configs" / "board_defconfig", "CONFIG_BOARD=y\n")
    write_file(workspace / "build" / ".config", "CONFIG_ACTIVE=y\n")

    manifest = connector.scan()

    assert [entry.relative_path for entry in manifest.defconfigs] == ["configs/board_defconfig"]
    assert [entry.relative_path for entry in manifest.dotconfigs] == ["build/.config"]
    assert manifest.compile_dbs == ()
    assert any(
        warning.code == "compile_db.missing"
        and warning.details["candidates"]
        == ["build/compile_commands.json", "build/out_hub/compile_commands.json"]
        and warning.details["non_blocking"] is True
        for warning in manifest.warnings
    )


def test_build_outputs_scan_blocks_symlink_escape_for_compile_db(tmp_path: Path) -> None:
    connector, workspace = build_connector(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    write_file(outside / "compile_commands.json", "[]\n")
    try:
        (workspace / "build" / "out_hub").mkdir(parents=True)
        (workspace / "build" / "out_hub" / "compile_commands.json").symlink_to(
            outside / "compile_commands.json"
        )
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlinks are not available in this environment: {exc}")

    manifest = connector.scan()

    assert manifest.compile_dbs == ()
    assert any(
        warning.code == "security.path_blocked"
        and warning.details["reason"] == "symlink_outside_allowlist"
        for warning in manifest.warnings
    )
    assert any(warning.code == "compile_db.missing" for warning in manifest.warnings)