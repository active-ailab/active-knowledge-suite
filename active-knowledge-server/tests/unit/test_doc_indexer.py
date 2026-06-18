from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID, DocumentIndexer
from active_knowledge_server.storage import FTSQuery, QueryScope, StorageWriteRequest, VectorQuery
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
)


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
            "baseline": {"manifest": str(tmp_path / ".active-kb" / "baseline" / "manifest.json")},
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


def build_adapters(
    config: ActiveKnowledgeConfig,
) -> tuple[SQLiteStorageAdapter, LanceDBVectorAdapter, Path, Path]:
    baseline_metadata = Path(config.storage.metadata.path)
    overlay_metadata = Path(config.storage.overlay.path)
    jobs_path = Path(config.storage.jobs.path)
    migrate_sqlite_store(baseline_metadata, target="baseline_metadata")
    migrate_sqlite_store(overlay_metadata, target="overlay_metadata")
    migrate_sqlite_store(jobs_path, target="jobs")

    metadata_adapter = SQLiteStorageAdapter(
        baseline_metadata_path=baseline_metadata,
        overlay_metadata_path=overlay_metadata,
        jobs_path=jobs_path,
    )
    baseline_vectors = Path(config.storage.vector.path)
    delta_vectors = Path(config.storage.vector_delta.path)
    vector_adapter = LanceDBVectorAdapter(
        baseline_vector_path=baseline_vectors,
        delta_vector_path=delta_vectors,
        metadata_adapter=metadata_adapter,
    )
    return metadata_adapter, vector_adapter, baseline_vectors, delta_vectors


def read_collection(path: Path, object_type: str = "chunk") -> list[dict[str, object]]:
    collection = path / f"{object_type}.json"
    if not collection.exists():
        return []
    payload = json.loads(collection.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return payload


def test_doc_indexer_indexes_api_and_widget_docs_and_writes_vectors(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    docs_root = Path(config.runtime.source_docs_root)
    (docs_root / "api").mkdir()
    (docs_root / "widgets").mkdir()
    (docs_root / "api" / "sensor.md").write_text(
        """---
title: Sensor Register API
authority_level: official
version: 1.2.0
module: sensor
code_symbols:
  - sensor_open
  - sensor_close
tags:
  - sensor
  - register
profiles:
  - watch
---
# Sensor Register API

## sensor_open
Open the sensor register and return a handle for runtime use.

## sensor_close
Close the sensor handle and release runtime resources.
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

    metadata_adapter, vector_adapter, _baseline_vectors, delta_vectors = build_adapters(config)
    indexer = DocumentIndexer.from_config(config, cwd=tmp_path)

    indexed = indexer.collect_and_store(
        metadata_adapter.writer(StorageWriteRequest(target="overlay")),
        vector_writer=vector_adapter.writer(StorageWriteRequest(target="overlay")),
        snapshot_id=CURRENT_SNAPSHOT_ID,
    )

    assert indexed.schema_version == "doc_indexer.v1"
    assert {record.source_id for record in indexed.source_records} == {
        "knowledge-api",
        "knowledge-widgets",
    }
    assert {record.relative_path for record in indexed.file_records} == {
        "knowledge-sources/api/sensor.md",
        "knowledge-sources/widgets/heart_tile.md",
    }
    assert any(record.chunk_type == "doc.api_item" for record in indexed.chunk_records)
    assert any(record.chunk_type == "doc.widget_item" for record in indexed.chunk_records)
    assert {record.entity_type for record in indexed.entity_records} >= {
        "Document",
        "API",
        "Widget",
    }
    assert {record.name for record in indexed.entity_records if record.entity_type == "API"} == {
        "sensor_open",
        "sensor_close",
    }
    assert {record.name for record in indexed.entity_records if record.entity_type == "Widget"} == {
        "heart_tile"
    }
    assert all(record.object_type == "entity" for record in indexed.evidence_records)
    assert any(
        record.citation_label is not None
        and "knowledge-sources/api/sensor.md" in record.citation_label
        for record in indexed.evidence_records
    )

    doc_matches = metadata_adapter.reader().search_fts(
        FTSQuery(
            index_name="doc_fts",
            query="sensor register handle",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
            domain="engineering",
            doc_type="api",
        )
    )
    assert doc_matches
    assert doc_matches[0].doc_type == "api"

    entity_matches = metadata_adapter.reader().search_fts(
        FTSQuery(
            index_name="entity_fts",
            query="sensor_open",
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
            domain="engineering",
            doc_type="api",
        )
    )
    assert entity_matches
    assert entity_matches[0].logical_object_id in {
        record.entity_id for record in indexed.entity_records if record.entity_type == "API"
    }

    stored_vectors = read_collection(delta_vectors)
    assert len(stored_vectors) == len(indexed.vector_writes)
    chunk_ids = {record.chunk_id for record in indexed.vector_refs}
    assert {record.chunk_id for record in indexed.vector_refs} <= {
        record.chunk_id for record in indexed.chunk_records
    }

    vector_result = vector_adapter.reader().search(
        VectorQuery(
            embedding=indexer.embed_text("sensor open runtime handle"),
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
            embedding_model_version=indexer.embedding_model_version,
        )
    )
    assert vector_result.matches
    assert vector_result.matches[0].logical_object_id in chunk_ids


def test_doc_indexer_reuses_embedding_cache_on_repeated_collect(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    docs_root = Path(config.runtime.source_docs_root)
    (docs_root / "engineering").mkdir()
    (docs_root / "engineering" / "runtime.md").write_text(
        """---
title: Runtime Scheduling Guide
authority_level: official
version: 1.0.0
---
# Runtime Scheduling Guide

Runtime scheduling keeps UI and sensor tasks responsive.

## Task Model
The runtime scheduler prioritizes sensor sampling, UI refresh, and health pipelines.
""",
        encoding="utf-8",
    )

    indexer = DocumentIndexer.from_config(config, cwd=tmp_path)
    cold = indexer.collect(snapshot_id=CURRENT_SNAPSHOT_ID)
    warm = indexer.collect(snapshot_id=CURRENT_SNAPSHOT_ID)

    cold_cache = dict(cold.metadata.get("embedding_cache", {}))
    warm_cache = dict(warm.metadata.get("embedding_cache", {}))

    assert cold.vector_writes
    assert cold_cache["cache_hits"] == 0
    assert cold_cache["computed_embeddings"] == len(cold.embedding_preparation.accepted_inputs)
    assert warm_cache["computed_embeddings"] == 0
    assert warm_cache["cache_hits"] == len(warm.embedding_preparation.accepted_inputs)
    assert [write.embedding for write in warm.vector_writes] == [
        write.embedding for write in cold.vector_writes
    ]
    assert list((Path(config.storage.cache_root) / "embeddings").rglob("*.json"))


def test_doc_indexer_reuses_cached_secret_scan_skip_results(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    docs_root = Path(config.runtime.source_docs_root)
    (docs_root / "engineering").mkdir()
    (docs_root / "engineering" / "secrets.md").write_text(
        """---
title: Runtime Secret Notes
authority_level: official
version: 1.0.0
---
# Runtime Secret Notes

TOKEN=super-secret-value
""",
        encoding="utf-8",
    )

    indexer = DocumentIndexer.from_config(config, cwd=tmp_path)
    cold = indexer.collect(snapshot_id=CURRENT_SNAPSHOT_ID)
    warm = indexer.collect(snapshot_id=CURRENT_SNAPSHOT_ID)

    cold_cache = dict(cold.metadata.get("embedding_cache", {}))
    warm_cache = dict(warm.metadata.get("embedding_cache", {}))

    assert cold.embedding_preparation.skipped_reports
    assert warm.embedding_preparation.skipped_reports
    assert cold.vector_writes == ()
    assert cold_cache["cache_stores"] >= 1
    assert warm_cache["computed_embeddings"] == 0
    assert warm_cache["cache_hits"] == len(warm.embedding_preparation.skipped_reports)
    assert [report.source_path for report in warm.embedding_preparation.skipped_reports] == [
        "knowledge-sources/engineering/secrets.md"
    ]


def test_doc_indexer_reports_api_version_missing_without_blocking_index(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    docs_root = Path(config.runtime.source_docs_root)
    (docs_root / "api").mkdir()
    (docs_root / "api" / "sensor.md").write_text(
        """---
title: Sensor Register API
authority_level: official
module: sensor
code_symbols:
  - sensor_open
---
# Sensor Register API

## sensor_open
Open the sensor register.
""",
        encoding="utf-8",
    )

    indexer = DocumentIndexer.from_config(config, cwd=tmp_path)
    indexed = indexer.collect(snapshot_id=CURRENT_SNAPSHOT_ID)

    assert len(indexed.file_records) == 1
    assert any(warning.code == "docs.version_missing" for warning in indexed.warnings)
    assert indexed.chunk_records


def test_doc_indexer_parallel_collect_matches_serial_output(tmp_path: Path) -> None:
    config = resolve_model(
        tmp_path,
        overrides={"indexing": {"workers": 1}},
    )
    parallel_config = config.model_copy(
        update={"indexing": config.indexing.model_copy(update={"workers": 4})}
    )
    docs_root = Path(config.runtime.source_docs_root)
    (docs_root / "api").mkdir()
    (docs_root / "widgets").mkdir()
    (docs_root / "api" / "sensor.md").write_text(
        """---
title: Sensor Register API
authority_level: official
version: 1.2.0
module: sensor
code_symbols:
  - sensor_open
---
# Sensor Register API

## sensor_open
Open the sensor register.
""",
        encoding="utf-8",
    )
    (docs_root / "widgets" / "heart_tile.md").write_text(
        """---
title: Heart Tile Widget
authority_level: official
widget: heart_tile
---
# Heart Tile Widget

## Properties
Heart tile shows bpm.
""",
        encoding="utf-8",
    )

    serial = DocumentIndexer.from_config(config, cwd=tmp_path).collect(
        snapshot_id=CURRENT_SNAPSHOT_ID
    )
    parallel = DocumentIndexer.from_config(parallel_config, cwd=tmp_path).collect(
        snapshot_id=CURRENT_SNAPSHOT_ID,
        source_docs_manifest=serial.source_manifest,
    )

    assert _record_signature(serial.file_records) == _record_signature(parallel.file_records)
    assert _record_signature(serial.chunk_records) == _record_signature(parallel.chunk_records)
    assert _record_signature(serial.entity_records) == _record_signature(parallel.entity_records)
    assert _record_signature(serial.evidence_records) == _record_signature(
        parallel.evidence_records
    )
    assert _record_signature(serial.vector_refs) == _record_signature(parallel.vector_refs)
    assert [write.embedding for write in serial.vector_writes] == [
        write.embedding for write in parallel.vector_writes
    ]
    assert serial.metadata["collect_workers"]["workers"] == 1
    assert parallel.metadata["collect_workers"]["workers"] == 2


def test_doc_indexer_parallel_collect_skips_single_failed_document(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = resolve_model(
        tmp_path,
        overrides={"indexing": {"workers": 4}},
    )
    docs_root = Path(config.runtime.source_docs_root)
    (docs_root / "api").mkdir()
    (docs_root / "widgets").mkdir()
    (docs_root / "api" / "sensor.md").write_text(
        """---
title: Sensor Register API
authority_level: official
version: 1.2.0
module: sensor
code_symbols:
  - sensor_open
---
# Sensor Register API

## sensor_open
Open the sensor register.
""",
        encoding="utf-8",
    )
    (docs_root / "widgets" / "heart_tile.md").write_text(
        """---
title: Heart Tile Widget
authority_level: official
widget: heart_tile
---
# Heart Tile Widget

## Properties
Heart tile shows bpm.
""",
        encoding="utf-8",
    )

    original_collect_entry = DocumentIndexer._collect_document_entry

    def flaky_collect_entry(self, *, snapshot_id: str, manifest, entry):
        if entry.relative_path == "widgets/heart_tile.md":
            raise RuntimeError("synthetic worker failure")
        return original_collect_entry(
            self,
            snapshot_id=snapshot_id,
            manifest=manifest,
            entry=entry,
        )

    monkeypatch.setattr(
        DocumentIndexer,
        "_collect_document_entry",
        flaky_collect_entry,
    )

    indexed = DocumentIndexer.from_config(config, cwd=tmp_path).collect(
        snapshot_id=CURRENT_SNAPSHOT_ID
    )

    assert {record.relative_path for record in indexed.file_records} == {
        "knowledge-sources/api/sensor.md"
    }
    failed_warnings = [
        warning for warning in indexed.warnings if warning.code == "docs.collect_failed"
    ]
    assert len(failed_warnings) == 1
    assert failed_warnings[0].relative_path == "knowledge-sources/widgets/heart_tile.md"
    assert indexed.chunk_records
    assert indexed.metadata["collect_workers"]["workers"] == 2


def deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    merged = dict(base)
    for key, value in overrides.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            nested = merged[key]
            assert isinstance(nested, dict)
            merged[key] = deep_merge(nested, value)
            continue
        merged[key] = value
    return merged


def _record_signature(records: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(json.dumps(asdict(record), ensure_ascii=True, sort_keys=True) for record in records)
    )
