from __future__ import annotations

import json
from pathlib import Path

from active_knowledge_server.cli import main
from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage import (
    ChunkRecord,
    EvidenceRecord,
    FileRecord,
    QueryScope,
    RelationRecord,
    StorageWriteRequest,
    VectorRefRecord,
)
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
    sqlite_connection,
)
from active_knowledge_server.storage.validation import validate_storage_consistency


def resolve_model(tmp_path: Path) -> ActiveKnowledgeConfig:
    workspace = tmp_path / "workspace"
    docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    docs.mkdir()
    baseline_db = tmp_path / "baseline.db"
    overlay_db = tmp_path / "overlay.db"
    jobs_db = tmp_path / "jobs.db"
    overrides: ConfigDict = {
        "runtime": {
            "workdir": str(tmp_path / ".active-kb"),
            "baseline_dir": str(tmp_path / ".active-kb" / "baseline"),
            "local_dir": str(tmp_path / ".active-kb" / "local"),
            "source_docs_root": str(docs),
        },
        "project": {"workspace_root": str(workspace)},
        "storage": {
            "baseline": {
                "manifest": str(tmp_path / ".active-kb" / "baseline" / "manifest.json")
            },
            "metadata": {"path": str(baseline_db), "mode": "readwrite"},
            "overlay": {"path": str(overlay_db), "mode": "readwrite"},
            "jobs": {"path": str(jobs_db), "mode": "readwrite"},
            "vector": {"path": str(tmp_path / "baseline-vectors"), "mode": "readwrite"},
            "vector_delta": {"path": str(tmp_path / "delta-vectors"), "mode": "readwrite"},
        },
    }
    return resolve_config(cli_overrides=overrides, env={}, cwd=tmp_path).model


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


def seed_file_and_chunk(adapter: SQLiteStorageAdapter) -> None:
    writer = adapter.writer(StorageWriteRequest(target="baseline"))
    writer.upsert_file(
        FileRecord(
            file_id="file-doc",
            snapshot_id="current",
            source_id="docs",
            relative_path="knowledge-sources/runtime.md",
            content_hash="hash:file",
            language="md",
        )
    )
    writer.upsert_chunk(
        ChunkRecord(
            chunk_id="chunk-doc",
            snapshot_id="current",
            file_id="file-doc",
            content_hash="hash:chunk",
            chunk_type="doc_section",
            ordinal=0,
            text="Runtime queue handler details.",
            metadata={"doc_type": "engineering"},
        )
    )


def test_validate_cli_json_contains_machine_readable_storage_report(capsys) -> None:
    exit_code = main(["validate", "--format", "json"])

    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == "active_kb_validate.v1"
    assert payload["storage_report"]["schema_version"] == "validate_report.v1"
    assert payload["storage_report"]["status"] in {"ok", "degraded", "blocked"}
    assert isinstance(payload["storage_report"]["checks"], list)


def test_validate_reports_missing_fts_row(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    adapter = build_adapter(config)
    seed_file_and_chunk(adapter)
    with sqlite_connection(Path(config.storage.metadata.path)) as connection:
        connection.execute("DELETE FROM chunk_fts WHERE object_id = ?", ("chunk-doc",))
        connection.commit()

    report = validate_storage_consistency(config, cwd=tmp_path)

    assert report.status == "degraded"
    assert [check.check_code for check in report.checks] == [
        "storage.fts_metadata_mismatch"
    ]
    assert report.checks[0].affected_objects == ("chunk:chunk-doc",)


def test_validate_reports_missing_vector_payload(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    adapter = build_adapter(config)
    seed_file_and_chunk(adapter)
    adapter.writer(StorageWriteRequest(target="baseline")).upsert_vector_ref(
        VectorRefRecord(
            vector_ref_id="vec-doc",
            object_type="chunk",
            object_id="chunk-doc",
            chunk_id="chunk-doc",
            embedding_model_version="bge-m3",
            content_hash="hash:chunk",
        )
    )

    report = validate_storage_consistency(config, cwd=tmp_path)

    assert "storage.vector_ref_missing" in {check.check_code for check in report.checks}


def test_validate_reports_dangling_evidence(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    adapter = build_adapter(config)
    adapter.writer(StorageWriteRequest(target="baseline")).upsert_evidence(
        EvidenceRecord(
            evidence_id="ev-missing-file",
            snapshot_id="current",
            object_type="chunk",
            object_id="chunk-missing",
            file_id="file-missing",
            chunk_id="chunk-missing",
            excerpt="Missing source.",
        )
    )

    report = validate_storage_consistency(config, cwd=tmp_path)

    codes = [check.check_code for check in report.checks]
    assert "storage.dangling_evidence" in codes


def test_validate_reports_orphan_relation(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    adapter = build_adapter(config)
    adapter.writer(StorageWriteRequest(target="baseline")).upsert_relation(
        RelationRecord(
            relation_id="rel-orphan",
            snapshot_id="current",
            relation_type="calls",
            src_entity_id="entity-missing-src",
            dst_entity_id="entity-missing-dst",
        )
    )

    report = validate_storage_consistency(config, cwd=tmp_path, scope=QueryScope())

    assert "storage.orphan_relation" in {check.check_code for check in report.checks}
