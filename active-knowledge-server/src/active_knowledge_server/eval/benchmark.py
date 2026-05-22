"""Deterministic synthetic benchmark used by the E7-02 quality gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.eval.cases import EvalCase
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID, CodeIndexer, DocumentIndexer
from active_knowledge_server.models.query import QueryRequest
from active_knowledge_server.models.responses import QueryResult
from active_knowledge_server.query.retrievers import FullTextRetriever, GraphRetriever, SymbolRetriever
from active_knowledge_server.query.router import QueryRouter
from active_knowledge_server.query.service import QueryService
from active_knowledge_server.security.config import validate_startup_security
from active_knowledge_server.storage import EntityRecord, EvidenceRecord, FileRecord, StorageWriteRequest
from active_knowledge_server.storage.sqlite_store import SQLiteStorageAdapter, migrate_sqlite_store


@dataclass(frozen=True)
class _BenchmarkProfileResolution:
	requested: str | None
	status: str
	resolved_profile_id: str | None = None

	def to_dict(self) -> dict[str, object]:
		return {
			"requested": self.requested,
			"status": self.status,
			"resolved_profile_id": self.resolved_profile_id,
			"profile_record_id": None,
			"source": "quality_benchmark",
			"confidence": 1.0 if self.status == "resolved" else None,
			"candidates": [],
			"warnings": [],
		}


@dataclass(frozen=True)
class _BenchmarkCollectedProfiles:
	resolution: _BenchmarkProfileResolution


class _BenchmarkProfileCollector:
	"""Deterministic profile collector for quality benchmark cases."""

	_VALID_PROFILES = {"watch", "sensorhub"}

	def collect(
		self,
		snapshot_id: str | None = None,
		*,
		requested_profile_id: str | None = None,
		build_outputs_manifest: object | None = None,
		client_context: dict[str, object] | None = None,
	) -> _BenchmarkCollectedProfiles:
		del snapshot_id, build_outputs_manifest, client_context
		if requested_profile_id in self._VALID_PROFILES:
			return _BenchmarkCollectedProfiles(
				resolution=_BenchmarkProfileResolution(
					requested=requested_profile_id,
					status="resolved",
					resolved_profile_id=requested_profile_id,
				)
			)
		if requested_profile_id not in (None, "auto"):
			return _BenchmarkCollectedProfiles(
				resolution=_BenchmarkProfileResolution(
					requested=requested_profile_id,
					status="invalid",
				)
			)
		return _BenchmarkCollectedProfiles(
			resolution=_BenchmarkProfileResolution(
				requested="auto",
				status="unresolved",
			)
		)


class QualityBenchmark:
	"""Synthetic corpus plus real QueryService used by the quality gate."""

	def __init__(self) -> None:
		self._tmpdir = TemporaryDirectory(prefix="active-kb-quality-")
		self._root = Path(self._tmpdir.name)
		self._config = _resolve_model(self._root)
		self._adapter = _build_indexed_fixture(self._root, self._config)
		self._router = QueryRouter.from_config(
			self._config,
			cwd=self._root,
			profile_collector=_BenchmarkProfileCollector(),
		)
		self._query_service = QueryService(
			self._config,
			router=self._router,
			metadata_adapter=self._adapter,
			symbol_retriever=SymbolRetriever.from_storage(self._adapter),
			fulltext_retriever=FullTextRetriever.from_storage(self._adapter),
			graph_retriever=GraphRetriever.from_config(
				self._config,
				metadata_adapter=self._adapter,
			),
		)

	def route(self, request: QueryRequest):
		return self._router.route(request)

	def search(self, case: EvalCase) -> QueryResult:
		return self._query_service.search(
			QueryRequest.model_validate(case.request.to_dict())
		)

	def blocked_security_probe(self) -> QueryResult:
		blocked_config = self._config.model_copy(deep=True)
		blocked_config.deployment_mode = "remote_shared"
		blocked_config.server.transport = "streamable-http"
		# Keep the host loopback so the fail-safe probe yields unique warning codes.
		blocked_config.server.http.host = "127.0.0.1"
		blocked_config.server.http.require_auth = False
		blocked_config.server.http.allowed_origins = ["*"]
		blocked_config.security.audit.enabled = False
		probe = validate_startup_security(blocked_config)
		return QueryResult.model_validate(probe.to_blocked_response())

	def close(self) -> None:
		self._tmpdir.cleanup()


def _resolve_model(root: Path, overrides: ConfigDict | None = None) -> ActiveKnowledgeConfig:
	workspace = root / "workspace"
	docs = root / "knowledge-sources"
	workspace.mkdir(parents=True, exist_ok=True)
	docs.mkdir(parents=True, exist_ok=True)
	merged: ConfigDict = {
		"runtime": {
			"workdir": str(root / ".active-kb"),
			"baseline_dir": str(root / ".active-kb" / "baseline"),
			"local_dir": str(root / ".active-kb" / "local"),
			"source_docs_root": str(docs),
		},
		"project": {
			"id": "quality-benchmark",
			"display_name": "Quality Benchmark",
			"workspace_root": str(workspace),
			"default_profile": "auto",
		},
		"storage": {
			"baseline": {
				"manifest": str(root / ".active-kb" / "baseline" / "manifest.json"),
			},
			"metadata": {
				"path": str(root / ".active-kb" / "baseline" / "db" / "metadata.db"),
				"mode": "readwrite",
			},
			"overlay": {
				"path": str(root / ".active-kb" / "local" / "db" / "overlay.db"),
				"mode": "readwrite",
			},
			"jobs": {
				"path": str(root / ".active-kb" / "local" / "db" / "jobs.db"),
				"mode": "readwrite",
			},
			"vector": {
				"path": str(root / ".active-kb" / "baseline" / "vectors"),
				"mode": "readwrite",
			},
			"vector_delta": {
				"path": str(root / ".active-kb" / "local" / "vectors"),
				"mode": "readwrite",
			},
			"cache_root": str(root / ".active-kb" / "local" / "cache"),
		},
	}
	if overrides:
		merged = _deep_merge(merged, overrides)
	return resolve_config(cli_overrides=merged, env={}, cwd=root).model


def _build_adapter(config: ActiveKnowledgeConfig) -> SQLiteStorageAdapter:
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


def _build_indexed_fixture(root: Path, config: ActiveKnowledgeConfig) -> SQLiteStorageAdapter:
	workspace_root = Path(config.project.workspace_root)
	docs_root = Path(config.runtime.source_docs_root)
	_seed_workspace(workspace_root)
	_seed_docs(docs_root)
	adapter = _build_adapter(config)
	writer = adapter.writer(StorageWriteRequest(target="overlay"))
	CodeIndexer.from_config(config, cwd=root).collect_and_store(
		writer,
		snapshot_id=CURRENT_SNAPSHOT_ID,
	)
	DocumentIndexer.from_config(config, cwd=root).collect_and_store(
		writer,
		snapshot_id=CURRENT_SNAPSHOT_ID,
	)
	_seed_profile_specific_entities(adapter)
	return adapter


def _seed_workspace(workspace_root: Path) -> None:
	component_dir = workspace_root / "components" / "health"
	sensor_dir = workspace_root / "components" / "sensor"
	ui_dir = workspace_root / "ui" / "widgets"
	component_dir.mkdir(parents=True, exist_ok=True)
	sensor_dir.mkdir(parents=True, exist_ok=True)
	ui_dir.mkdir(parents=True, exist_ok=True)
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

int health_service_publish_event(void)
{
    return HEALTH_DEFAULT;
}

int health_init(void)
{
    return health_service_publish_event() + ERR_HEALTH_TIMEOUT;
}
""",
		encoding="utf-8",
	)
	(component_dir / "health.h").write_text(
		"""#ifndef HEALTH_H
#define HEALTH_H

int health_service_publish_event(void);
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
	(sensor_dir / "module.mk").write_text(
		"""NAME = sensor
MODULE = sensor.api
SENSOR_SOURCES = sensor.c sensor.h
""",
		encoding="utf-8",
	)
	(sensor_dir / "sensor.c").write_text(
		"""#include "sensor.h"

int sensor_open(void)
{
    return 1;
}

int sensor_close(void)
{
    return 0;
}
""",
		encoding="utf-8",
	)
	(sensor_dir / "sensor.h").write_text(
		"""#ifndef SENSOR_H
#define SENSOR_H

int sensor_open(void);
int sensor_close(void);

#endif
""",
		encoding="utf-8",
	)
	(ui_dir / "heart_tile.c").write_text(
		"""int heart_tile_render(void)
{
    return 0;
}
""",
		encoding="utf-8",
	)


def _seed_docs(docs_root: Path) -> None:
	(docs_root / "api").mkdir(parents=True, exist_ok=True)
	(docs_root / "widgets").mkdir(parents=True, exist_ok=True)
	(docs_root / "engineering").mkdir(parents=True, exist_ok=True)
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
	(docs_root / "engineering" / "health_boot.md").write_text(
		"""---
title: Health Boot Notes
authority_level: official
code_symbols:
  - health_init
tags:
  - boot
  - entrypoint
---
# Boot Entry Point

The boot entrypoint initializes the health runtime before tasks start.
""",
		encoding="utf-8",
	)


def _seed_profile_specific_entities(adapter: SQLiteStorageAdapter) -> None:
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
	watch_evidence = EvidenceRecord(
		evidence_id="evidence:profile_only:watch",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		object_type="entity",
		object_id=watch_entity.entity_id,
		file_id=watch_file.file_id,
		source_scope="components",
		profile_id="watch",
		excerpt="watch-only symbol",
		citation_label="profiles/watch_only.c:1",
		start_line=1,
		end_line=3,
		metadata={"path": watch_file.relative_path},
	)
	sensorhub_evidence = EvidenceRecord(
		evidence_id="evidence:profile_only:sensorhub",
		snapshot_id=CURRENT_SNAPSHOT_ID,
		object_type="entity",
		object_id=sensorhub_entity.entity_id,
		file_id=sensorhub_file.file_id,
		source_scope="components",
		profile_id="sensorhub",
		excerpt="sensorhub-only symbol",
		citation_label="profiles/sensorhub_only.c:1",
		start_line=1,
		end_line=3,
		metadata={"path": sensorhub_file.relative_path},
	)
	for record in (
		watch_file,
		sensorhub_file,
		watch_entity,
		sensorhub_entity,
		watch_evidence,
		sensorhub_evidence,
	):
		if isinstance(record, FileRecord):
			writer.upsert_file(record)
		elif isinstance(record, EntityRecord):
			writer.upsert_entity(record)
		else:
			writer.upsert_evidence(record)
	writer.flush()


def _deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
	merged = dict(base)
	for key, value in overrides.items():
		if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
			nested = merged[key]
			assert isinstance(nested, dict)
			merged[key] = _deep_merge(nested, value)
			continue
		merged[key] = value
	return merged