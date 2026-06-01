from __future__ import annotations

from pathlib import Path
from typing import Any

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing.pipeline import (
    INCREMENTAL_INDEX_STATE_SCHEMA_VERSION,
    IncrementalIndexPlan,
    IncrementalIndexState,
)
from active_knowledge_server.indexing.resume import (
    diff_plan_signature_payloads,
    format_plan_signature_mismatch_reason,
    make_index_plan_signature,
)


def test_plan_signature_is_stable_for_same_inputs(tmp_path: Path) -> None:
    plan = _plan()
    config = _config(tmp_path)

    first = make_index_plan_signature(plan, config=config)
    second = make_index_plan_signature(plan, config=config)

    assert first.digest == second.digest
    assert first.to_dict() == second.to_dict()
    assert first.payload["workspace_inventory_hash"] == "workspace-hash"


def test_plan_signature_ignores_parallel_and_writer_tuning(tmp_path: Path) -> None:
    plan = _plan()
    base = _config(tmp_path / "base")
    tuned = _config(
        tmp_path / "tuned",
        {
            "indexing": {
                "workers": 12,
                "parallel": {"mode": "process"},
                "writer": {"batch_size": 7, "commit_interval_ms": 333},
                "embeddings": {"batch_size": 4},
            }
        },
    )

    assert (
        make_index_plan_signature(plan, config=base).digest
        == make_index_plan_signature(
            plan,
            config=tuned,
        ).digest
    )


def test_plan_signature_changes_for_manifest_parser_embedding_and_config(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    base = make_index_plan_signature(_plan(), config=config)
    manifest_changed = make_index_plan_signature(
        _plan(source_docs_manifest_hash="docs-hash-2"),
        config=config,
    )
    embedding_changed = make_index_plan_signature(
        _plan(),
        config=_config(tmp_path / "embedding", {"indexing": {"embeddings": {"model": "e5"}}}),
    )
    parser_changed = make_index_plan_signature(
        _plan(),
        config=config,
        parser_schema_versions={
            "c_family": "c_family_parser.v2",
            "doc": "doc_parser.v1",
            "kconfig": "kconfig_parser.v1",
            "makefile": "makefile_parser.v1",
        },
    )
    storage_changed = make_index_plan_signature(
        _plan(),
        config=config,
        storage_schema_version="2.0.0",
    )
    config_changed = make_index_plan_signature(
        _plan(),
        config=_config(tmp_path / "config", {"indexing": {"docs": {"enable_html": False}}}),
    )

    assert manifest_changed.digest != base.digest
    assert embedding_changed.digest != base.digest
    assert parser_changed.digest != base.digest
    assert storage_changed.digest != base.digest
    assert config_changed.digest != base.digest
    assert "source_docs_manifest_hash" in diff_plan_signature_payloads(
        base.to_dict(),
        manifest_changed.to_dict(),
    )
    assert "embeddings.model" in diff_plan_signature_payloads(
        base.to_dict(),
        embedding_changed.to_dict(),
    )
    assert "schemas.parser.c_family" in diff_plan_signature_payloads(
        base.to_dict(),
        parser_changed.to_dict(),
    )
    assert "schemas.storage" in diff_plan_signature_payloads(
        base.to_dict(),
        storage_changed.to_dict(),
    )
    assert "impacting_config_summary.indexing.docs.enable_html" in diff_plan_signature_payloads(
        base.to_dict(),
        config_changed.to_dict(),
    )
    assert "plan signature mismatch:" in format_plan_signature_mismatch_reason(
        base.to_dict(),
        embedding_changed.to_dict(),
    )


def _plan(
    *,
    workspace_inventory_hash: str = "workspace-hash",
    source_docs_manifest_hash: str = "docs-hash",
) -> IncrementalIndexPlan:
    state = IncrementalIndexState(
        schema_version=INCREMENTAL_INDEX_STATE_SCHEMA_VERSION,
        snapshot_id="current",
        code_indexer_schema_version="code_indexer.v1",
        doc_indexer_schema_version="doc_indexer.v1",
        profile_collector_schema_version="profile_collector.v1",
        profile_conditioned_relation_schema_version="profile_conditioned_relations.v1",
        embedding_model_version="bge-m3",
        embeddings_enabled=True,
        workspace_inventory_hash=workspace_inventory_hash,
        source_docs_manifest_hash=source_docs_manifest_hash,
        code_files={"src/app.c": "code-hash"},
        doc_files={"guide.md": "doc-hash"},
        profile_config_hashes={"default": "profile-hash"},
    )
    return IncrementalIndexPlan(
        snapshot_id="current",
        source="all",
        previous_state=None,
        current_state=state,
        workspace_inventory=object(),  # type: ignore[arg-type]
        source_docs_manifest=object(),  # type: ignore[arg-type]
        collected_profiles=object(),  # type: ignore[arg-type]
    )


def _config(tmp_path: Path, overrides: ConfigDict | None = None) -> ActiveKnowledgeConfig:
    workspace = tmp_path / "workspace"
    docs = tmp_path / "knowledge-sources"
    workspace.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)
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
            "baseline": {"manifest": str(tmp_path / ".active-kb" / "baseline" / "manifest.json")},
            "metadata": {"path": str(tmp_path / ".active-kb" / "baseline" / "db" / "metadata.db")},
            "overlay": {"path": str(tmp_path / ".active-kb" / "local" / "db" / "overlay.db")},
            "jobs": {"path": str(tmp_path / ".active-kb" / "local" / "db" / "jobs.db")},
            "vector": {"path": str(tmp_path / ".active-kb" / "baseline" / "vectors")},
            "vector_delta": {"path": str(tmp_path / ".active-kb" / "local" / "vectors")},
            "cache_root": str(tmp_path / ".active-kb" / "local" / "cache"),
        },
    }
    if overrides:
        merged = _deep_merge(merged, overrides)
    return resolve_config(cli_overrides=merged, env={}, cwd=tmp_path).model


def _deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    result: dict[str, Any] = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = value
    return result
