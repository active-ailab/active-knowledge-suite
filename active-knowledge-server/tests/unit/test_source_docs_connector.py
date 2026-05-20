from __future__ import annotations

import json
from pathlib import Path

import pytest

from active_knowledge_server.config.loader import resolve_config
from active_knowledge_server.connectors.source_docs import SourceDocsConnector


def build_connector(
    tmp_path: Path,
    *,
    create_source_docs_root: bool = True,
) -> tuple[SourceDocsConnector, Path]:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir(exist_ok=True)
    workdir.mkdir(exist_ok=True)
    if create_source_docs_root:
        source_docs.mkdir(exist_ok=True)
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
    return SourceDocsConnector.from_config(resolved.model, cwd=tmp_path), source_docs


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_source_docs_scan_builds_stable_manifest_and_skips_unsupported_areas(
    tmp_path: Path,
) -> None:
    connector, source_docs = build_connector(tmp_path)
    write_file(source_docs / "api" / "sensor.md", "# Sensor API\n")
    write_file(source_docs / "widgets" / "button.md", "# Button\n")
    write_file(source_docs / "engineering" / "runtime.html", "<h1>Runtime</h1>\n")
    write_file(source_docs / "experimental" / "draft.md", "# Draft\n")

    first = connector.scan()
    second = connector.scan()

    assert [entry.relative_path for entry in first.files] == [
        "api/sensor.md",
        "engineering/runtime.html",
        "widgets/button.md",
    ]
    assert [entry.format for entry in first.files] == ["markdown", "html", "markdown"]
    assert first.manifest_hash == second.manifest_hash
    assert any(
        warning.code == "source_docs.unsupported_area"
        and warning.details["area"] == "experimental"
        for warning in first.warnings
    )
    categories = {category.name: category for category in first.categories}
    assert categories["api"].exists is True
    assert categories["api"].file_count == 1
    assert categories["product"].exists is False
    assert json.loads(json.dumps(first.to_dict()))["file_count"] == 3
    assert first.to_baseline_manifest_fragment() == {
        "source_docs": {
            "schema_version": "source_docs_manifest.v1",
            "manifest_hash": first.manifest_hash,
            "root": str(source_docs),
            "file_count": 3,
            "supported_categories": [
                "api",
                "widgets",
                "engineering",
                "product",
                "design",
                "project",
                "qa",
                "release",
                "learned-seeds",
            ],
            "present_categories": ["api", "widgets", "engineering"],
            "file_count_by_category": {
                "api": 1,
                "widgets": 1,
                "engineering": 1,
            },
        }
    }


def test_source_docs_scan_creates_missing_root_and_returns_warning(tmp_path: Path) -> None:
    connector, source_docs = build_connector(tmp_path, create_source_docs_root=False)

    manifest = connector.scan()

    assert source_docs.exists()
    assert source_docs.is_dir()
    assert manifest.files == ()
    assert any(warning.code == "source_docs.root_created" for warning in manifest.warnings)
    assert all(category.exists is False for category in manifest.categories)


def test_source_docs_scan_blocks_symlink_escape(tmp_path: Path) -> None:
    connector, source_docs = build_connector(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    write_file(outside / "secret.md", "# Secret\n")
    try:
        (source_docs / "api").mkdir()
        (source_docs / "api" / "secret.md").symlink_to(outside / "secret.md")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlinks are not available in this environment: {exc}")

    manifest = connector.scan()

    assert all(entry.relative_path != "api/secret.md" for entry in manifest.files)
    assert any(
        warning.code == "security.path_blocked"
        and warning.details["reason"] == "symlink_outside_allowlist"
        for warning in manifest.warnings
    )