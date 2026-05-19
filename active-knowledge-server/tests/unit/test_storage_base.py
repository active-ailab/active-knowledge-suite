from __future__ import annotations

import ast
from pathlib import Path

import pytest

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage.base import (
    BaselineWriteBlockedError,
    StorageWriteRequest,
    default_write_request,
    validate_write_request,
)

_FORBIDDEN_QUERY_IMPORTS = (
    "sqlite3",
    "lancedb",
    "active_knowledge_server.storage.sqlite_store",
    "active_knowledge_server.storage.lancedb_store",
)


def resolve_model(
    tmp_path: Path,
    overrides: ConfigDict | None = None,
) -> ActiveKnowledgeConfig:
    return resolve_config(cli_overrides=overrides or {}, env={}, cwd=tmp_path).model


def test_default_write_request_uses_overlay_for_normal_runs(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)

    request = default_write_request(config)

    assert request == StorageWriteRequest(target="overlay", operation_mode="normal")


def test_default_write_request_blocks_baseline_without_publish_mode(tmp_path: Path) -> None:
    config = resolve_model(
        tmp_path,
        {"indexing": {"write_target": "baseline"}},
    )

    with pytest.raises(BaselineWriteBlockedError, match="Baseline writes are blocked"):
        default_write_request(config)


def test_default_write_request_allows_explicit_baseline_publish(tmp_path: Path) -> None:
    config = resolve_model(
        tmp_path,
        {
            "indexing": {"write_target": "baseline"},
            "storage": {
                "metadata": {"mode": "readwrite"},
                "vector": {"mode": "readwrite"},
            },
        },
    )

    request = default_write_request(config, operation_mode="baseline_publish")

    assert request == StorageWriteRequest(
        target="baseline",
        operation_mode="baseline_publish",
    )


def test_overlay_write_requires_writable_overlay_stores(tmp_path: Path) -> None:
    config = resolve_model(
        tmp_path,
        {"storage": {"overlay": {"mode": "readonly"}}},
    )

    with pytest.raises(ValueError, match="storage.overlay.mode must be readwrite"):
        validate_write_request(config, StorageWriteRequest(target="overlay"))


def test_query_modules_only_depend_on_stable_storage_contracts() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    query_dir = repo_root / "src" / "active_knowledge_server" / "query"

    for path in sorted(query_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = list(forbidden_imports(tree))
        assert forbidden == [], f"{path.name} imports physical backend modules: {forbidden}"


def forbidden_imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_forbidden(alias.name):
                    names.append(alias.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and is_forbidden(node.module)
        ):
            names.append(node.module)
    return names


def is_forbidden(module_name: str) -> bool:
    return any(
        module_name == forbidden or module_name.startswith(f"{forbidden}.")
        for forbidden in _FORBIDDEN_QUERY_IMPORTS
    )
