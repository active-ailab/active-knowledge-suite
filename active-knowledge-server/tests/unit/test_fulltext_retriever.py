from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID, CodeIndexer, DocumentIndexer
from active_knowledge_server.query import FullTextRetriever, FullTextSearchRequest
from active_knowledge_server.storage import (
	ALL_SCOPE,
	ChunkRecord,
	EntityRecord,
	FileRecord,
	QueryScope,
	StorageWriteRequest,
)
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
			"baseline": {
				"manifest": str(tmp_path / ".active-kb" / "baseline" / "manifest.json")
			},
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


def test_fulltext_retriever_recalls_api_and_widget_docs(tmp_path: Path) -> None:
	_config, adapter = build_indexed_fixture(tmp_path)
	retriever = FullTextRetriever.from_storage(adapter)

	api_result = retriever.search(
		FullTextSearchRequest(
			query="sensor_open",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
			domain="engineering",
			doc_type="api",
			top_k=8,
		)
	)
	assert api_result.matches
	api_indexes = {index_name for item in api_result.matches for index_name in item.matched_indexes}
	assert {"chunk_fts", "doc_fts", "entity_fts"} <= api_indexes
	assert any(item.title == "sensor_open" and item.primary_index == "entity_fts" for item in api_result.matches)
	assert all("doc_type=api" in item.match_reason for item in api_result.matches)
	assert all(item.score > 0.0 for item in api_result.matches)

	widget_result = retriever.search(
		FullTextSearchRequest(
			query="heart_tile",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
			doc_type="widget",
			top_k=6,
		)
	)
	assert widget_result.matches
	assert any(item.relative_path == "knowledge-sources/widgets/heart_tile.md" for item in widget_result.matches)
	assert any(item.doc_type == "widget" for item in widget_result.matches)


def test_fulltext_retriever_recalls_error_codes_paths_and_module_filtered_code(tmp_path: Path) -> None:
	_config, adapter = build_indexed_fixture(tmp_path)
	retriever = FullTextRetriever.from_storage(adapter)

	error_result = retriever.search(
		FullTextSearchRequest(
			query="ERR_HEALTH_TIMEOUT",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
			indexes=("entity_fts", "code_fts"),
			top_k=6,
		)
	)
	assert error_result.matches
	error_indexes = {index_name for item in error_result.matches for index_name in item.matched_indexes}
	assert "entity_fts" in error_indexes
	assert "code_fts" in error_indexes
	assert any(item.title == "ERR_HEALTH_TIMEOUT" for item in error_result.matches)

	path_result = retriever.search(
		FullTextSearchRequest(
			query="main.c",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
			indexes=("entity_fts",),
		)
	)
	assert path_result.matches
	assert path_result.matches[0].relative_path == "components/health/main.c"
	assert path_result.matches[0].primary_index == "entity_fts"

	module_result = retriever.search(
		FullTextSearchRequest(
			query="health_init",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
			indexes=("entity_fts", "code_fts"),
			module="health.logic",
			top_k=6,
		)
	)
	assert module_result.matches
	assert all("health.logic" in item.module_names for item in module_result.matches)
	assert all("module=health.logic" in item.match_reason for item in module_result.matches)


def test_fulltext_retriever_applies_profile_scope(tmp_path: Path) -> None:
	_config, adapter = build_indexed_fixture(tmp_path)
	seed_profile_specific_entities(adapter)
	retriever = FullTextRetriever.from_storage(adapter)

	watch = retriever.search(
		FullTextSearchRequest(
			query="profile_only",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
			indexes=("entity_fts",),
		)
	)
	assert [item.profile_id for item in watch.matches] == ["watch"]
	assert all("profile=watch" in item.match_reason for item in watch.matches)

	sensorhub = retriever.search(
		FullTextSearchRequest(
			query="profile_only",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="sensorhub"),
			indexes=("entity_fts",),
		)
	)
	assert [item.profile_id for item in sensorhub.matches] == ["sensorhub"]

	all_profiles = retriever.search(
		FullTextSearchRequest(
			query="profile_only",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id=ALL_SCOPE),
			indexes=("entity_fts",),
		)
	)
	assert {item.profile_id for item in all_profiles.matches} == {"watch", "sensorhub"}
	assert all_profiles.total_matches == 2


def test_fulltext_retriever_applies_source_index_filter(tmp_path: Path) -> None:
	config = resolve_model(tmp_path)
	adapter = build_adapter(config)
	seed_source_index_docs(adapter)
	retriever = FullTextRetriever.from_storage(adapter)

	all_sources = retriever.search(
		FullTextSearchRequest(
			query="sensor source filter",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
			indexes=("doc_fts",),
			top_k=4,
		)
	)
	assert {item.title for item in all_sources.matches} == {
		"Baseline Sensor Source",
		"Overlay Sensor Source",
	}

	baseline = retriever.search(
		FullTextSearchRequest(
			query="sensor source filter",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
			indexes=("doc_fts",),
			source_index="baseline",
		)
	)
	assert [item.source_index for item in baseline.matches] == ["baseline"]
	assert [item.title for item in baseline.matches] == ["Baseline Sensor Source"]
	assert all("source_index=baseline" in item.match_reason for item in baseline.matches)

	overlay = retriever.search(
		FullTextSearchRequest(
			query="sensor source filter",
			scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
			indexes=("doc_fts",),
			source_index="overlay",
		)
	)
	assert [item.source_index for item in overlay.matches] == ["overlay"]
	assert [item.title for item in overlay.matches] == ["Overlay Sensor Source"]


def build_indexed_fixture(tmp_path: Path) -> tuple[ActiveKnowledgeConfig, SQLiteStorageAdapter]:
	config = resolve_model(tmp_path)
	workspace_root = Path(config.project.workspace_root)
	docs_root = Path(config.runtime.source_docs_root)
	seed_workspace(workspace_root)
	seed_docs(docs_root)

	adapter = build_adapter(config)
	writer = adapter.writer(StorageWriteRequest(target="overlay"))
	CodeIndexer.from_config(config, cwd=tmp_path).collect_and_store(
		writer,
		snapshot_id=CURRENT_SNAPSHOT_ID,
	)
	DocumentIndexer.from_config(config, cwd=tmp_path).collect_and_store(
		writer,
		snapshot_id=CURRENT_SNAPSHOT_ID,
	)
	return config, adapter


def seed_workspace(workspace_root: Path) -> None:
	component_dir = workspace_root / "components" / "health"
	component_dir.mkdir(parents=True)
	(component_dir / "module.mk").write_text(
		"""NAME = health
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
#define ERR_HEALTH_TIMEOUT 17

typedef struct HealthState {
    int ready;
} HealthState;

int health_init(void)
{
    return ERR_HEALTH_TIMEOUT + HEALTH_DEFAULT;
}
""",
		encoding="utf-8",
	)
	(component_dir / "health.h").write_text(
		"""#ifndef HEALTH_H
#define HEALTH_H

int health_init(void);

#endif
""",
		encoding="utf-8",
	)
	(component_dir / "bt.c").write_text(
		"""#include "health.h"

int health_bt_init(void)
{
    return 0;
}
""",
		encoding="utf-8",
	)


def seed_docs(docs_root: Path) -> None:
	(docs_root / "api").mkdir(parents=True)
	(docs_root / "widgets").mkdir(parents=True)
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


def seed_profile_specific_entities(adapter: SQLiteStorageAdapter) -> None:
	writer = adapter.writer(StorageWriteRequest(target="overlay"))
	watch_file = FileRecord(
		file_id="file-profile-watch",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		source_id="workspace",
		relative_path="profiles/watch_only.c",
		content_hash="hash:watch-profile",
		source_scope="components",
		profile_id="watch",
		language="c",
	)
	sensorhub_file = FileRecord(
		file_id="file-profile-sensorhub",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		source_id="workspace",
		relative_path="profiles/sensorhub_only.c",
		content_hash="hash:sensorhub-profile",
		source_scope="components",
		profile_id="sensorhub",
		language="c",
	)
	watch_entity = EntityRecord(
		entity_id="entity:profile_only:watch",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		file_id=watch_file.file_id,
		entity_type="Function",
		name="profile_only",
		qualified_name="profiles/watch_only.c::profile_only",
		path="profiles/watch_only.c#profile_only",
		source_scope="components",
		profile_id="watch",
		start_line=1,
		end_line=3,
		metadata={"summary": "watch-only symbol"},
	)
	sensorhub_entity = EntityRecord(
		entity_id="entity:profile_only:sensorhub",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		file_id=sensorhub_file.file_id,
		entity_type="Function",
		name="profile_only",
		qualified_name="profiles/sensorhub_only.c::profile_only",
		path="profiles/sensorhub_only.c#profile_only",
		source_scope="components",
		profile_id="sensorhub",
		start_line=1,
		end_line=3,
		metadata={"summary": "sensorhub-only symbol"},
	)
	for record in (watch_file, sensorhub_file, watch_entity, sensorhub_entity):
		if isinstance(record, FileRecord):
			writer.upsert_file(record)
		else:
			writer.upsert_entity(record)
	writer.flush()


def seed_source_index_docs(adapter: SQLiteStorageAdapter) -> None:
	baseline_writer = adapter.writer(StorageWriteRequest(target="baseline"))
	overlay_writer = adapter.writer(StorageWriteRequest(target="overlay"))

	baseline_file = FileRecord(
		file_id="file-source-filter-baseline",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		source_id="knowledge-api",
		relative_path="knowledge-sources/api/baseline_source.md",
		content_hash="hash:baseline-source",
		source_scope="api",
		language="markdown",
	)
	overlay_file = FileRecord(
		file_id="file-source-filter-overlay",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		source_id="knowledge-api",
		relative_path="knowledge-sources/api/overlay_source.md",
		content_hash="hash:overlay-source",
		source_scope="api",
		language="markdown",
	)
	baseline_chunk = ChunkRecord(
		chunk_id="chunk-source-filter-baseline",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		file_id=baseline_file.file_id,
		content_hash="hash:baseline-source-chunk",
		chunk_type="doc.section",
		ordinal=0,
		text="sensor source filter baseline response",
		source_scope="api",
		metadata={
			"title": "Baseline Sensor Source",
			"domain": "engineering",
			"doc_type": "api",
		},
	)
	overlay_chunk = ChunkRecord(
		chunk_id="chunk-source-filter-overlay",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		file_id=overlay_file.file_id,
		content_hash="hash:overlay-source-chunk",
		chunk_type="doc.section",
		ordinal=0,
		text="sensor source filter overlay response",
		source_scope="api",
		metadata={
			"title": "Overlay Sensor Source",
			"domain": "engineering",
			"doc_type": "api",
		},
	)
	baseline_writer.upsert_file(baseline_file)
	baseline_writer.upsert_chunk(baseline_chunk)
	baseline_writer.flush()
	overlay_writer.upsert_file(overlay_file)
	overlay_writer.upsert_chunk(overlay_chunk)
	overlay_writer.flush()


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