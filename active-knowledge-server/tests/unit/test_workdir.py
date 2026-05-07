from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from active_knowledge_server.config.loader import ConfigError, resolve_config
from active_knowledge_server.config.workdir import initialize_workdir


def resolve_for_workdir(tmp_path: Path):
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir(exist_ok=True)
    source_docs.mkdir(exist_ok=True)
    return resolve_config(
        cli_overrides={
            "runtime": {
                "workdir": str(workdir),
                "source_docs_root": str(source_docs),
            },
            "project": {"workspace_root": str(workspace)},
        },
        env={},
        cwd=tmp_path,
    )


def test_initialize_workdir_creates_full_layout_and_local_config(tmp_path: Path) -> None:
    resolved = resolve_for_workdir(tmp_path)

    result = initialize_workdir(resolved, cwd=tmp_path)
    layout = result.layout

    assert layout.baseline_dir.is_dir()
    assert layout.local_dir.is_dir()
    assert layout.local_config_path.is_file()
    assert layout.local_db_dir.is_dir()
    assert layout.local_vectors_dir.is_dir()
    assert layout.local_artifacts_dir.is_dir()
    assert layout.local_cache_dir.is_dir()
    assert layout.local_logs_dir.is_dir()
    assert layout.local_tmp_dir.is_dir()
    assert layout.local_locks_dir.is_dir()
    assert (layout.local_dir / ".gitignore").is_file()
    assert result.baseline_manifest.exists is False
    assert any(warning.code == "baseline.manifest_missing" for warning in result.warnings)


def test_initialize_workdir_is_idempotent_and_preserves_local_config(tmp_path: Path) -> None:
    resolved = resolve_for_workdir(tmp_path)
    first = initialize_workdir(resolved, cwd=tmp_path)
    first.layout.local_config_path.write_text(
        "project:\n  default_profile: manual\n",
        encoding="utf-8",
    )

    second = initialize_workdir(resolved, cwd=tmp_path)

    assert second.created == ()
    assert "manual" in second.layout.local_config_path.read_text(encoding="utf-8")


def test_initialize_workdir_force_rewrites_local_config(tmp_path: Path) -> None:
    resolved = resolve_for_workdir(tmp_path)
    first = initialize_workdir(resolved, cwd=tmp_path)
    first.layout.local_config_path.write_text(
        "project:\n  default_profile: manual\n",
        encoding="utf-8",
    )

    second = initialize_workdir(resolved, cwd=tmp_path, force=True)

    assert "manual" not in second.layout.local_config_path.read_text(encoding="utf-8")


def test_initialize_workdir_accepts_readable_baseline_manifest(tmp_path: Path) -> None:
    resolved = resolve_for_workdir(tmp_path)
    manifest = tmp_path / ".active-kb" / "baseline" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"baseline_id": "unit-test"}\n', encoding="utf-8")

    result = initialize_workdir(resolved, cwd=tmp_path)

    assert result.baseline_manifest.exists is True
    assert result.baseline_manifest.readable is True
    assert all(warning.code != "baseline.manifest_missing" for warning in result.warnings)


def test_initialize_workdir_warns_for_tracked_runtime_local_files(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)

    resolved = resolve_config(
        cli_overrides={"runtime": {"workdir": str(repo / ".active-kb")}},
        env={},
        cwd=repo,
    )
    tracked_file = repo / ".active-kb" / "local" / "db" / "overlay.db"
    tracked_file.parent.mkdir(parents=True)
    tracked_file.write_text("tracked runtime file", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", ".active-kb/local/db/overlay.db"],
        check=True,
        capture_output=True,
        text=True,
    )

    result = initialize_workdir(resolved, cwd=repo)

    tracked_warning = next(
        warning for warning in result.warnings if warning.code == "workdir.local_tracked"
    )
    assert tracked_warning.details == ("db/overlay.db",)


def test_initialize_workdir_fails_fast_when_workdir_is_not_directory(tmp_path: Path) -> None:
    resolved = resolve_for_workdir(tmp_path)
    workdir = tmp_path / ".active-kb"
    workdir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigError, match="workdir path exists but is not a directory"):
        initialize_workdir(resolved, cwd=tmp_path)


def test_initialize_workdir_fails_fast_when_parent_is_unwritable(tmp_path: Path) -> None:
    parent = tmp_path / "readonly"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        if os.access(parent, os.W_OK):
            pytest.skip("current user can still write to chmod 0500 directory")
        resolved = resolve_config(
            cli_overrides={"runtime": {"workdir": str(parent / ".active-kb")}},
            env={},
            cwd=tmp_path,
        )

        with pytest.raises(ConfigError, match="workdir is not writable"):
            initialize_workdir(resolved, cwd=tmp_path)
    finally:
        parent.chmod(0o700)
