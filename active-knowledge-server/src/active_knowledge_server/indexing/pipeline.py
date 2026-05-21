"""Incremental indexing pipeline and stateful diff orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, Literal, cast

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.source_docs import (
    SourceDocsConnector,
    SourceDocsManifest,
)
from active_knowledge_server.connectors.workspace import WorkspaceConnector, WorkspaceInventory
from active_knowledge_server.indexing.code_indexer import (
    CODE_INDEXER_SCHEMA_VERSION,
    CodeIndexer,
    IndexedCode,
)
from active_knowledge_server.indexing.doc_indexer import (
    DOC_INDEXER_SCHEMA_VERSION,
    DocumentIndexer,
    IndexedDocuments,
)
from active_knowledge_server.indexing.profile import (
    PROFILE_COLLECTOR_SCHEMA_VERSION,
    CollectedProfiles,
    ProfileCollector,
)
from active_knowledge_server.indexing.relation_extractor import (
    PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION,
    ProfileConditionedRelationExtractor,
    profile_config_hash,
)
from active_knowledge_server.indexing.snapshot import CURRENT_SNAPSHOT_ID
from active_knowledge_server.indexing.workspace_map import WorkspaceMapBuilder
from active_knowledge_server.storage import (
    ALL_SCOPE,
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    QueryScope,
    RelationRecord,
    StorageAdapter,
    StorageObjectType,
    StorageSourceIndex,
    StorageWriteRequest,
    TombstoneRecord,
    VectorRefRecord,
    make_tombstone_id,
)
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.sqlite_store import SQLiteStorageAdapter, utc_now

INCREMENTAL_INDEX_STATE_SCHEMA_VERSION: Final = "incremental_index_state.v1"
INCREMENTAL_INDEX_RESULT_SCHEMA_VERSION: Final = "incremental_index_result.v1"
_PROFILE_RELATION_TYPES: Final = {"enabled_by", "disabled_by", "unknown_by"}
_CODE_SOURCE_ID: Final = "workspace"


@dataclass(frozen=True)
class IncrementalIndexWarning:
    """One structured warning emitted during incremental planning or execution."""

    code: str
    message: str
    level: str = "degraded"
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable warning payload."""

        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class IncrementalIndexState:
    """Persisted incremental state used to plan subsequent overlay-only updates."""

    schema_version: str
    snapshot_id: str
    code_indexer_schema_version: str
    doc_indexer_schema_version: str
    profile_collector_schema_version: str
    profile_conditioned_relation_schema_version: str
    embedding_model_version: str
    embeddings_enabled: bool
    workspace_inventory_hash: str
    source_docs_manifest_hash: str
    code_files: Mapping[str, str]
    doc_files: Mapping[str, str]
    profile_config_hashes: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-serializable state payload."""

        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "code_indexer_schema_version": self.code_indexer_schema_version,
            "doc_indexer_schema_version": self.doc_indexer_schema_version,
            "profile_collector_schema_version": self.profile_collector_schema_version,
            "profile_conditioned_relation_schema_version": (
                self.profile_conditioned_relation_schema_version
            ),
            "embedding_model_version": self.embedding_model_version,
            "embeddings_enabled": self.embeddings_enabled,
            "workspace_inventory_hash": self.workspace_inventory_hash,
            "source_docs_manifest_hash": self.source_docs_manifest_hash,
            "code_files": dict(sorted(self.code_files.items())),
            "doc_files": dict(sorted(self.doc_files.items())),
            "profile_config_hashes": dict(sorted(self.profile_config_hashes.items())),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> IncrementalIndexState:
        """Decode one persisted state payload."""

        return cls(
            schema_version=str(
                payload.get("schema_version", INCREMENTAL_INDEX_STATE_SCHEMA_VERSION)
            ),
            snapshot_id=str(payload.get("snapshot_id", CURRENT_SNAPSHOT_ID)),
            code_indexer_schema_version=str(payload.get("code_indexer_schema_version", "")),
            doc_indexer_schema_version=str(payload.get("doc_indexer_schema_version", "")),
            profile_collector_schema_version=str(
                payload.get("profile_collector_schema_version", "")
            ),
            profile_conditioned_relation_schema_version=str(
                payload.get("profile_conditioned_relation_schema_version", "")
            ),
            embedding_model_version=str(payload.get("embedding_model_version", "")),
            embeddings_enabled=bool(payload.get("embeddings_enabled", True)),
            workspace_inventory_hash=str(payload.get("workspace_inventory_hash", "")),
            source_docs_manifest_hash=str(payload.get("source_docs_manifest_hash", "")),
            code_files=_decode_str_map(payload.get("code_files")),
            doc_files=_decode_str_map(payload.get("doc_files")),
            profile_config_hashes=_decode_str_map(payload.get("profile_config_hashes")),
        )


@dataclass(frozen=True)
class IncrementalIndexPlan:
    """One fully expanded incremental update plan."""

    snapshot_id: str
    source: Literal["all", "code", "docs"]
    previous_state: IncrementalIndexState | None
    current_state: IncrementalIndexState
    workspace_inventory: WorkspaceInventory
    source_docs_manifest: SourceDocsManifest
    collected_profiles: CollectedProfiles
    reindex_all_code: bool = False
    reindex_all_docs: bool = False
    rebuild_vectors: bool = False
    rebuild_profile_conditioned_relations: bool = False
    changed_code_paths: tuple[str, ...] = ()
    deleted_code_paths: tuple[str, ...] = ()
    changed_doc_paths: tuple[str, ...] = ()
    deleted_doc_paths: tuple[str, ...] = ()
    changed_profile_ids: tuple[str, ...] = ()
    removed_profile_ids: tuple[str, ...] = ()
    warnings: tuple[IncrementalIndexWarning, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable planning summary."""

        return {
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "reindex_all_code": self.reindex_all_code,
            "reindex_all_docs": self.reindex_all_docs,
            "rebuild_vectors": self.rebuild_vectors,
            "rebuild_profile_conditioned_relations": self.rebuild_profile_conditioned_relations,
            "changed_code_paths": list(self.changed_code_paths),
            "deleted_code_paths": list(self.deleted_code_paths),
            "changed_doc_paths": list(self.changed_doc_paths),
            "deleted_doc_paths": list(self.deleted_doc_paths),
            "changed_profile_ids": list(self.changed_profile_ids),
            "removed_profile_ids": list(self.removed_profile_ids),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(frozen=True)
class IncrementalIndexResult:
    """One incremental execution result with plan, warnings, and outcome."""

    schema_version: str
    snapshot_id: str
    result_status: Literal["ready", "partial_ready"]
    plan: IncrementalIndexPlan
    warnings: tuple[IncrementalIndexWarning, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable execution result."""

        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "result_status": self.result_status,
            "plan": self.plan.to_dict(),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(frozen=True)
class _ObjectBundle:
    file_record: FileRecord
    chunks: tuple[ChunkRecord, ...]
    entities: tuple[EntityRecord, ...]
    relations: tuple[RelationRecord, ...]
    evidence: tuple[EvidenceRecord, ...]
    vectors: tuple[VectorRefRecord, ...] = ()


@dataclass(frozen=True)
class _LiveObject:
    logical_object_id: str
    source_index: StorageSourceIndex
    record: ChunkRecord | EntityRecord | EvidenceRecord | RelationRecord


class IncrementalIndexPipeline:
    """Coordinate overlay-only incremental index writes using current manifests and state."""

    def __init__(
        self,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
        metadata_adapter: StorageAdapter | None = None,
        vector_adapter: LanceDBVectorAdapter | None = None,
        workspace_connector: WorkspaceConnector | None = None,
        source_docs_connector: SourceDocsConnector | None = None,
        code_indexer: CodeIndexer | None = None,
        doc_indexer: DocumentIndexer | None = None,
        profile_collector: ProfileCollector | None = None,
        profile_relation_extractor: ProfileConditionedRelationExtractor | None = None,
        workspace_map_builder: WorkspaceMapBuilder | None = None,
    ) -> None:
        self._config = config
        self._cwd = (cwd or Path.cwd()).expanduser()
        self._metadata_adapter = metadata_adapter or SQLiteStorageAdapter.from_config(
            config,
            cwd=self._cwd,
        )
        self._vector_adapter = vector_adapter or LanceDBVectorAdapter.from_config(
            config,
            cwd=self._cwd,
            metadata_adapter=self._metadata_adapter,
        )
        self._workspace_connector = workspace_connector or WorkspaceConnector.from_config(
            config,
            cwd=self._cwd,
        )
        self._source_docs_connector = source_docs_connector or SourceDocsConnector.from_config(
            config,
            cwd=self._cwd,
        )
        self._code_indexer = code_indexer or CodeIndexer.from_config(config, cwd=self._cwd)
        self._doc_indexer = doc_indexer or DocumentIndexer.from_config(config, cwd=self._cwd)
        self._profile_collector = profile_collector or ProfileCollector.from_config(
            config,
            cwd=self._cwd,
        )
        self._profile_relation_extractor = (
            profile_relation_extractor or ProfileConditionedRelationExtractor()
        )
        self._workspace_map_builder = workspace_map_builder or WorkspaceMapBuilder.from_config(
            config,
            cwd=self._cwd,
        )

    @property
    def state_path(self) -> Path:
        """Return the persisted incremental state path under local artifacts."""

        root = resolve_runtime_path(self._config.storage.local_artifacts_root, self._cwd)
        return root / "index_state.json"

    def load_state(self) -> IncrementalIndexState | None:
        """Load the last successful incremental state when present."""

        path = self.state_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return IncrementalIndexState.from_dict(payload)

    def save_state(self, state: IncrementalIndexState) -> Path:
        """Persist the latest successful incremental state."""

        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def capture_state(
        self,
        *,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
    ) -> tuple[IncrementalIndexState, WorkspaceInventory, SourceDocsManifest, CollectedProfiles]:
        """Scan current manifests and return the derived incremental state."""

        workspace_inventory = self._workspace_connector.scan()
        source_docs_manifest = self._source_docs_connector.scan()
        collected_profiles = self._profile_collector.collect(snapshot_id=snapshot_id)
        state = IncrementalIndexState(
            schema_version=INCREMENTAL_INDEX_STATE_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            code_indexer_schema_version=CODE_INDEXER_SCHEMA_VERSION,
            doc_indexer_schema_version=DOC_INDEXER_SCHEMA_VERSION,
            profile_collector_schema_version=PROFILE_COLLECTOR_SCHEMA_VERSION,
            profile_conditioned_relation_schema_version=(
                PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION
            ),
            embedding_model_version=self._config.indexing.embeddings.model,
            embeddings_enabled=self._config.indexing.embeddings.enabled,
            workspace_inventory_hash=workspace_inventory.inventory_hash,
            source_docs_manifest_hash=source_docs_manifest.manifest_hash,
            code_files={
                entry.relative_path: entry.content_hash or ""
                for entry in workspace_inventory.files
            },
            doc_files={
                entry.relative_path: entry.content_hash or ""
                for entry in source_docs_manifest.files
            },
            profile_config_hashes={
                record.profile_id: profile_config_hash(record) or ""
                for record in collected_profiles.profile_records
            },
        )
        return state, workspace_inventory, source_docs_manifest, collected_profiles

    def plan(
        self,
        *,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
        source: Literal["all", "code", "docs"] = "all",
    ) -> IncrementalIndexPlan:
        """Plan one incremental update from persisted state and current manifests."""

        previous = self.load_state()
        current, workspace_inventory, source_docs_manifest, collected_profiles = self.capture_state(
            snapshot_id=snapshot_id
        )

        code_schema_changed = (
            previous is None
            or previous.code_indexer_schema_version != current.code_indexer_schema_version
        )
        doc_schema_changed = (
            previous is None
            or previous.doc_indexer_schema_version != current.doc_indexer_schema_version
        )
        profile_schema_changed = (
            previous is None
            or previous.profile_collector_schema_version
            != current.profile_collector_schema_version
            or previous.profile_conditioned_relation_schema_version
            != current.profile_conditioned_relation_schema_version
        )
        embedding_changed = previous is None or (
            previous.embedding_model_version != current.embedding_model_version
            or previous.embeddings_enabled != current.embeddings_enabled
        )

        changed_code_paths, deleted_code_paths = _diff_maps(
            {} if previous is None else previous.code_files,
            current.code_files,
        )
        changed_doc_paths, deleted_doc_paths = _diff_maps(
            {} if previous is None else previous.doc_files,
            current.doc_files,
        )
        changed_profile_ids, removed_profile_ids = _diff_maps(
            {} if previous is None else previous.profile_config_hashes,
            current.profile_config_hashes,
        )

        warnings: list[IncrementalIndexWarning] = []
        if code_schema_changed and previous is not None:
            warnings.append(
                IncrementalIndexWarning(
                    code="index.code_schema_changed",
                    message=(
                        "Code parser/extractor version changed; all code files "
                        "will be rebuilt."
                    ),
                    level="caution",
                    details={
                        "previous": previous.code_indexer_schema_version,
                        "current": current.code_indexer_schema_version,
                    },
                )
            )
        if doc_schema_changed and previous is not None:
            warnings.append(
                IncrementalIndexWarning(
                    code="index.doc_schema_changed",
                    message=(
                        "Document parser/extractor version changed; all source documents "
                        "will be rebuilt."
                    ),
                    level="caution",
                    details={
                        "previous": previous.doc_indexer_schema_version,
                        "current": current.doc_indexer_schema_version,
                    },
                )
            )
        if embedding_changed and previous is not None:
            warnings.append(
                IncrementalIndexWarning(
                    code="index.embedding_model_changed",
                    message="Embedding model changed; all document vectors will be rebuilt.",
                    level="caution",
                    details={
                        "previous": previous.embedding_model_version,
                        "current": current.embedding_model_version,
                    },
                )
            )

        reindex_all_code = source in {"all", "code"} and code_schema_changed
        reindex_all_docs = source in {"all", "docs"} and doc_schema_changed
        rebuild_vectors = (
            source in {"all", "docs"}
            and current.embeddings_enabled
            and (embedding_changed or reindex_all_docs)
        )
        rebuild_profile_conditioned_relations = source in {"all", "code"} and (
            profile_schema_changed or bool(changed_profile_ids) or bool(removed_profile_ids)
        )

        return IncrementalIndexPlan(
            snapshot_id=snapshot_id,
            source=source,
            previous_state=previous,
            current_state=current,
            workspace_inventory=workspace_inventory,
            source_docs_manifest=source_docs_manifest,
            collected_profiles=collected_profiles,
            reindex_all_code=reindex_all_code,
            reindex_all_docs=reindex_all_docs,
            rebuild_vectors=rebuild_vectors,
            rebuild_profile_conditioned_relations=rebuild_profile_conditioned_relations,
            changed_code_paths=(
                tuple(sorted(current.code_files))
                if reindex_all_code
                else tuple(sorted(changed_code_paths))
            ),
            deleted_code_paths=tuple(sorted(deleted_code_paths)),
            changed_doc_paths=(
                tuple(sorted(current.doc_files))
                if reindex_all_docs
                else tuple(sorted(changed_doc_paths))
            ),
            deleted_doc_paths=tuple(sorted(deleted_doc_paths)),
            changed_profile_ids=tuple(sorted(changed_profile_ids)),
            removed_profile_ids=tuple(sorted(removed_profile_ids)),
            warnings=tuple(warnings),
        )

    def run(
        self,
        *,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
        source: Literal["all", "code", "docs"] = "all",
    ) -> IncrementalIndexResult:
        """Execute one incremental overlay update and persist the next incremental state."""

        plan = self.plan(snapshot_id=snapshot_id, source=source)
        reader = self._metadata_adapter.reader()
        writer = self._metadata_adapter.writer(StorageWriteRequest(target="overlay"))
        vector_writer = self._vector_adapter.writer(StorageWriteRequest(target="overlay"))

        warnings = list(plan.warnings)
        failed = False

        code_indexed: IndexedCode | None = None
        if source in {"all", "code"} and (
            plan.changed_code_paths or plan.deleted_code_paths or plan.reindex_all_code
        ):
            try:
                code_indexed = self._code_indexer.collect(
                    snapshot_id=snapshot_id,
                    workspace_inventory=plan.workspace_inventory,
                )
                new_code_bundles = _code_bundles_by_path(code_indexed)
                for relative_path in plan.deleted_code_paths:
                    self._tombstone_deleted_path(
                        reader,
                        writer,
                        relative_path=relative_path,
                        snapshot_id=snapshot_id,
                        reason="incremental_delete",
                    )
                for relative_path in plan.changed_code_paths:
                    bundle = new_code_bundles.get(relative_path)
                    if bundle is None:
                        continue
                    self._apply_code_bundle(
                        reader,
                        writer,
                        relative_path=relative_path,
                        new_bundle=bundle,
                        snapshot_id=snapshot_id,
                    )
                writer.flush()
            except Exception as exc:  # noqa: BLE001 - surface as degraded incremental failure.
                failed = True
                warnings.append(
                    IncrementalIndexWarning(
                        code="index.code_incremental_failed",
                        message=(
                            "Code incremental indexing failed; keeping previous "
                            "live code state."
                        ),
                        details={"error": str(exc)},
                    )
                )

        if source in {"all", "docs"} and (
            plan.changed_doc_paths
            or plan.deleted_doc_paths
            or plan.reindex_all_docs
            or plan.rebuild_vectors
        ):
            for relative_path in plan.deleted_doc_paths:
                try:
                    self._tombstone_deleted_path(
                        reader,
                        writer,
                        relative_path=_doc_storage_relative_path(
                            plan.source_docs_manifest,
                            relative_path,
                        ),
                        snapshot_id=snapshot_id,
                        reason="incremental_delete",
                    )
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    warnings.append(
                        IncrementalIndexWarning(
                            code="index.doc_delete_failed",
                            message=(
                                "A deleted source document could not be tombstoned during "
                                "incremental indexing."
                            ),
                            details={"path": relative_path, "error": str(exc)},
                        )
                    )

            doc_paths_to_collect = set(plan.changed_doc_paths)
            if plan.reindex_all_docs or (plan.rebuild_vectors and not doc_paths_to_collect):
                doc_paths_to_collect.update(plan.current_state.doc_files)

            for relative_path in sorted(doc_paths_to_collect):
                try:
                    storage_relative_path = _doc_storage_relative_path(
                        plan.source_docs_manifest,
                        relative_path,
                    )
                    manifest = _filter_source_docs_manifest(
                        plan.source_docs_manifest,
                        include_paths=(relative_path,),
                    )
                    indexed = self._doc_indexer.collect(
                        snapshot_id=snapshot_id,
                        source_docs_manifest=manifest,
                    )
                    new_bundle = _doc_bundle_from_indexed(indexed, storage_relative_path)
                    if new_bundle is None:
                        continue
                    metadata_changed = (
                        plan.reindex_all_docs or relative_path in plan.changed_doc_paths
                    )
                    if metadata_changed:
                        self._apply_doc_bundle(
                            reader,
                            writer,
                            relative_path=storage_relative_path,
                            new_bundle=new_bundle,
                            snapshot_id=snapshot_id,
                        )
                    if plan.rebuild_vectors or metadata_changed:
                        self._upsert_doc_vectors(
                            vector_writer,
                            indexed,
                            relative_path=storage_relative_path,
                        )
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    warnings.append(
                        IncrementalIndexWarning(
                            code="index.doc_incremental_failed",
                            message="A source document failed during incremental indexing.",
                            details={"path": relative_path, "error": str(exc)},
                        )
                    )
            writer.flush()
            vector_writer.flush()

        if source in {"all", "code"} and plan.rebuild_profile_conditioned_relations:
            try:
                self._rebuild_profile_conditioned_relations(
                    reader=reader,
                    writer=writer,
                    snapshot_id=snapshot_id,
                    collected_profiles=plan.collected_profiles,
                    changed_profile_ids=set(plan.changed_profile_ids),
                    removed_profile_ids=set(plan.removed_profile_ids),
                )
                writer.flush()
            except Exception as exc:  # noqa: BLE001
                failed = True
                warnings.append(
                    IncrementalIndexWarning(
                        code="index.profile_incremental_failed",
                        message=(
                            "Profile-conditioned relations failed to rebuild; previous profile "
                            "projection remains live."
                        ),
                        details={"error": str(exc)},
                    )
                )

        if _workspace_map_refresh_required(plan):
            try:
                self._workspace_map_builder.collect_and_write(
                    snapshot_id=snapshot_id,
                    workspace_inventory=plan.workspace_inventory,
                    reader=self._metadata_adapter.reader(),
                    profiles=plan.collected_profiles.profile_records,
                    profile_resolution=plan.collected_profiles.resolution.to_dict(),
                )
            except Exception as exc:  # noqa: BLE001
                failed = True
                warnings.append(
                    IncrementalIndexWarning(
                        code="index.workspace_map_failed",
                        message=(
                            "Workspace map projection failed to refresh; previous "
                            "workspace navigation artifact remains live."
                        ),
                        details={"error": str(exc)},
                    )
                )

        if not failed:
            self.save_state(plan.current_state)

        return IncrementalIndexResult(
            schema_version=INCREMENTAL_INDEX_RESULT_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            result_status="partial_ready" if failed else "ready",
            plan=plan,
            warnings=tuple(warnings),
        )

    def _apply_code_bundle(
        self,
        reader: object,
        writer: object,
        *,
        relative_path: str,
        new_bundle: _ObjectBundle,
        snapshot_id: str,
    ) -> None:
        old_bundle = _load_live_bundle(reader, relative_path=relative_path, snapshot_id=snapshot_id)
        if old_bundle is not None:
            self._diff_and_mark_stale(
                writer,
                old_bundle=old_bundle,
                new_bundle=new_bundle,
                snapshot_id=snapshot_id,
                reason="incremental_replace",
            )
        writer.upsert_file(new_bundle.file_record)
        for record in new_bundle.chunks:
            writer.upsert_chunk(record)
        for record in new_bundle.entities:
            writer.upsert_entity(record)
        for record in new_bundle.relations:
            writer.upsert_relation(record)
        for record in new_bundle.evidence:
            writer.upsert_evidence(record)

    def _apply_doc_bundle(
        self,
        reader: object,
        writer: object,
        *,
        relative_path: str,
        new_bundle: _ObjectBundle,
        snapshot_id: str,
    ) -> None:
        old_bundle = _load_live_bundle(reader, relative_path=relative_path, snapshot_id=snapshot_id)
        if old_bundle is not None:
            self._diff_and_mark_stale(
                writer,
                old_bundle=old_bundle,
                new_bundle=new_bundle,
                snapshot_id=snapshot_id,
                reason="incremental_replace",
            )
        writer.upsert_file(new_bundle.file_record)
        for record in new_bundle.chunks:
            writer.upsert_chunk(record)
        for record in new_bundle.entities:
            writer.upsert_entity(record)
        for record in new_bundle.evidence:
            writer.upsert_evidence(record)

    def _upsert_doc_vectors(
        self,
        vector_writer: object,
        indexed: IndexedDocuments,
        *,
        relative_path: str,
    ) -> None:
        for write in indexed.vector_writes:
            chunk = next(
                (
                    record
                    for record in indexed.chunk_records
                    if record.chunk_id == write.record.object_id
                    and _chunk_relative_path(record) == relative_path
                ),
                None,
            )
            if chunk is None:
                continue
            vector_writer.upsert_vector(write.record, write.embedding)

    def _tombstone_deleted_path(
        self,
        reader: object,
        writer: object,
        *,
        relative_path: str,
        snapshot_id: str,
        reason: str,
    ) -> None:
        bundle = _load_live_bundle(reader, relative_path=relative_path, snapshot_id=snapshot_id)
        if bundle is None:
            return
        writer.tombstone_file(
            bundle.file_record.file_id,
            scope=QueryScope(snapshot_id=snapshot_id),
            reason=reason,
            created_by_job="job:incremental_index",
        )

    def _rebuild_profile_conditioned_relations(
        self,
        *,
        reader: object,
        writer: object,
        snapshot_id: str,
        collected_profiles: CollectedProfiles,
        changed_profile_ids: set[str],
        removed_profile_ids: set[str],
    ) -> None:
        profiles = collected_profiles.profile_records
        for record in profiles:
            writer.upsert_profile(record)

        live_entities = tuple(
            item.record
            for item in reader.logical_entities(QueryScope(snapshot_id=snapshot_id))
            if item.record.profile_id == ALL_SCOPE
        )
        live_relations = tuple(
            item.record
            for item in reader.logical_relations(QueryScope(snapshot_id=snapshot_id))
            if item.record.profile_id == ALL_SCOPE
        )
        indexed = self._profile_relation_extractor.collect(
            snapshot_id=snapshot_id,
            profiles=profiles,
            entities=live_entities,
            relations=live_relations,
        )
        target_profile_ids = changed_profile_ids | removed_profile_ids
        if not target_profile_ids:
            target_profile_ids = {record.profile_id for record in profiles}

        old_relations = {
            item.logical_object_id: item.record
            for item in reader.logical_relations(QueryScope(snapshot_id=snapshot_id))
            if item.record.profile_id in target_profile_ids
            and item.record.relation_type in _PROFILE_RELATION_TYPES
        }
        new_relations = {
            record.relation_id: record
            for record in indexed.relation_records
            if record.profile_id in target_profile_ids
        }
        for relation_id, relation in old_relations.items():
            if relation_id in new_relations:
                continue
            self._tombstone_object(
                writer,
                object_type="relation",
                object_id=relation.relation_id,
                scope=QueryScope(
                    snapshot_id=relation.snapshot_id,
                    profile_id=relation.profile_id,
                    source_scope=relation.source_scope,
                ),
                reason="profile_rebuild",
                baseline_id=relation.relation_id,
            )
        for record in new_relations.values():
            writer.upsert_relation(record)

    def _diff_and_mark_stale(
        self,
        writer: object,
        *,
        old_bundle: _ObjectBundle,
        new_bundle: _ObjectBundle,
        snapshot_id: str,
        reason: str,
    ) -> None:
        chunk_replacements = _matched_chunk_replacements(old_bundle.chunks, new_bundle.chunks)
        entity_replacements = _matched_entity_replacements(old_bundle.entities, new_bundle.entities)
        relation_replacements = _matched_relation_replacements(
            old_bundle.relations,
            new_bundle.relations,
            entity_replacements=entity_replacements,
        )
        new_chunk_ids = {record.chunk_id for record in new_bundle.chunks}
        new_entity_ids = {record.entity_id for record in new_bundle.entities}
        new_relation_ids = {record.relation_id for record in new_bundle.relations}

        for old_chunk in old_bundle.chunks:
            replacement_id = chunk_replacements.get(old_chunk.chunk_id)
            if replacement_id is not None and replacement_id != old_chunk.chunk_id:
                writer.replace_object(
                    "chunk",
                    old_chunk.chunk_id,
                    replacement_id,
                    scope=QueryScope(
                        snapshot_id=snapshot_id,
                        profile_id=old_chunk.profile_id,
                        source_scope=old_chunk.source_scope,
                    ),
                    reason=reason,
                    created_by_job="job:incremental_index",
                    baseline_id=old_chunk.chunk_id,
                )
            if old_chunk.chunk_id not in new_chunk_ids:
                writer.tombstone_chunk(
                    old_chunk.chunk_id,
                    scope=QueryScope(
                        snapshot_id=snapshot_id,
                        profile_id=old_chunk.profile_id,
                        source_scope=old_chunk.source_scope,
                    ),
                    reason=reason,
                    created_by_job="job:incremental_index",
                )

        for old_entity in old_bundle.entities:
            replacement_id = entity_replacements.get(old_entity.entity_id)
            if replacement_id is not None and replacement_id != old_entity.entity_id:
                writer.replace_object(
                    "entity",
                    old_entity.entity_id,
                    replacement_id,
                    scope=QueryScope(
                        snapshot_id=snapshot_id,
                        profile_id=old_entity.profile_id,
                        source_scope=old_entity.source_scope,
                    ),
                    reason=reason,
                    created_by_job="job:incremental_index",
                    baseline_id=old_entity.entity_id,
                )
            if old_entity.entity_id not in new_entity_ids:
                self._tombstone_object(
                    writer,
                    object_type="entity",
                    object_id=old_entity.entity_id,
                    scope=QueryScope(
                        snapshot_id=snapshot_id,
                        profile_id=old_entity.profile_id,
                        source_scope=old_entity.source_scope,
                    ),
                    reason=reason,
                    baseline_id=old_entity.entity_id,
                )

        for old_relation in old_bundle.relations:
            replacement_id = relation_replacements.get(old_relation.relation_id)
            if replacement_id is not None and replacement_id != old_relation.relation_id:
                writer.replace_object(
                    "relation",
                    old_relation.relation_id,
                    replacement_id,
                    scope=QueryScope(
                        snapshot_id=snapshot_id,
                        profile_id=old_relation.profile_id,
                        source_scope=old_relation.source_scope,
                    ),
                    reason=reason,
                    created_by_job="job:incremental_index",
                    baseline_id=old_relation.relation_id,
                )
            if old_relation.relation_id not in new_relation_ids:
                self._tombstone_object(
                    writer,
                    object_type="relation",
                    object_id=old_relation.relation_id,
                    scope=QueryScope(
                        snapshot_id=snapshot_id,
                        profile_id=old_relation.profile_id,
                        source_scope=old_relation.source_scope,
                    ),
                    reason=reason,
                    baseline_id=old_relation.relation_id,
                )

    def _tombstone_object(
        self,
        writer: object,
        *,
        object_type: str,
        object_id: str,
        scope: QueryScope,
        reason: str,
        baseline_id: str | None = None,
    ) -> None:
        writer.upsert_tombstone(
            TombstoneRecord(
                tombstone_id=make_tombstone_id(
                    cast(StorageObjectType, object_type),
                    object_id,
                    scope=scope,
                    reason=reason,
                ),
                object_type=cast(StorageObjectType, object_type),
                object_id=object_id,
                reason=reason,
                created_by_job="job:incremental_index",
                snapshot_id=scope.snapshot_id,
                profile_id=scope.profile_id,
                source_scope=scope.source_scope,
                baseline_id=baseline_id,
                created_at=utc_now(),
            )
        )


def _decode_str_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and item is not None
    }


def _diff_maps(
    previous: Mapping[str, str],
    current: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    changed = tuple(
        sorted(
            path for path, value in current.items() if previous.get(path) != value
        )
    )
    deleted = tuple(sorted(path for path in previous if path not in current))
    return changed, deleted


def _workspace_map_refresh_required(plan: IncrementalIndexPlan) -> bool:
    return bool(
        plan.source in {"all", "code"}
        and (
            plan.reindex_all_code
            or plan.changed_code_paths
            or plan.deleted_code_paths
            or plan.rebuild_profile_conditioned_relations
            or plan.changed_profile_ids
            or plan.removed_profile_ids
        )
    )


def _filter_source_docs_manifest(
    manifest: SourceDocsManifest,
    *,
    include_paths: Sequence[str],
) -> SourceDocsManifest:
    include = set(include_paths)
    files = tuple(entry for entry in manifest.files if entry.relative_path in include)
    counts: dict[str, int] = {}
    for entry in files:
        counts[entry.category] = counts.get(entry.category, 0) + 1
    categories = tuple(
        replace(
            category,
            file_count=counts.get(category.name, 0),
            exists=category.exists or counts.get(category.name, 0) > 0,
        )
        for category in manifest.categories
        if counts.get(category.name, 0) > 0
    )
    payload = {
        "schema_version": manifest.schema_version,
        "source_docs_root": manifest.source_docs_root,
        "supported_categories": list(manifest.supported_categories),
        "files": [entry.to_dict() for entry in files],
    }
    return SourceDocsManifest(
        schema_version=manifest.schema_version,
        source_docs_root=manifest.source_docs_root,
        source_docs_display_path=manifest.source_docs_display_path,
        supported_categories=manifest.supported_categories,
        categories=categories,
        files=files,
        manifest_hash=_stable_hash(payload),
        warnings=manifest.warnings,
    )


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _doc_storage_relative_path(manifest: SourceDocsManifest, relative_path: str) -> str:
    prefix = Path(manifest.source_docs_root).name or "knowledge-sources"
    if not prefix or relative_path.startswith(f"{prefix}/"):
        return relative_path
    return f"{prefix}/{relative_path}"


def _code_bundles_by_path(indexed: IndexedCode) -> dict[str, _ObjectBundle]:
    file_by_id = {record.file_id: record for record in indexed.file_records}
    entities_by_file: dict[str, list[EntityRecord]] = {}
    for entity in indexed.entity_records:
        entities_by_file.setdefault(entity.file_id, []).append(entity)
    chunks_by_file: dict[str, list[ChunkRecord]] = {}
    for chunk in indexed.chunk_records:
        chunks_by_file.setdefault(chunk.file_id, []).append(chunk)
    evidence_by_file: dict[str, list[EvidenceRecord]] = {}
    for evidence in indexed.evidence_records:
        evidence_by_file.setdefault(evidence.file_id, []).append(evidence)

    bundles: dict[str, _ObjectBundle] = {}
    for file_id, file_record in file_by_id.items():
        entity_ids = {item.entity_id for item in entities_by_file.get(file_id, [])}
        relations = tuple(
            record
            for record in indexed.relation_records
            if record.src_entity_id in entity_ids or record.dst_entity_id in entity_ids
        )
        if file_record.metadata.get("synthetic_anchor") is True:
            continue
        bundles[file_record.relative_path] = _ObjectBundle(
            file_record=file_record,
            chunks=tuple(chunks_by_file.get(file_id, [])),
            entities=tuple(entities_by_file.get(file_id, [])),
            relations=relations,
            evidence=tuple(evidence_by_file.get(file_id, [])),
        )
    return bundles


def _doc_bundle_from_indexed(indexed: IndexedDocuments, relative_path: str) -> _ObjectBundle | None:
    file_record = next(
        (record for record in indexed.file_records if record.relative_path == relative_path),
        None,
    )
    if file_record is None:
        return None
    chunks = tuple(
        record for record in indexed.chunk_records if record.file_id == file_record.file_id
    )
    entities = tuple(
        record for record in indexed.entity_records if record.file_id == file_record.file_id
    )
    evidence = tuple(
        record for record in indexed.evidence_records if record.file_id == file_record.file_id
    )
    vectors = tuple(
        write.record
        for write in indexed.vector_writes
        if write.record.object_id in {chunk.chunk_id for chunk in chunks}
    )
    return _ObjectBundle(
        file_record=file_record,
        chunks=chunks,
        entities=entities,
        relations=(),
        evidence=evidence,
        vectors=vectors,
    )


def _load_live_bundle(
    reader: object,
    *,
    relative_path: str,
    snapshot_id: str,
) -> _ObjectBundle | None:
    scope = QueryScope(snapshot_id=snapshot_id)
    file_record = next(
        (record for record in reader.iter_files(scope) if record.relative_path == relative_path),
        None,
    )
    if file_record is None:
        return None
    chunks = tuple(
        item.record
        for item in reader.logical_chunks(scope)
        if item.record.file_id == file_record.file_id
    )
    entities = tuple(
        item.record
        for item in reader.logical_entities(scope)
        if item.record.file_id == file_record.file_id
    )
    entity_ids = {record.entity_id for record in entities}
    relations = tuple(
        item.record
        for item in reader.logical_relations(scope)
        if item.record.src_entity_id in entity_ids or item.record.dst_entity_id in entity_ids
    )
    evidence = tuple(
        item.record
        for item in reader.logical_evidence(scope)
        if item.record.file_id == file_record.file_id
    )
    vectors = tuple(
        record
        for record in reader.iter_vector_refs(scope)
        if record.object_id in {chunk.chunk_id for chunk in chunks}
    )
    return _ObjectBundle(
        file_record=file_record,
        chunks=chunks,
        entities=entities,
        relations=relations,
        evidence=evidence,
        vectors=vectors,
    )


def _matched_chunk_replacements(
    old_chunks: Sequence[ChunkRecord],
    new_chunks: Sequence[ChunkRecord],
) -> dict[str, str]:
    new_by_key = {_chunk_match_key(record): record.chunk_id for record in new_chunks}
    replacements: dict[str, str] = {}
    for record in old_chunks:
        replacement_id = new_by_key.get(_chunk_match_key(record))
        if replacement_id is not None:
            replacements[record.chunk_id] = replacement_id
    return replacements


def _matched_entity_replacements(
    old_entities: Sequence[EntityRecord],
    new_entities: Sequence[EntityRecord],
) -> dict[str, str]:
    new_by_key = {_entity_match_key(record): record.entity_id for record in new_entities}
    replacements: dict[str, str] = {}
    for record in old_entities:
        replacement_id = new_by_key.get(_entity_match_key(record))
        if replacement_id is not None:
            replacements[record.entity_id] = replacement_id
    return replacements


def _matched_relation_replacements(
    old_relations: Sequence[RelationRecord],
    new_relations: Sequence[RelationRecord],
    *,
    entity_replacements: Mapping[str, str],
) -> dict[str, str]:
    new_by_key = {
        _relation_match_key(record, entity_replacements={}): record.relation_id
        for record in new_relations
    }
    replacements: dict[str, str] = {}
    for record in old_relations:
        normalized = replace(
            record,
            src_entity_id=entity_replacements.get(record.src_entity_id, record.src_entity_id),
            dst_entity_id=entity_replacements.get(record.dst_entity_id, record.dst_entity_id),
        )
        replacement_id = new_by_key.get(_relation_match_key(normalized, entity_replacements={}))
        if replacement_id is not None:
            replacements[record.relation_id] = replacement_id
    return replacements


def _chunk_match_key(record: ChunkRecord) -> tuple[object, ...]:
    return (
        record.chunk_type,
        record.ordinal,
        record.metadata.get("item_name"),
        record.metadata.get("item_type"),
    )


def _entity_match_key(record: EntityRecord) -> tuple[object, ...]:
    return (
        record.entity_type,
        record.name,
        record.qualified_name if record.entity_type in {"Document", "API", "Widget"} else None,
        record.metadata.get("symbol_kind"),
    )


def _relation_match_key(
    record: RelationRecord,
    *,
    entity_replacements: Mapping[str, str],
) -> tuple[object, ...]:
    return (
        record.relation_type,
        entity_replacements.get(record.src_entity_id, record.src_entity_id),
        entity_replacements.get(record.dst_entity_id, record.dst_entity_id),
        record.metadata.get("condition_expr"),
    )


def _chunk_relative_path(record: ChunkRecord) -> str | None:
    value = record.metadata.get("path")
    return None if value is None else str(value)
