from __future__ import annotations

import os
from pathlib import Path

import pytest

from active_knowledge_server.config.loader import resolve_config
from active_knowledge_server.security.path_guard import PathBlockedError, PathGuard


def build_guard(tmp_path: Path) -> tuple[PathGuard, Path, Path, Path]:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    workdir.mkdir()
    resolved = resolve_config(
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
    return PathGuard.from_config(resolved.model, cwd=tmp_path), workspace, source_docs, workdir


def create_dir_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Directory symlinks are not available in this environment: {exc}")


def test_workspace_path_is_allowed_and_displayed_relative(tmp_path: Path) -> None:
    guard, workspace, _, _ = build_guard(tmp_path)
    source = workspace / "src" / "main.c"
    source.parent.mkdir()
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    guarded = guard.guard("src/main.c", base=workspace, must_exist=True)

    assert guarded.normalized_path == source
    assert guarded.display_path == "workspace:src/main.c"


def test_source_docs_path_is_allowed(tmp_path: Path) -> None:
    guard, _, source_docs, _ = build_guard(tmp_path)
    doc = source_docs / "api" / "sensor.md"
    doc.parent.mkdir()
    doc.write_text("# Sensor\n", encoding="utf-8")

    guarded = guard.guard(doc, must_exist=True)

    assert guarded.display_path == "source_docs:api/sensor.md"


def test_workdir_path_is_allowed(tmp_path: Path) -> None:
    guard, _, _, workdir = build_guard(tmp_path)
    artifact = workdir / "local" / "artifacts" / "report.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")

    guarded = guard.guard(artifact, must_exist=True)

    assert guarded.display_path == "workdir:local/artifacts/report.json"


def test_dotdot_escape_is_blocked(tmp_path: Path) -> None:
    guard, workspace, _, _ = build_guard(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("nope\n", encoding="utf-8")

    with pytest.raises(PathBlockedError) as exc_info:
        guard.guard("../secret.txt", base=workspace, must_exist=True)

    warning = exc_info.value.warning.to_dict()
    assert warning["code"] == "security.path_blocked"
    assert warning["details"]["reason"] == "path_outside_allowlist"


def test_symlink_escape_is_blocked_by_default(tmp_path: Path) -> None:
    guard, workspace, _, _ = build_guard(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret\n", encoding="utf-8")
    link = workspace / "linked"
    create_dir_symlink(link, outside)

    with pytest.raises(PathBlockedError) as exc_info:
        guard.guard("linked/secret.txt", base=workspace, must_exist=True)

    assert exc_info.value.warning.to_dict()["details"]["reason"] == "symlink_outside_allowlist"


def test_symlink_escape_can_be_explicitly_allowed(tmp_path: Path) -> None:
    guard, workspace, _, _ = build_guard(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret\n", encoding="utf-8")
    link = workspace / "linked"
    create_dir_symlink(link, outside)

    guarded = guard.guard(
        "linked/secret.txt",
        base=workspace,
        must_exist=True,
        allow_symlink_escape=True,
    )

    assert guarded.display_path == "workspace:linked/secret.txt"
    assert guarded.real_path == target


def test_symlink_to_another_allowlisted_root_is_allowed(tmp_path: Path) -> None:
    guard, workspace, source_docs, _ = build_guard(tmp_path)
    doc = source_docs / "guide.md"
    doc.write_text("# Guide\n", encoding="utf-8")
    link = workspace / "docs"
    create_dir_symlink(link, source_docs)

    guarded = guard.guard("docs/guide.md", base=workspace, must_exist=True)

    assert guarded.display_path == "workspace:docs/guide.md"
    assert guarded.real_path == doc


def test_internal_dotdot_is_normalized_and_allowed(tmp_path: Path) -> None:
    guard, workspace, _, _ = build_guard(tmp_path)
    source = workspace / "src" / "main.c"
    source.parent.mkdir()
    source.write_text("int main(void);\n", encoding="utf-8")

    guarded = guard.guard(os.fspath(Path("src") / ".." / "src" / "main.c"), base=workspace)

    assert guarded.normalized_path == source
    assert guarded.display_path == "workspace:src/main.c"


def test_blocked_response_contract_shape(tmp_path: Path) -> None:
    guard, workspace, _, _ = build_guard(tmp_path)

    with pytest.raises(PathBlockedError) as exc_info:
        guard.guard("../../etc/passwd", base=workspace)

    payload = exc_info.value.to_blocked_response()
    assert payload["result_status"] == "blocked"
    assert payload["items"] == []
    assert payload["evidence_refs"] == []
    assert payload["warnings"][0]["level"] == "blocked"
    assert payload["warnings"][0]["code"] == "security.path_blocked"
