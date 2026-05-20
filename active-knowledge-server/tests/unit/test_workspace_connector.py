from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from active_knowledge_server.config.loader import resolve_config
from active_knowledge_server.connectors.workspace import WorkspaceConnector


def build_connector(
    tmp_path: Path,
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> tuple[WorkspaceConnector, Path]:
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
            "paths": {
                "include": list(include),
                "exclude": list(exclude),
            },
        },
        env={},
        cwd=tmp_path,
    )
    return WorkspaceConnector.from_config(resolved.model, cwd=tmp_path), workspace


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def init_git_repo(repo: Path) -> str:
    git(repo, "init")
    git(repo, "config", "user.name", "Active Knowledge Tests")
    git(repo, "config", "user.email", "active-knowledge-tests@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return git(repo, "rev-parse", "HEAD")


def test_workspace_scan_generates_stable_inventory_without_compile_db(tmp_path: Path) -> None:
    connector, workspace = build_connector(
        tmp_path,
        include=("drivers/**", "README.md"),
        exclude=("build/**", "**/generated/**", "*.tmp"),
    )
    write_file(workspace / "drivers" / "input" / "button.c", "int button(void) { return 1; }\n")
    write_file(workspace / "drivers" / "generated" / "auto.c", "generated\n")
    write_file(workspace / "apps" / "watch" / "main.c", "int main(void) { return 0; }\n")
    write_file(workspace / "build" / "out" / "artifact.o", "object\n")
    write_file(workspace / "README.md", "# Active\n")
    write_file(workspace / "scratch.tmp", "temporary\n")

    first = connector.scan()
    second = connector.scan()

    assert [entry.relative_path for entry in first.files] == [
        "README.md",
        "drivers/input/button.c",
    ]
    assert first.inventory_hash == second.inventory_hash
    assert first.files[1].content_hash == second.files[1].content_hash
    assert first.files[1].language == "c"
    assert "drivers" in {area.name for area in first.areas}
    assert "build" not in {area.name for area in first.areas}
    assert json.loads(json.dumps(first.to_dict()))["file_count"] == 2


def test_workspace_scan_blocks_symlink_escape(tmp_path: Path) -> None:
    connector, workspace = build_connector(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    write_file(outside / "secret.c", "int secret;\n")
    try:
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Directory symlinks are not available in this environment: {exc}")

    inventory = connector.scan()

    assert all(entry.relative_path != "linked/secret.c" for entry in inventory.files)
    assert any(
        warning.code == "security.path_blocked"
        and warning.details["reason"] == "symlink_outside_allowlist"
        for warning in inventory.warnings
    )


def test_workspace_scan_detects_repo_boundaries_and_commit_map(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not available")

    connector, workspace = build_connector(tmp_path)
    write_file(workspace / "root.c", "int root;\n")
    root_head = init_git_repo(workspace)

    nested = workspace / "vendor" / "lib"
    nested.mkdir(parents=True)
    write_file(nested / "lib.c", "int lib;\n")
    nested_head = init_git_repo(nested)

    inventory = connector.scan()

    assert inventory.commit_map["."] == root_head
    assert inventory.commit_map["vendor/lib"] == nested_head
    assert {entry.repo_relative_path for entry in inventory.files} == {".", "vendor/lib"}


def test_workspace_scan_ignores_git_internals_even_without_config_exclude(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not available")

    connector, workspace = build_connector(tmp_path, exclude=())
    write_file(workspace / "main.c", "int main(void) { return 0; }\n")
    init_git_repo(workspace)

    inventory = connector.scan()

    assert "main.c" in {entry.relative_path for entry in inventory.files}
    assert all(not entry.relative_path.startswith(".git/") for entry in inventory.files)
