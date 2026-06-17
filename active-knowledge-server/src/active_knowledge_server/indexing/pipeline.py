"""Incremental indexing pipeline and stateful diff orchestration."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.source_docs import (
    SourceDocsConnector,
    SourceDocsManifest,
    SourceDocsScanProgress,
)
from active_knowledge_server.connectors.workspace import (
    WorkspaceConnector,
    WorkspaceInventory,
    WorkspaceScanProgress,
)
from active_knowledge_server.indexing.artifacts import (
    CollectArtifactRef,
    IndexCollectArtifactStore,
)
from active_knowledge_server.indexing.code_indexer import (
    CODE_INDEXER_SCHEMA_VERSION,
    CodeIndexer,
    IndexedCode,
    count_indexable_workspace_files,
)
from active_knowledge_server.indexing.doc_indexer import (
    DOC_INDEXER_SCHEMA_VERSION,
    DocumentIndexer,
    IndexedDocuments,
)
from active_knowledge_server.indexing.jobs import (
    IndexJobCancelled,
    JobStateTransitionError,
    SQLiteJobStore,
    decode_task_checkpoint,
    record_task_applied_checkpoint,
    record_task_attempt,
    record_task_collected_checkpoint,
    task_checkpoint_key,
    task_has_attempt_record,
)
from active_knowledge_server.indexing.profile import (
    PROFILE_COLLECTOR_SCHEMA_VERSION,
    CollectedProfiles,
    ProfileCollector,
)
from active_knowledge_server.indexing.progress import (
    IndexProgressCallback,
    IndexProgressEvent,
    SlidingWindowEtaEstimator,
    noop_progress_callback,
    utc_timestamp,
)
from active_knowledge_server.indexing.relation_extractor import (
    PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION,
    ProfileConditionedRelationExtractor,
    profile_config_hash,
)
from active_knowledge_server.indexing.resume import IndexPlanSignature, make_index_plan_signature
from active_knowledge_server.indexing.snapshot import CURRENT_SNAPSHOT_ID
from active_knowledge_server.indexing.tasks import IndexTask, make_index_task_list
from active_knowledge_server.indexing.workspace_map import WorkspaceMapBuilder
from active_knowledge_server.storage import (
    ALL_SCOPE,
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    JobStatus,
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
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_local_sqlite_stores,
    utc_now,
)

INCREMENTAL_INDEX_STATE_SCHEMA_VERSION: Final = "incremental_index_state.v1"
INCREMENTAL_INDEX_RESULT_SCHEMA_VERSION: Final = "incremental_index_result.v1"
INCREMENTAL_INDEX_CREATED_BY_JOB_FALLBACK: Final = "job:incremental_index"
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
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable execution result."""

        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "result_status": self.result_status,
            "plan": self.plan.to_dict(),
            "warnings": [warning.to_dict() for warning in self.warnings],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class IndexRunContext:
    """Persistent job context observed by the incremental pipeline."""

    job_store: SQLiteJobStore
    job_id: str
    resume_policy: Mapping[str, object] | None = None
    plan_signature: IndexPlanSignature | None = None


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


@dataclass(frozen=True)
class ApplyBatchItem:
    """One deterministic incremental apply unit grouped into a writer batch."""

    task_key: str
    phase: str
    source_kind: str
    operation: str
    relative_path: str
    storage_relative_path: str | None = None
    old_bundle: _ObjectBundle | None = None
    new_bundle: _ObjectBundle | None = None
    vector_writes: tuple[tuple[VectorRefRecord, tuple[float, ...]], ...] = ()
    checkpoint_metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def record_counts(self) -> Mapping[str, object]:
        value = self.checkpoint_metadata.get("record_counts")
        if isinstance(value, Mapping):
            return value
        return {}

    @property
    def record_total(self) -> int:
        return _record_counts_total(self.record_counts)


@dataclass(frozen=True)
class ApplyBatch:
    """One committed transaction-sized batch of incremental apply work."""

    phase: str
    items: tuple[ApplyBatchItem, ...]
    max_files_per_transaction: int
    max_records_per_transaction: int
    commit_interval_ms: int

    @property
    def file_count(self) -> int:
        return len(self.items)

    @property
    def record_count(self) -> int:
        return sum(item.record_total for item in self.items)


@dataclass(frozen=True)
class _AppliedBatchResult:
    batch: ApplyBatch
    elapsed_seconds: float
    item_elapsed_seconds: tuple[float, ...]


@dataclass(frozen=True)
class _FailedApplyItem:
    item: ApplyBatchItem
    error: Exception


_RUNNING_JOB_STATUS_ORDER: Final[tuple[JobStatus, ...]] = (
    "discovering",
    "parsing",
    "extracting",
    "embedding",
    "reporting",
)
_INDEX_PHASE_JOB_STATUS: Final[dict[str, JobStatus]] = {
    "discover": "discovering",
    "plan": "discovering",
    "code": "parsing",
    "code_collect": "parsing",
    "code_finalize": "parsing",
    "docs": "parsing",
    "doc_collect": "parsing",
    "doc_finalize": "parsing",
    "code_apply": "extracting",
    "doc_apply": "extracting",
    "profile_relations": "extracting",
    "workspace_map": "extracting",
    "vectors_apply": "embedding",
    "done": "reporting",
}


@dataclass
class _PipelineJobReporter:
    context: IndexRunContext
    config: ActiveKnowledgeConfig
    plan: IncrementalIndexPlan
    tasks: tuple[IndexTask, ...]
    source: str
    snapshot_id: str
    started_at: str
    _status: str = field(init=False)
    _last_phase: str | None = field(default=None, init=False)
    _tasks_applied: int = field(default=0, init=False)
    _tasks_failed: int = field(default=0, init=False)
    _tasks_skipped: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        job = self.context.job_store.get_job(self.context.job_id)
        self._status = "pending" if job is None else job.status

    def start(self) -> None:
        signature = self.context.plan_signature or make_index_plan_signature(
            self.plan,
            config=self.config,
        )
        task_counts = _index_task_counts(self.tasks)
        self._update(
            phase="plan",
            status="discovering",
            metadata_update={
                "execution_state": "running",
                "started_at": self.started_at,
                "last_phase": "plan",
                "plan_signature": signature.digest,
                "plan_signature_payload": signature.to_dict(),
                "plan_summary": _index_plan_summary(self.plan),
                "tasks_total": task_counts["total"],
                "tasks_by_phase": task_counts["by_phase"],
                "tasks_by_source_kind": task_counts["by_source_kind"],
                "tasks_required": task_counts["required"],
                "tasks_applied": self._tasks_applied,
                "tasks_skipped": self._tasks_skipped,
                "tasks_failed": self._tasks_failed,
                **_resume_policy_metadata(self.context.resume_policy),
            },
        )

    def observe_event(self, event: IndexProgressEvent) -> None:
        phase = str(event.phase)
        if phase == self._last_phase and event.current_path is None and phase != "done":
            return
        self._update(
            phase=phase,
            status=_INDEX_PHASE_JOB_STATUS.get(phase),
            metadata_update={
                "execution_state": "running",
                "last_phase": phase,
                "last_message": event.message,
                "global_total": event.global_total,
                "global_done": event.global_done,
            },
        )

    def begin_task(self, task_key: str, *, phase: str | None = None) -> None:
        if self.context.job_store.cancel_requested(self.context.job_id):
            raise IndexJobCancelled(f"index job {self.context.job_id!r} was cancelled")
        task = _find_index_task(self.tasks, task_key)
        task_phase = phase or (task.phase if task is not None else self._last_phase or "plan")
        metadata_update: dict[str, object] = {
            "execution_state": "running",
            "last_phase": task_phase,
            "last_task_key": task_key,
            "tasks_applied": self._tasks_applied,
            "tasks_skipped": self._tasks_skipped,
            "tasks_failed": self._tasks_failed,
        }
        if task is not None:
            metadata_update["last_task"] = task.to_dict()
            if task.relative_path is not None:
                metadata_update["last_path"] = task.relative_path
        self._update(
            phase=task_phase,
            status=_INDEX_PHASE_JOB_STATUS.get(task_phase),
            metadata_update=metadata_update,
        )

    def task_applied(self, task_key: str) -> None:
        self._tasks_applied += 1
        self._update(
            phase=self._last_phase or "plan",
            metadata_update={
                "last_task_key": task_key,
                "tasks_applied": self._tasks_applied,
                "tasks_failed": self._tasks_failed,
                "tasks_skipped": self._tasks_skipped,
            },
        )

    def task_skipped(self, task_key: str) -> None:
        self._tasks_skipped += 1
        task = _find_index_task(self.tasks, task_key)
        task_phase = task.phase if task is not None else self._last_phase or "plan"
        metadata_update: dict[str, object] = {
            "last_task_key": task_key,
            "tasks_applied": self._tasks_applied,
            "tasks_failed": self._tasks_failed,
            "tasks_skipped": self._tasks_skipped,
        }
        if task is not None:
            metadata_update["last_task"] = task.to_dict()
            if task.relative_path is not None:
                metadata_update["last_path"] = task.relative_path
        self._update(
            phase=task_phase,
            status=_INDEX_PHASE_JOB_STATUS.get(task_phase),
            metadata_update=metadata_update,
        )

    def task_failed(self, task_key: str, error: BaseException | str) -> None:
        self._tasks_failed += 1
        self._update(
            phase=self._last_phase or "plan",
            metadata_update={
                "last_task_key": task_key,
                "last_task_error": str(error),
                "tasks_applied": self._tasks_applied,
                "tasks_failed": self._tasks_failed,
                "tasks_skipped": self._tasks_skipped,
            },
        )

    def _update(
        self,
        *,
        phase: str,
        metadata_update: Mapping[str, object],
        status: JobStatus | None = None,
    ) -> None:
        self._last_phase = phase
        if status is None:
            self.context.job_store.transition_or_update_running_metadata(
                self.context.job_id,
                metadata_update=metadata_update,
            )
            return
        self._advance_to(status, metadata_update=metadata_update)

    def _advance_to(self, status: JobStatus, *, metadata_update: Mapping[str, object]) -> None:
        if status not in _RUNNING_JOB_STATUS_ORDER:
            self.context.job_store.transition_or_update_running_metadata(
                self.context.job_id,
                metadata_update=metadata_update,
            )
            return
        target_index = _RUNNING_JOB_STATUS_ORDER.index(status)
        if self._status in _RUNNING_JOB_STATUS_ORDER:
            current_index = _RUNNING_JOB_STATUS_ORDER.index(self._status)
            if target_index < current_index:
                self.context.job_store.transition_or_update_running_metadata(
                    self.context.job_id,
                    metadata_update=metadata_update,
                )
                return
        elif self._status != "pending":
            raise JobStateTransitionError(
                f"job {self.context.job_id!r} is not running or pending from {self._status!r}"
            )

        while self._status != status:
            next_status = _next_running_status(self._status)
            update = metadata_update if next_status == status else None
            job = self.context.job_store.transition_or_update_running_metadata(
                self.context.job_id,
                next_status,
                metadata_update=update,
            )
            self._status = job.status
        if self._status == status:
            self.context.job_store.transition_or_update_running_metadata(
                self.context.job_id,
                metadata_update=metadata_update,
            )


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
        self._manage_local_sqlite_stores = metadata_adapter is None
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
        """Persist the latest fully successful incremental state.

        This file is the next-run diff baseline, not an in-flight task checkpoint.
        """

        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), ensure_ascii=True, indent=2, sort_keys=True)
        temporary_path = path.with_name(f"{path.name}.tmp")
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(path)
        return path

    def capture_state(
        self,
        *,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
        progress_callback: IndexProgressCallback | None = None,
        started_at: str | None = None,
    ) -> tuple[IncrementalIndexState, WorkspaceInventory, SourceDocsManifest, CollectedProfiles]:
        """Scan current manifests and return the derived incremental state."""

        callback = progress_callback or noop_progress_callback
        discover_started_at = started_at or utc_timestamp()

        def emit_discover(stage_done: int, message: str) -> None:
            callback(
                IndexProgressEvent(
                    phase="discover",
                    stage_total=3,
                    stage_done=stage_done,
                    message=message,
                    started_at=discover_started_at,
                    updated_at=utc_timestamp(),
                )
            )

        def handle_workspace_scan_progress(progress: WorkspaceScanProgress) -> None:
            emit_discover(0, _format_workspace_discover_message(progress))

        def handle_source_docs_scan_progress(progress: SourceDocsScanProgress) -> None:
            emit_discover(1, _format_source_docs_discover_message(progress))

        callback(
            IndexProgressEvent(
                phase="discover",
                stage_total=3,
                stage_done=0,
                message="Scanning workspace inventory",
                started_at=discover_started_at,
                updated_at=discover_started_at,
            )
        )
        workspace_inventory = self._workspace_connector.scan(
            progress_callback=handle_workspace_scan_progress,
        )
        emit_discover(1, "Scanning source documents")
        source_docs_manifest = self._source_docs_connector.scan(
            progress_callback=handle_source_docs_scan_progress,
        )
        emit_discover(2, "Collecting build profiles")
        collected_profiles = self._profile_collector.collect(snapshot_id=snapshot_id)
        emit_discover(3, "Discovery complete")
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
                entry.relative_path: entry.content_hash or "" for entry in workspace_inventory.files
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
        progress_callback: IndexProgressCallback | None = None,
        started_at: str | None = None,
    ) -> IncrementalIndexPlan:
        """Plan one incremental update from persisted state and current manifests."""

        previous = self.load_state()
        current, workspace_inventory, source_docs_manifest, collected_profiles = self.capture_state(
            snapshot_id=snapshot_id,
            progress_callback=progress_callback,
            started_at=started_at,
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
            or previous.profile_collector_schema_version != current.profile_collector_schema_version
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
                        "Code parser/extractor version changed; all code files will be rebuilt."
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
        progress_callback: IndexProgressCallback | None = None,
        plan: IncrementalIndexPlan | None = None,
        run_context: IndexRunContext | None = None,
    ) -> IncrementalIndexResult:
        """Execute one incremental overlay update and persist the next incremental state."""

        if self._manage_local_sqlite_stores:
            migrate_local_sqlite_stores(self._config, cwd=self._cwd)

        callback = progress_callback or noop_progress_callback
        started_at = utc_timestamp()
        if plan is None:
            plan = self.plan(
                snapshot_id=snapshot_id,
                source=source,
                progress_callback=callback,
                started_at=started_at,
            )
        tasks = make_index_task_list(plan)
        signature = (
            run_context.plan_signature
            if run_context is not None and run_context.plan_signature is not None
            else make_index_plan_signature(plan, config=self._config)
        )
        checkpoints: Mapping[str, str] = {}
        job_plan_signature: str | None = None
        if run_context is not None:
            checkpoints = run_context.job_store.get_checkpoints(run_context.job_id)
            job = run_context.job_store.get_job(run_context.job_id)
            if job is not None and isinstance(job.metadata.get("plan_signature"), str):
                job_plan_signature = cast(str, job.metadata["plan_signature"])
        skipped_tasks = tuple(
            task
            for task in tasks
            if _task_has_matching_applied_checkpoint(
                checkpoints,
                task,
                plan_signature=signature.digest,
                job_plan_signature=job_plan_signature,
            )
        )
        skipped_task_keys = frozenset(task.task_key for task in skipped_tasks)
        tasks_to_run = tuple(task for task in tasks if task.task_key not in skipped_task_keys)
        replayed_task_keys = frozenset(
            task.task_key for task in tasks_to_run if task_has_attempt_record(checkpoints, task)
        )
        task_counts = _index_task_counts(tasks)
        job_reporter = (
            None
            if run_context is None
            else _PipelineJobReporter(
                context=run_context,
                config=self._config,
                plan=plan,
                tasks=tasks,
                source=source,
                snapshot_id=snapshot_id,
                started_at=started_at,
            )
        )
        if job_reporter is not None:
            job_reporter.start()
        artifact_store = (
            None
            if run_context is None
            else IndexCollectArtifactStore.from_config(
                self._config,
                cwd=self._cwd,
                job_id=run_context.job_id,
            )
        )
        doc_paths_to_collect = _collect_dependency_paths(tasks_to_run, prefix="doc:collect:")
        code_paths_to_collect = _code_collect_paths_after_skips(plan, skipped_tasks)
        progress_totals = _progress_totals_from_tasks(
            plan,
            source,
            tasks,
            code_paths_to_collect=code_paths_to_collect,
            doc_paths_to_collect=doc_paths_to_collect,
        )
        global_total = progress_totals["global_total"]
        global_done = 0
        task_stats = {"applied": 0, "skipped": 0, "failed": 0, "replayed": 0}
        started_task_keys: set[str] = set()
        phase_done = {
            "code_apply": 0,
            "doc_apply": 0,
            "vectors_apply": 0,
            "profile_relations": 0,
            "workspace_map": 0,
        }

        def emit(
            *,
            phase: str,
            stage_total: int | None,
            stage_done: int | None,
            current_path: str | None = None,
            message: str | None = None,
            warnings_count: int = 0,
            explicit_global_done: int | None = None,
            eta_seconds: float | None = None,
        ) -> None:
            event = IndexProgressEvent(
                phase=cast(Any, phase),
                stage_total=stage_total,
                stage_done=stage_done,
                global_total=global_total,
                global_done=global_done if explicit_global_done is None else explicit_global_done,
                current_path=current_path,
                message=message,
                warnings_count=warnings_count,
                started_at=started_at,
                updated_at=utc_timestamp(),
                eta_seconds=eta_seconds,
            )
            if job_reporter is not None:
                job_reporter.observe_event(event)
            callback(event)

        def mark_task_applied(
            task_key: str,
            *,
            checkpoint_metadata: Mapping[str, object] | None = None,
            update_stats: bool = True,
        ) -> None:
            if run_context is not None and checkpoint_metadata is not None:
                task = _find_index_task(tasks, task_key)
                if task is not None:
                    record_task_applied_checkpoint(
                        run_context.job_store,
                        run_context.job_id,
                        task,
                        metadata={
                            "job_id": run_context.job_id,
                            "plan_signature": signature.digest,
                            "applied_at": utc_now(),
                            **dict(checkpoint_metadata),
                        },
                    )
            if update_stats:
                task_stats["applied"] += 1
            if job_reporter is not None and update_stats:
                job_reporter.task_applied(task_key)

        def mark_task_started(task_key: str) -> None:
            if task_key in started_task_keys:
                return
            started_task_keys.add(task_key)
            task = _find_index_task(tasks, task_key)
            if task is None:
                return
            if task.task_key in replayed_task_keys:
                task_stats["replayed"] += 1
            if run_context is not None:
                record_task_attempt(
                    run_context.job_store,
                    run_context.job_id,
                    task,
                    metadata={
                        "job_id": run_context.job_id,
                        "plan_signature": signature.digest,
                    },
                )

        def mark_task_skipped(task: IndexTask) -> None:
            nonlocal global_done
            task_stats["skipped"] += 1
            phase_done[task.phase] = phase_done.get(task.phase, 0) + 1
            global_done += 1
            if job_reporter is not None:
                job_reporter.task_skipped(task.task_key)
            emit(
                phase=task.phase,
                stage_total=progress_totals.get(task.phase),
                stage_done=phase_done[task.phase],
                current_path=task.relative_path,
                message="Skipping previously applied task",
            )

        def mark_task_failed(task_key: str, error: BaseException | str) -> None:
            task_stats["failed"] += 1
            if job_reporter is not None:
                job_reporter.task_failed(task_key, error)

        def task_is_skipped(task_key: str) -> bool:
            return task_key in skipped_task_keys

        global_done += 1
        emit(
            phase="plan",
            stage_total=1,
            stage_done=1,
            message="Incremental plan ready",
            warnings_count=len(plan.warnings),
        )
        for skipped_task in skipped_tasks:
            mark_task_skipped(skipped_task)
        reader = self._metadata_adapter.reader()
        writer = self._metadata_adapter.writer(StorageWriteRequest(target="overlay"))
        vector_writer = self._vector_adapter.writer(StorageWriteRequest(target="overlay"))

        warnings = list(plan.warnings)
        failed = False
        writer_limits = _writer_apply_batch_limits(self._config)
        result_metadata: dict[str, object] = {
            "writer": {
                "batch_size": self._config.indexing.writer.batch_size,
                "max_files_per_transaction": writer_limits["max_files_per_transaction"],
                "max_records_per_transaction": writer_limits["max_records_per_transaction"],
                "commit_interval_ms": self._config.indexing.writer.commit_interval_ms,
            },
            "timings": {
                "parser_seconds": 0.0,
                "embedding_seconds": 0.0,
                "metadata_write_seconds": 0.0,
                "vector_write_seconds": 0.0,
            },
            "diagnostics": {
                "slowest_items": [],
            },
            "apply_batches": _empty_apply_batch_metadata(writer_limits),
            "collect_artifacts": {},
            "tasks": {
                "total": task_counts["total"],
                "by_phase": task_counts["by_phase"],
                "by_source_kind": task_counts["by_source_kind"],
                "required": task_counts["required"],
                "applied": task_stats["applied"],
                "skipped": task_stats["skipped"],
                "failed": task_stats["failed"],
                "replayed": task_stats["replayed"],
            },
        }

        code_indexed: IndexedCode | None = None
        if source in {"all", "code"} and (
            plan.changed_code_paths or plan.deleted_code_paths or plan.reindex_all_code
        ):
            try:
                code_collect_artifact: CollectArtifactRef | None = None
                if artifact_store is not None and code_paths_to_collect:
                    loaded_code_artifact = artifact_store.load_code(
                        plan_signature=signature.digest,
                        expected_paths=code_paths_to_collect,
                        expected_schema_version=CODE_INDEXER_SCHEMA_VERSION,
                    )
                    if loaded_code_artifact is not None:
                        code_indexed, code_collect_artifact = loaded_code_artifact
                        result_metadata["collect_artifacts"] = {
                            **cast(dict[str, object], result_metadata["collect_artifacts"]),
                            "code": _collect_artifact_metadata(
                                status="hit",
                                artifact=code_collect_artifact,
                            ),
                        }
                        if progress_totals["code_collect"] > 0:
                            global_done += progress_totals["code_collect"]
                            emit(
                                phase="code_collect",
                                stage_total=progress_totals["code_collect"],
                                stage_done=progress_totals["code_collect"],
                                message="Reusing collected code artifact",
                            )
                if code_indexed is None:
                    code_collect_base = global_done

                    def handle_code_collect(event: IndexProgressEvent) -> None:
                        updated_event = replace(
                            event,
                            global_total=global_total,
                            global_done=code_collect_base + (event.stage_done or 0),
                            started_at=started_at,
                            updated_at=utc_timestamp(),
                        )
                        if job_reporter is not None:
                            job_reporter.observe_event(updated_event)
                        callback(updated_event)

                    code_indexed = self._code_indexer.collect(
                        snapshot_id=snapshot_id,
                        workspace_inventory=plan.workspace_inventory,
                        include_paths=code_paths_to_collect,
                        progress_callback=handle_code_collect,
                    )
                    if artifact_store is not None and code_paths_to_collect:
                        code_collect_tasks = _tasks_with_collect_dependency(
                            tasks_to_run,
                            prefix="code:collect:",
                        )
                        if not _has_warning_code(
                            code_indexed.warnings,
                            "code_indexer.collect_failed",
                        ):
                            code_collect_artifact = artifact_store.save_code(
                                code_indexed,
                                plan_signature=signature.digest,
                                collect_paths=code_paths_to_collect,
                                task_keys=tuple(task.task_key for task in code_collect_tasks),
                            )
                            _record_collect_artifact_checkpoints(
                                run_context=run_context,
                                tasks=code_collect_tasks,
                                artifact=code_collect_artifact,
                                plan_signature=signature.digest,
                            )
                            result_metadata["collect_artifacts"] = {
                                **cast(dict[str, object], result_metadata["collect_artifacts"]),
                                "code": _collect_artifact_metadata(
                                    status="stored",
                                    artifact=code_collect_artifact,
                                ),
                            }
                        else:
                            result_metadata["collect_artifacts"] = {
                                **cast(dict[str, object], result_metadata["collect_artifacts"]),
                                "code": {
                                    "status": "partial",
                                    "path_count": len(code_paths_to_collect),
                                },
                            }
                result_metadata["code_collect_workers"] = code_indexed.metadata.get(
                    "collect_workers",
                    {},
                )
                _merge_result_section(
                    result_metadata,
                    section_name="code_collect",
                    section_metadata=code_indexed.metadata,
                )
                if any(
                    warning.code == "code_indexer.collect_failed"
                    for warning in code_indexed.warnings
                ):
                    failed = True
                    warnings.append(
                        IncrementalIndexWarning(
                            code="index.code_collect_partial",
                            message=(
                                "One or more code files failed during collect; successful "
                                "files were applied and the previous incremental state was kept."
                            ),
                            details={
                                "paths": [
                                    warning.relative_path
                                    for warning in code_indexed.warnings
                                    if warning.code == "code_indexer.collect_failed"
                                ],
                            },
                        )
                    )
                if code_collect_artifact is None and progress_totals["code_collect"] > 0:
                    global_done += progress_totals["code_collect"]
                new_code_bundles = _code_bundles_by_path(code_indexed)
                code_apply_done = phase_done["code_apply"]
                code_apply_eta = SlidingWindowEtaEstimator()
                code_apply_items: list[ApplyBatchItem] = []
                for relative_path in plan.deleted_code_paths:
                    task_key = f"code:delete:{relative_path}"
                    if task_is_skipped(task_key):
                        continue
                    code_apply_items.append(
                        ApplyBatchItem(
                            task_key=task_key,
                            phase="code_apply",
                            source_kind="code",
                            operation="delete",
                            relative_path=relative_path,
                            old_bundle=_load_live_bundle(
                                reader,
                                relative_path=relative_path,
                                snapshot_id=snapshot_id,
                            ),
                            checkpoint_metadata=_delete_checkpoint_metadata(
                                source_kind="code",
                                relative_path=relative_path,
                            ),
                        )
                    )
                for relative_path in plan.changed_code_paths:
                    task_key = f"code:apply:{relative_path}"
                    if task_is_skipped(task_key):
                        continue
                    bundle = new_code_bundles.get(relative_path)
                    if bundle is None:
                        continue
                    code_apply_items.append(
                        ApplyBatchItem(
                            task_key=task_key,
                            phase="code_apply",
                            source_kind="code",
                            operation="apply",
                            relative_path=relative_path,
                            old_bundle=_load_live_bundle(
                                reader,
                                relative_path=relative_path,
                                snapshot_id=snapshot_id,
                            ),
                            new_bundle=bundle,
                            checkpoint_metadata=_bundle_checkpoint_metadata(
                                bundle,
                                source_kind="code",
                                relative_path=relative_path,
                                warnings=_warning_codes_for_path(
                                    code_indexed.warnings,
                                    relative_path,
                                ),
                            ),
                        )
                    )
                code_apply_items.sort(key=lambda item: item.task_key)
                metadata_batch_results, failed_code_items = self._apply_metadata_batches(
                    reader,
                    writer,
                    job_id=None if run_context is None else run_context.job_id,
                    snapshot_id=snapshot_id,
                    items=code_apply_items,
                    limits=writer_limits,
                    on_batch_open=(
                        None
                        if job_reporter is None
                        else lambda item: (
                            mark_task_started(item.task_key),
                            job_reporter.begin_task(
                                item.task_key,
                                phase=item.phase,
                            ),
                        )[-1]
                    ),
                )
                for batch_result in metadata_batch_results:
                    batch = batch_result.batch
                    _record_apply_batch(result_metadata, batch)
                    batch_timing_total = 0.0
                    for item, elapsed_seconds in zip(
                        batch.items,
                        batch_result.item_elapsed_seconds,
                        strict=False,
                    ):
                        if job_reporter is not None and item is not batch.items[0]:
                            mark_task_started(item.task_key)
                            job_reporter.begin_task(item.task_key, phase=batch.phase)
                        code_apply_done += 1
                        global_done += 1
                        batch_timing_total += elapsed_seconds
                        _record_elapsed(
                            result_metadata,
                            timing_key="metadata_write_seconds",
                            elapsed_seconds=elapsed_seconds,
                            path=item.relative_path,
                            stage=batch.phase,
                        )
                        emit(
                            phase="code_apply",
                            stage_total=progress_totals["code_apply"],
                            stage_done=code_apply_done,
                            current_path=item.relative_path,
                            message="Applying code changes",
                            eta_seconds=code_apply_eta.observe(
                                completed=code_apply_done,
                                total=progress_totals["code_apply"],
                            ),
                        )
                        mark_task_applied(
                            item.task_key,
                            checkpoint_metadata=item.checkpoint_metadata,
                        )
                    _add_timing(
                        result_metadata,
                        timing_key="metadata_write_seconds",
                        elapsed_seconds=max(
                            batch_result.elapsed_seconds - batch_timing_total,
                            0.0,
                        ),
                    )
                for failed_item in failed_code_items:
                    mark_task_failed(failed_item.item.task_key, failed_item.error)
                    failed = True
                    warnings.append(
                        _task_apply_failure_warning(failed_item.item, failed_item.error)
                    )
                if progress_totals["code_apply"] > 0:
                    emit(
                        phase="code_apply",
                        stage_total=progress_totals["code_apply"],
                        stage_done=code_apply_done,
                        message="Flushing code changes to overlay metadata",
                    )
                writer.flush()
            except IndexJobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - surface as degraded incremental failure.
                failed = True
                warnings.append(
                    IncrementalIndexWarning(
                        code="index.code_incremental_failed",
                        message=(
                            "Code incremental indexing failed; keeping previous live code state."
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
            doc_apply_done = phase_done["doc_apply"]
            vector_apply_done = phase_done["vectors_apply"]
            failed_doc_items: tuple[_FailedApplyItem, ...] = ()
            failed_vector_items: tuple[_FailedApplyItem, ...] = ()
            doc_apply_items: list[ApplyBatchItem] = []
            for relative_path in plan.deleted_doc_paths:
                task_key = f"doc:delete:{relative_path}"
                if task_is_skipped(task_key):
                    continue
                storage_relative_path = _doc_storage_relative_path(
                    plan.source_docs_manifest,
                    relative_path,
                )
                doc_apply_items.append(
                    ApplyBatchItem(
                        task_key=task_key,
                        phase="doc_apply",
                        source_kind="doc",
                        operation="delete",
                        relative_path=relative_path,
                        storage_relative_path=storage_relative_path,
                        old_bundle=_load_live_bundle(
                            reader,
                            relative_path=storage_relative_path,
                            snapshot_id=snapshot_id,
                        ),
                        checkpoint_metadata=_delete_checkpoint_metadata(
                            source_kind="doc",
                            relative_path=relative_path,
                            storage_relative_path=storage_relative_path,
                        ),
                    )
                )
            vector_apply_items: list[ApplyBatchItem] = []
            doc_apply_eta = SlidingWindowEtaEstimator()
            vector_apply_eta = SlidingWindowEtaEstimator()
            indexed_docs: IndexedDocuments | None = None
            if doc_paths_to_collect:
                try:
                    doc_collect_artifact: CollectArtifactRef | None = None
                    if artifact_store is not None:
                        loaded_doc_artifact = artifact_store.load_docs(
                            plan_signature=signature.digest,
                            expected_paths=doc_paths_to_collect,
                            expected_schema_version=DOC_INDEXER_SCHEMA_VERSION,
                        )
                        if loaded_doc_artifact is not None:
                            indexed_docs, doc_collect_artifact = loaded_doc_artifact
                            result_metadata["collect_artifacts"] = {
                                **cast(dict[str, object], result_metadata["collect_artifacts"]),
                                "docs": _collect_artifact_metadata(
                                    status="hit",
                                    artifact=doc_collect_artifact,
                                ),
                            }
                            global_done += progress_totals["doc_collect"]
                            emit(
                                phase="doc_collect",
                                stage_total=progress_totals["doc_collect"],
                                stage_done=progress_totals["doc_collect"],
                                message="Reusing collected document artifact",
                            )
                    if indexed_docs is None:
                        doc_collect_base = global_done

                        def handle_doc_collect(event: IndexProgressEvent) -> None:
                            updated_event = replace(
                                event,
                                global_total=global_total,
                                global_done=doc_collect_base + (event.stage_done or 0),
                                started_at=started_at,
                                updated_at=utc_timestamp(),
                            )
                            if job_reporter is not None:
                                job_reporter.observe_event(updated_event)
                            callback(updated_event)

                        manifest = _filter_source_docs_manifest(
                            plan.source_docs_manifest,
                            include_paths=doc_paths_to_collect,
                        )
                        indexed_docs = self._doc_indexer.collect(
                            snapshot_id=snapshot_id,
                            source_docs_manifest=manifest,
                            progress_callback=handle_doc_collect,
                        )
                        if artifact_store is not None:
                            doc_collect_tasks = _tasks_with_collect_dependency(
                                tasks_to_run,
                                prefix="doc:collect:",
                            )
                            if not _has_warning_code(indexed_docs.warnings, "docs.collect_failed"):
                                doc_collect_artifact = artifact_store.save_docs(
                                    indexed_docs,
                                    plan_signature=signature.digest,
                                    collect_paths=doc_paths_to_collect,
                                    task_keys=tuple(task.task_key for task in doc_collect_tasks),
                                )
                                _record_collect_artifact_checkpoints(
                                    run_context=run_context,
                                    tasks=doc_collect_tasks,
                                    artifact=doc_collect_artifact,
                                    plan_signature=signature.digest,
                                )
                                result_metadata["collect_artifacts"] = {
                                    **cast(
                                        dict[str, object],
                                        result_metadata["collect_artifacts"],
                                    ),
                                    "docs": _collect_artifact_metadata(
                                        status="stored",
                                        artifact=doc_collect_artifact,
                                    ),
                                }
                            else:
                                result_metadata["collect_artifacts"] = {
                                    **cast(
                                        dict[str, object],
                                        result_metadata["collect_artifacts"],
                                    ),
                                    "docs": {
                                        "status": "partial",
                                        "path_count": len(doc_paths_to_collect),
                                    },
                                }
                    result_metadata["doc_collect_workers"] = indexed_docs.metadata.get(
                        "collect_workers",
                        {},
                    )
                    _merge_result_section(
                        result_metadata,
                        section_name="doc_collect",
                        section_metadata=indexed_docs.metadata,
                    )
                    if any(
                        warning.code == "docs.collect_failed" for warning in indexed_docs.warnings
                    ):
                        failed = True
                        warnings.append(
                            IncrementalIndexWarning(
                                code="index.doc_collect_partial",
                                message=(
                                    "One or more source documents failed during collect; "
                                    "successful documents were applied and the previous "
                                    "incremental state was kept."
                                ),
                                details={
                                    "paths": [
                                        warning.relative_path
                                        for warning in indexed_docs.warnings
                                        if warning.code == "docs.collect_failed"
                                    ],
                                },
                            )
                        )
                    if doc_collect_artifact is None:
                        global_done += progress_totals["doc_collect"]
                except IndexJobCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    warnings.append(
                        IncrementalIndexWarning(
                            code="index.doc_incremental_failed",
                            message="Source document collect failed during incremental indexing.",
                            details={"error": str(exc)},
                        )
                    )

            if indexed_docs is not None:
                indexed_by_path = {
                    record.relative_path: _doc_bundle_from_indexed(
                        indexed_docs, record.relative_path
                    )
                    for record in indexed_docs.file_records
                }
                for relative_path in doc_paths_to_collect:
                    storage_relative_path = _doc_storage_relative_path(
                        plan.source_docs_manifest,
                        relative_path,
                    )
                    new_bundle = indexed_by_path.get(storage_relative_path)
                    if new_bundle is None:
                        continue
                    metadata_changed = (
                        plan.reindex_all_docs or relative_path in plan.changed_doc_paths
                    )
                    if metadata_changed:
                        task_key = f"doc:apply:{relative_path}"
                        if not task_is_skipped(task_key):
                            doc_apply_items.append(
                                ApplyBatchItem(
                                    task_key=task_key,
                                    phase="doc_apply",
                                    source_kind="doc",
                                    operation="apply",
                                    relative_path=relative_path,
                                    storage_relative_path=storage_relative_path,
                                    old_bundle=_load_live_bundle(
                                        reader,
                                        relative_path=storage_relative_path,
                                        snapshot_id=snapshot_id,
                                    ),
                                    new_bundle=new_bundle,
                                    checkpoint_metadata=_bundle_checkpoint_metadata(
                                        new_bundle,
                                        source_kind="doc",
                                        relative_path=relative_path,
                                        storage_relative_path=storage_relative_path,
                                        warnings=_warning_codes_for_path(
                                            indexed_docs.warnings,
                                            storage_relative_path,
                                        ),
                                    ),
                                )
                            )
                    if plan.rebuild_vectors or metadata_changed:
                        task_key = f"vector:doc:{relative_path}"
                        if task_is_skipped(task_key):
                            continue
                        vector_apply_items.append(
                            ApplyBatchItem(
                                task_key=task_key,
                                phase="vectors_apply",
                                source_kind="vector",
                                operation="doc",
                                relative_path=relative_path,
                                storage_relative_path=storage_relative_path,
                                vector_writes=_vector_writes_for_path(
                                    indexed_docs,
                                    storage_relative_path=storage_relative_path,
                                ),
                                checkpoint_metadata=_vector_checkpoint_metadata(
                                    indexed_docs,
                                    relative_path=relative_path,
                                    storage_relative_path=storage_relative_path,
                                ),
                            )
                        )
            doc_apply_items.sort(key=lambda item: item.task_key)
            vector_apply_items.sort(key=lambda item: item.task_key)
            try:
                metadata_batch_results, failed_doc_items = self._apply_metadata_batches(
                    reader,
                    writer,
                    job_id=None if run_context is None else run_context.job_id,
                    snapshot_id=snapshot_id,
                    items=doc_apply_items,
                    limits=writer_limits,
                    on_batch_open=(
                        None
                        if job_reporter is None
                        else lambda item: (
                            mark_task_started(item.task_key),
                            job_reporter.begin_task(
                                item.task_key,
                                phase=item.phase,
                            ),
                        )[-1]
                    ),
                )
                for batch_result in metadata_batch_results:
                    batch = batch_result.batch
                    _record_apply_batch(result_metadata, batch)
                    batch_timing_total = 0.0
                    for item, elapsed_seconds in zip(
                        batch.items,
                        batch_result.item_elapsed_seconds,
                        strict=False,
                    ):
                        if job_reporter is not None and item is not batch.items[0]:
                            mark_task_started(item.task_key)
                            job_reporter.begin_task(item.task_key, phase=batch.phase)
                        doc_apply_done += 1
                        global_done += 1
                        batch_timing_total += elapsed_seconds
                        _record_elapsed(
                            result_metadata,
                            timing_key="metadata_write_seconds",
                            elapsed_seconds=elapsed_seconds,
                            path=item.storage_relative_path or item.relative_path,
                            stage=batch.phase,
                        )
                        emit(
                            phase="doc_apply",
                            stage_total=progress_totals["doc_apply"],
                            stage_done=doc_apply_done,
                            current_path=item.storage_relative_path or item.relative_path,
                            message="Applying document changes",
                            eta_seconds=doc_apply_eta.observe(
                                completed=doc_apply_done,
                                total=progress_totals["doc_apply"],
                            ),
                        )
                        mark_task_applied(
                            item.task_key,
                            checkpoint_metadata=item.checkpoint_metadata,
                        )
                    _add_timing(
                        result_metadata,
                        timing_key="metadata_write_seconds",
                        elapsed_seconds=max(
                            batch_result.elapsed_seconds - batch_timing_total,
                            0.0,
                        ),
                    )
                vector_batch_results, failed_vector_items = self._apply_vector_batches(
                    vector_writer,
                    items=vector_apply_items,
                    limits=writer_limits,
                    on_batch_open=(
                        None
                        if job_reporter is None
                        else lambda item: (
                            mark_task_started(item.task_key),
                            job_reporter.begin_task(
                                item.task_key,
                                phase=item.phase,
                            ),
                        )[-1]
                    ),
                )
                for batch_result in vector_batch_results:
                    batch = batch_result.batch
                    _record_apply_batch(result_metadata, batch)
                    batch_timing_total = 0.0
                    for item, elapsed_seconds in zip(
                        batch.items,
                        batch_result.item_elapsed_seconds,
                        strict=False,
                    ):
                        if job_reporter is not None and item is not batch.items[0]:
                            mark_task_started(item.task_key)
                            job_reporter.begin_task(item.task_key, phase=batch.phase)
                        vector_apply_done += 1
                        global_done += 1
                        batch_timing_total += elapsed_seconds
                        _record_elapsed(
                            result_metadata,
                            timing_key="vector_write_seconds",
                            elapsed_seconds=elapsed_seconds,
                            path=item.storage_relative_path or item.relative_path,
                            stage=batch.phase,
                        )
                        emit(
                            phase="vectors_apply",
                            stage_total=progress_totals["vectors_apply"],
                            stage_done=vector_apply_done,
                            current_path=item.storage_relative_path or item.relative_path,
                            message="Applying document vectors",
                            eta_seconds=vector_apply_eta.observe(
                                completed=vector_apply_done,
                                total=progress_totals["vectors_apply"],
                            ),
                        )
                        mark_task_applied(
                            item.task_key,
                            checkpoint_metadata=item.checkpoint_metadata,
                        )
                    _add_timing(
                        result_metadata,
                        timing_key="vector_write_seconds",
                        elapsed_seconds=max(
                            batch_result.elapsed_seconds - batch_timing_total,
                            0.0,
                        ),
                    )
                for failed_item in failed_doc_items:
                    mark_task_failed(failed_item.item.task_key, failed_item.error)
                    failed = True
                    warnings.append(
                        _task_apply_failure_warning(failed_item.item, failed_item.error)
                    )
                for failed_item in failed_vector_items:
                    mark_task_failed(failed_item.item.task_key, failed_item.error)
                    failed = True
                    warnings.append(
                        _task_apply_failure_warning(failed_item.item, failed_item.error)
                    )
            except IndexJobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                failed = True
                warnings.append(
                    IncrementalIndexWarning(
                        code="index.doc_incremental_failed",
                        message="A source document failed during incremental indexing.",
                        details={"error": str(exc)},
                    )
                )
            if progress_totals["doc_apply"] > 0:
                emit(
                    phase="doc_apply",
                    stage_total=progress_totals["doc_apply"],
                    stage_done=doc_apply_done,
                    message="Flushing document changes to overlay metadata",
                )
            writer.flush()
            if progress_totals["vectors_apply"] > 0:
                emit(
                    phase="vectors_apply",
                    stage_total=progress_totals["vectors_apply"],
                    stage_done=vector_apply_done,
                    message="Flushing document vectors to overlay store",
                )
            vector_writer.flush()

        if source in {"all", "code"} and plan.rebuild_profile_conditioned_relations:
            task_key = "profile:relations"
            if not task_is_skipped(task_key):
                if job_reporter is not None:
                    mark_task_started(task_key)
                    job_reporter.begin_task(task_key, phase="profile_relations")
                try:
                    profile_started_at = time.perf_counter()
                    emit(
                        phase="profile_relations",
                        stage_total=1,
                        stage_done=0,
                        message="Refreshing profile-conditioned relations",
                    )
                    self._rebuild_profile_conditioned_relations(
                        reader=reader,
                        writer=writer,
                        job_id=None if run_context is None else run_context.job_id,
                        snapshot_id=snapshot_id,
                        collected_profiles=plan.collected_profiles,
                        changed_profile_ids=set(plan.changed_profile_ids),
                        removed_profile_ids=set(plan.removed_profile_ids),
                    )
                    emit(
                        phase="profile_relations",
                        stage_total=1,
                        stage_done=0,
                        message="Flushing profile-conditioned relation updates",
                    )
                    writer.flush()
                    result_metadata["timings"]["metadata_write_seconds"] = round(
                        float(result_metadata["timings"]["metadata_write_seconds"])
                        + (time.perf_counter() - profile_started_at),
                        6,
                    )
                    global_done += 1
                    emit(
                        phase="profile_relations",
                        stage_total=1,
                        stage_done=1,
                        message="Refreshing profile-conditioned relations",
                    )
                    mark_task_applied(task_key)
                except IndexJobCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    mark_task_failed(task_key, exc)
                    failed = True
                    warnings.append(
                        IncrementalIndexWarning(
                            code="index.profile_incremental_failed",
                            message=(
                                "Profile-conditioned relations failed to rebuild; previous "
                                "profile projection remains live."
                            ),
                            details={"error": str(exc)},
                        ),
                    )

        if _workspace_map_refresh_required(plan):
            task_key = "workspace:map"
            if not task_is_skipped(task_key):
                if job_reporter is not None:
                    mark_task_started(task_key)
                    job_reporter.begin_task(task_key, phase="workspace_map")
                try:
                    workspace_map_started_at = time.perf_counter()
                    emit(
                        phase="workspace_map",
                        stage_total=1,
                        stage_done=0,
                        message="Refreshing workspace map",
                    )
                    self._workspace_map_builder.collect_and_write(
                        snapshot_id=snapshot_id,
                        workspace_inventory=plan.workspace_inventory,
                        reader=self._metadata_adapter.reader(),
                        profiles=plan.collected_profiles.profile_records,
                        profile_resolution=plan.collected_profiles.resolution.to_dict(),
                    )
                    result_metadata["timings"]["workspace_map_seconds"] = round(
                        time.perf_counter() - workspace_map_started_at,
                        6,
                    )
                    global_done += 1
                    emit(
                        phase="workspace_map",
                        stage_total=1,
                        stage_done=1,
                        message="Refreshing workspace map",
                    )
                    mark_task_applied(task_key)
                except IndexJobCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    mark_task_failed(task_key, exc)
                    failed = True
                    warnings.append(
                        IncrementalIndexWarning(
                            code="index.workspace_map_failed",
                            message=(
                                "Workspace map projection failed to refresh; previous "
                                "workspace navigation artifact remains live."
                            ),
                            details={"error": str(exc)},
                        ),
                    )

        if not failed:
            self.save_state(plan.current_state)

        result_metadata["tasks"]["applied"] = task_stats["applied"]
        result_metadata["tasks"]["skipped"] = task_stats["skipped"]
        result_metadata["tasks"]["failed"] = task_stats["failed"]
        result_metadata["tasks"]["replayed"] = task_stats["replayed"]

        emit(
            phase="done",
            stage_total=1,
            stage_done=1,
            message="Incremental indexing finished",
            warnings_count=len(warnings),
            explicit_global_done=global_total,
        )

        return IncrementalIndexResult(
            schema_version=INCREMENTAL_INDEX_RESULT_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            result_status="partial_ready" if failed else "ready",
            plan=plan,
            warnings=tuple(warnings),
            metadata=result_metadata,
        )

    def _apply_metadata_batches(
        self,
        reader: object,
        writer: object,
        *,
        job_id: str | None,
        snapshot_id: str,
        items: Sequence[ApplyBatchItem],
        limits: Mapping[str, int],
        on_batch_open: Callable[[ApplyBatchItem], None] | None = None,
    ) -> tuple[tuple[_AppliedBatchResult, ...], tuple[_FailedApplyItem, ...]]:
        max_files = int(limits["max_files_per_transaction"])
        max_records = int(limits["max_records_per_transaction"])
        commit_interval_ms = int(limits["commit_interval_ms"])
        if not items:
            return (), ()

        results: list[_AppliedBatchResult] = []
        failed_items: list[_FailedApplyItem] = []
        current_items: list[ApplyBatchItem] = []
        current_records = 0
        batch_started_at: float | None = None
        transaction_cm: object | None = None

        def open_batch(first_item: ApplyBatchItem) -> None:
            nonlocal batch_started_at, transaction_cm
            batch_started_at = time.perf_counter()
            transaction_cm = writer.transaction()
            transaction_cm.__enter__()
            if on_batch_open is not None:
                on_batch_open(first_item)

        def current_batch() -> ApplyBatch:
            return ApplyBatch(
                phase=current_items[0].phase,
                items=tuple(current_items),
                max_files_per_transaction=max_files,
                max_records_per_transaction=max_records,
                commit_interval_ms=commit_interval_ms,
            )

        def reset_batch_state() -> None:
            nonlocal current_records, batch_started_at, transaction_cm
            current_items.clear()
            current_records = 0
            batch_started_at = None
            transaction_cm = None

        def close_batch() -> None:
            assert batch_started_at is not None
            assert transaction_cm is not None
            transaction_cm.__exit__(None, None, None)
            elapsed_seconds = time.perf_counter() - batch_started_at
            per_item_elapsed = (
                tuple(elapsed_seconds / len(current_items) for _ in current_items)
                if current_items
                else ()
            )
            results.append(
                _AppliedBatchResult(
                    batch=current_batch(),
                    elapsed_seconds=elapsed_seconds,
                    item_elapsed_seconds=per_item_elapsed,
                )
            )
            reset_batch_state()

        try:
            for item in items:
                item_records = max(1, item.record_total)
                if current_items and (
                    len(current_items) >= max_files
                    or current_records + item_records > max_records
                ):
                    close_batch()
                if transaction_cm is None:
                    open_batch(item)
                current_items.append(item)
                current_records += item_records
                try:
                    self._apply_metadata_batch_item(
                        reader,
                        writer,
                        job_id=job_id,
                        snapshot_id=snapshot_id,
                        item=item,
                    )
                except Exception as exc:
                    assert transaction_cm is not None
                    transaction_cm.__exit__(type(exc), exc, exc.__traceback__)
                    degraded_results, degraded_failures = self._apply_batch_with_degradation(
                        current_batch(),
                        apply_batch=lambda batch: self._apply_one_metadata_batch(
                            reader,
                            writer,
                            job_id=job_id,
                            snapshot_id=snapshot_id,
                            batch=batch,
                            on_batch_open=on_batch_open,
                        ),
                    )
                    results.extend(degraded_results)
                    failed_items.extend(degraded_failures)
                    reset_batch_state()
                    continue
                assert batch_started_at is not None
                if (time.perf_counter() - batch_started_at) * 1000.0 >= commit_interval_ms:
                    close_batch()
            if transaction_cm is not None:
                close_batch()
        except Exception as exc:
            if transaction_cm is not None:
                transaction_cm.__exit__(type(exc), exc, exc.__traceback__)
            raise

        return tuple(results), tuple(failed_items)

    def _apply_vector_batches(
        self,
        vector_writer: object,
        *,
        items: Sequence[ApplyBatchItem],
        limits: Mapping[str, int],
        on_batch_open: Callable[[ApplyBatchItem], None] | None = None,
    ) -> tuple[tuple[_AppliedBatchResult, ...], tuple[_FailedApplyItem, ...]]:
        batches = _build_apply_batches(
            items,
            max_files_per_transaction=int(limits["max_files_per_transaction"]),
            max_records_per_transaction=int(limits["max_records_per_transaction"]),
            commit_interval_ms=int(limits["commit_interval_ms"]),
        )
        results: list[_AppliedBatchResult] = []
        failed_items: list[_FailedApplyItem] = []
        for batch in batches:
            degraded_results, degraded_failures = self._apply_batch_with_degradation(
                batch,
                apply_batch=lambda current_batch: self._apply_one_vector_batch(
                    vector_writer,
                    batch=current_batch,
                    on_batch_open=on_batch_open,
                ),
            )
            results.extend(degraded_results)
            failed_items.extend(degraded_failures)
        return tuple(results), tuple(failed_items)

    def _apply_batch_with_degradation(
        self,
        batch: ApplyBatch,
        *,
        apply_batch: Callable[[ApplyBatch], _AppliedBatchResult],
    ) -> tuple[tuple[_AppliedBatchResult, ...], tuple[_FailedApplyItem, ...]]:
        try:
            return (apply_batch(batch),), ()
        except Exception as exc:
            if len(batch.items) == 1:
                return (), (_FailedApplyItem(item=batch.items[0], error=exc),)
            midpoint = max(1, len(batch.items) // 2)
            left_batch = replace(batch, items=batch.items[:midpoint])
            right_batch = replace(batch, items=batch.items[midpoint:])
            left_results, left_failures = self._apply_batch_with_degradation(
                left_batch,
                apply_batch=apply_batch,
            )
            right_results, right_failures = self._apply_batch_with_degradation(
                right_batch,
                apply_batch=apply_batch,
            )
            return (
                left_results + right_results,
                left_failures + right_failures,
            )

    def _apply_one_metadata_batch(
        self,
        reader: object,
        writer: object,
        *,
        job_id: str | None,
        snapshot_id: str,
        batch: ApplyBatch,
        on_batch_open: Callable[[ApplyBatchItem], None] | None = None,
    ) -> _AppliedBatchResult:
        if on_batch_open is not None and batch.items:
            on_batch_open(batch.items[0])
        started_at = time.perf_counter()
        with writer.transaction():
            for item in batch.items:
                self._apply_metadata_batch_item(
                    reader,
                    writer,
                    job_id=job_id,
                    snapshot_id=snapshot_id,
                    item=item,
                )
        elapsed_seconds = time.perf_counter() - started_at
        per_item_elapsed = (
            tuple(elapsed_seconds / len(batch.items) for _ in batch.items)
            if batch.items
            else ()
        )
        return _AppliedBatchResult(
            batch=batch,
            elapsed_seconds=elapsed_seconds,
            item_elapsed_seconds=per_item_elapsed,
        )

    def _apply_one_vector_batch(
        self,
        vector_writer: object,
        *,
        batch: ApplyBatch,
        on_batch_open: Callable[[ApplyBatchItem], None] | None = None,
    ) -> _AppliedBatchResult:
        if on_batch_open is not None and batch.items:
            on_batch_open(batch.items[0])
        started_at = time.perf_counter()
        self._apply_vector_batch(vector_writer, batch=batch)
        elapsed_seconds = time.perf_counter() - started_at
        per_item_elapsed = (
            tuple(elapsed_seconds / len(batch.items) for _ in batch.items)
            if batch.items
            else ()
        )
        return _AppliedBatchResult(
            batch=batch,
            elapsed_seconds=elapsed_seconds,
            item_elapsed_seconds=per_item_elapsed,
        )

    def _apply_metadata_batch_item(
        self,
        reader: object,
        writer: object,
        *,
        job_id: str | None,
        snapshot_id: str,
        item: ApplyBatchItem,
    ) -> None:
        if item.operation == "delete":
            self._tombstone_deleted_path(
                reader,
                writer,
                job_id=job_id,
                relative_path=item.storage_relative_path or item.relative_path,
                snapshot_id=snapshot_id,
                reason="incremental_delete",
            )
            return
        if item.source_kind == "code":
            assert item.new_bundle is not None
            self._apply_code_bundle(
                reader,
                writer,
                job_id=job_id,
                relative_path=item.relative_path,
                new_bundle=item.new_bundle,
                snapshot_id=snapshot_id,
            )
            return
        if item.source_kind == "doc":
            assert item.new_bundle is not None
            self._apply_doc_bundle(
                reader,
                writer,
                job_id=job_id,
                relative_path=item.storage_relative_path or item.relative_path,
                new_bundle=item.new_bundle,
                snapshot_id=snapshot_id,
            )
            return
        raise ValueError(
            f"unsupported metadata apply batch item: {item.source_kind}:{item.operation}"
        )

    def _apply_vector_batch(self, vector_writer: object, *, batch: ApplyBatch) -> None:
        vector_writer.upsert_vectors(
            vector_write
            for item in batch.items
            for vector_write in item.vector_writes
        )

    def _apply_code_bundle(
        self,
        reader: object,
        writer: object,
        *,
        job_id: str | None = None,
        relative_path: str,
        new_bundle: _ObjectBundle,
        snapshot_id: str,
    ) -> None:
        old_bundle = _load_live_bundle(reader, relative_path=relative_path, snapshot_id=snapshot_id)
        with writer.transaction():
            if old_bundle is not None:
                self._diff_and_mark_stale(
                    writer,
                    job_id=job_id,
                    old_bundle=old_bundle,
                    new_bundle=new_bundle,
                    snapshot_id=snapshot_id,
                    reason="incremental_replace",
                )
            writer.upsert_file(new_bundle.file_record)
            writer.upsert_chunks(new_bundle.chunks)
            writer.upsert_entities(new_bundle.entities)
            writer.upsert_relations(new_bundle.relations)
            writer.upsert_evidence_records(new_bundle.evidence)

    def _apply_doc_bundle(
        self,
        reader: object,
        writer: object,
        *,
        job_id: str | None = None,
        relative_path: str,
        new_bundle: _ObjectBundle,
        snapshot_id: str,
    ) -> None:
        old_bundle = _load_live_bundle(reader, relative_path=relative_path, snapshot_id=snapshot_id)
        with writer.transaction():
            if old_bundle is not None:
                self._diff_and_mark_stale(
                    writer,
                    job_id=job_id,
                    old_bundle=old_bundle,
                    new_bundle=new_bundle,
                    snapshot_id=snapshot_id,
                    reason="incremental_replace",
                )
            writer.upsert_file(new_bundle.file_record)
            writer.upsert_chunks(new_bundle.chunks)
            writer.upsert_entities(new_bundle.entities)
            writer.upsert_evidence_records(new_bundle.evidence)

    def _upsert_doc_vectors(
        self,
        vector_writer: object,
        indexed: IndexedDocuments,
        *,
        relative_path: str,
    ) -> None:
        chunk_paths = {
            record.chunk_id: _chunk_relative_path(record) for record in indexed.chunk_records
        }
        vector_writer.upsert_vectors(
            (write.record, write.embedding)
            for write in indexed.vector_writes
            if chunk_paths.get(write.record.object_id) == relative_path
        )

    def _tombstone_deleted_path(
        self,
        reader: object,
        writer: object,
        *,
        job_id: str | None = None,
        relative_path: str,
        snapshot_id: str,
        reason: str,
    ) -> None:
        bundle = _load_live_bundle(reader, relative_path=relative_path, snapshot_id=snapshot_id)
        if bundle is None:
            return
        with writer.transaction():
            writer.tombstone_file(
                bundle.file_record.file_id,
                scope=QueryScope(snapshot_id=snapshot_id),
                reason=reason,
                created_by_job=_resolve_created_by_job(job_id),
            )

    def _rebuild_profile_conditioned_relations(
        self,
        *,
        reader: object,
        writer: object,
        job_id: str | None = None,
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
                job_id=job_id,
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
        job_id: str | None = None,
        old_bundle: _ObjectBundle,
        new_bundle: _ObjectBundle,
        snapshot_id: str,
        reason: str,
    ) -> None:
        created_by_job = _resolve_created_by_job(job_id)
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
                    created_by_job=created_by_job,
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
                    created_by_job=created_by_job,
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
                    created_by_job=created_by_job,
                    baseline_id=old_entity.entity_id,
                )
            if old_entity.entity_id not in new_entity_ids:
                self._tombstone_object(
                    writer,
                    job_id=job_id,
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
                    created_by_job=created_by_job,
                    baseline_id=old_relation.relation_id,
                )
            if old_relation.relation_id not in new_relation_ids:
                self._tombstone_object(
                    writer,
                    job_id=job_id,
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
        job_id: str | None = None,
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
                created_by_job=_resolve_created_by_job(job_id),
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


def _resolve_created_by_job(job_id: str | None) -> str:
    if isinstance(job_id, str) and job_id:
        return job_id
    return INCREMENTAL_INDEX_CREATED_BY_JOB_FALLBACK


def _diff_maps(
    previous: Mapping[str, str],
    current: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    changed = tuple(sorted(path for path, value in current.items() if previous.get(path) != value))
    deleted = tuple(sorted(path for path in previous if path not in current))
    return changed, deleted


def _next_running_status(current: str) -> JobStatus:
    if current == "pending":
        return _RUNNING_JOB_STATUS_ORDER[0]
    current_index = _RUNNING_JOB_STATUS_ORDER.index(current)
    if current_index + 1 >= len(_RUNNING_JOB_STATUS_ORDER):
        return _RUNNING_JOB_STATUS_ORDER[-1]
    return _RUNNING_JOB_STATUS_ORDER[current_index + 1]


def _index_task_counts(tasks: Sequence[IndexTask]) -> dict[str, object]:
    by_phase: dict[str, int] = {}
    by_source_kind: dict[str, int] = {}
    required = 0
    for task in tasks:
        by_phase[task.phase] = by_phase.get(task.phase, 0) + 1
        by_source_kind[task.source_kind] = by_source_kind.get(task.source_kind, 0) + 1
        if task.required:
            required += 1
    return {
        "total": len(tasks),
        "required": required,
        "by_phase": dict(sorted(by_phase.items())),
        "by_source_kind": dict(sorted(by_source_kind.items())),
    }


def _tasks_with_collect_dependency(
    tasks: Sequence[IndexTask],
    *,
    prefix: str,
) -> tuple[IndexTask, ...]:
    return tuple(
        task
        for task in tasks
        if any(dependency.startswith(prefix) for dependency in task.collect_dependencies)
    )


def _record_collect_artifact_checkpoints(
    *,
    run_context: IndexRunContext | None,
    tasks: Sequence[IndexTask],
    artifact: CollectArtifactRef,
    plan_signature: str,
) -> None:
    if run_context is None:
        return
    for task in tasks:
        record_task_collected_checkpoint(
            run_context.job_store,
            run_context.job_id,
            task,
            metadata={
                "job_id": run_context.job_id,
                "plan_signature": plan_signature,
                "artifact_ref": str(artifact.path),
                "artifact_hash": artifact.artifact_hash,
                "collected_paths": list(artifact.collect_paths),
            },
        )


def _collect_artifact_metadata(
    *,
    status: str,
    artifact: CollectArtifactRef,
) -> dict[str, object]:
    return {
        "status": status,
        "artifact_path": str(artifact.path),
        "artifact_hash": artifact.artifact_hash,
        "path_count": len(artifact.collect_paths),
        "task_count": len(artifact.task_keys),
    }


def _index_plan_summary(plan: IncrementalIndexPlan) -> dict[str, object]:
    return {
        "snapshot_id": plan.snapshot_id,
        "source": plan.source,
        "has_previous_state": plan.previous_state is not None,
        "reindex_all_code": plan.reindex_all_code,
        "reindex_all_docs": plan.reindex_all_docs,
        "rebuild_vectors": plan.rebuild_vectors,
        "rebuild_profile_conditioned_relations": (plan.rebuild_profile_conditioned_relations),
        "changed_code_paths_count": len(plan.changed_code_paths),
        "deleted_code_paths_count": len(plan.deleted_code_paths),
        "changed_doc_paths_count": len(plan.changed_doc_paths),
        "deleted_doc_paths_count": len(plan.deleted_doc_paths),
        "changed_profile_ids_count": len(plan.changed_profile_ids),
        "removed_profile_ids_count": len(plan.removed_profile_ids),
        "warnings_count": len(plan.warnings),
    }


def _has_warning_code(
    warnings: Sequence[object],
    code: str,
) -> bool:
    return any(getattr(warning, "code", None) == code for warning in warnings)


def _task_has_matching_applied_checkpoint(
    checkpoints: Mapping[str, str],
    task: IndexTask,
    *,
    plan_signature: str,
    job_plan_signature: str | None,
) -> bool:
    checkpoint = decode_task_checkpoint(checkpoints.get(task_checkpoint_key(task)))
    if (
        checkpoint is None
        or checkpoint.status != "applied"
        or checkpoint.task_key != task.task_key
        or checkpoint.phase != task.phase
        or checkpoint.input_hash != task.input_hash
        or checkpoint.task_schema_version != task.schema_version
    ):
        return False
    checkpoint_plan_signature = checkpoint.metadata.get("plan_signature")
    if isinstance(checkpoint_plan_signature, str):
        return checkpoint_plan_signature == plan_signature
    return job_plan_signature == plan_signature


def _collect_dependency_paths(tasks: Sequence[IndexTask], *, prefix: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                dependency.removeprefix(prefix)
                for task in tasks
                for dependency in task.collect_dependencies
                if dependency.startswith(prefix)
            }
        )
    )


def _code_collect_paths_after_skips(
    plan: IncrementalIndexPlan,
    skipped_tasks: Sequence[IndexTask],
) -> tuple[str, ...]:
    skipped_apply_inputs = {
        task.relative_path
        for task in skipped_tasks
        if task.source_kind == "code" and task.operation == "apply" and task.relative_path
    }
    return tuple(
        path
        for path in _incremental_code_paths_to_collect(plan)
        if path not in skipped_apply_inputs
    )


def _progress_totals_from_tasks(
    plan: IncrementalIndexPlan,
    source: Literal["all", "code", "docs"],
    tasks: Sequence[IndexTask],
    *,
    code_paths_to_collect: Sequence[str],
    doc_paths_to_collect: Sequence[str],
) -> dict[str, int]:
    code_collect = 0
    if source in {"all", "code"} and (
        plan.changed_code_paths or plan.deleted_code_paths or plan.reindex_all_code
    ):
        code_collect = count_indexable_workspace_files(
            plan.workspace_inventory,
            include_paths=code_paths_to_collect,
        )

    phase_counts: dict[str, int] = {}
    for task in tasks:
        phase_counts[task.phase] = phase_counts.get(task.phase, 0) + 1

    totals = {
        "code_collect": code_collect,
        "code_apply": phase_counts.get("code_apply", 0),
        "doc_collect": (
            len(doc_paths_to_collect)
            if source in {"all", "docs"}
            and (
                plan.changed_doc_paths
                or plan.deleted_doc_paths
                or plan.reindex_all_docs
                or plan.rebuild_vectors
            )
            else 0
        ),
        "doc_apply": phase_counts.get("doc_apply", 0),
        "vectors_apply": phase_counts.get("vectors_apply", 0),
        "profile_relations": phase_counts.get("profile_relations", 0),
        "workspace_map": phase_counts.get("workspace_map", 0),
    }
    totals["global_total"] = 1 + sum(totals.values())
    return totals


def _resume_policy_metadata(
    resume_policy: Mapping[str, object] | None,
) -> dict[str, object]:
    if resume_policy is None:
        return {}
    return {
        "resume_policy": {
            str(key): value for key, value in resume_policy.items() if value is not None
        }
    }


def _writer_apply_batch_limits(config: ActiveKnowledgeConfig) -> dict[str, int]:
    return {
        "max_files_per_transaction": config.indexing.writer.max_files_per_transaction,
        "max_records_per_transaction": config.indexing.writer.max_records_per_transaction,
        "commit_interval_ms": config.indexing.writer.commit_interval_ms,
    }


def _empty_apply_batch_metadata(limits: Mapping[str, int]) -> dict[str, object]:
    return {
        "configured": {
            "max_files_per_transaction": int(limits["max_files_per_transaction"]),
            "max_records_per_transaction": int(limits["max_records_per_transaction"]),
            "commit_interval_ms": int(limits["commit_interval_ms"]),
        },
        "by_phase": {},
    }


def _record_apply_batch(
    result_metadata: dict[str, object],
    batch: ApplyBatch,
) -> None:
    apply_batches = result_metadata.get("apply_batches")
    assert isinstance(apply_batches, dict)
    by_phase = apply_batches.get("by_phase")
    assert isinstance(by_phase, dict)
    phase_stats = by_phase.get(batch.phase)
    if not isinstance(phase_stats, dict):
        phase_stats = {
            "batches": 0,
            "items": 0,
            "records": 0,
            "max_items_in_batch": 0,
            "max_records_in_batch": 0,
        }
    phase_stats["batches"] = int(phase_stats.get("batches", 0)) + 1
    phase_stats["items"] = int(phase_stats.get("items", 0)) + len(batch.items)
    phase_stats["records"] = int(phase_stats.get("records", 0)) + batch.record_count
    phase_stats["max_items_in_batch"] = max(
        int(phase_stats.get("max_items_in_batch", 0)),
        len(batch.items),
    )
    phase_stats["max_records_in_batch"] = max(
        int(phase_stats.get("max_records_in_batch", 0)),
        batch.record_count,
    )
    by_phase[batch.phase] = phase_stats


def _delete_checkpoint_metadata(
    *,
    source_kind: Literal["code", "doc"],
    relative_path: str,
    storage_relative_path: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "operation": "delete",
        "source_kind": source_kind,
        "relative_path": relative_path,
        "record_counts": {
            "files": 1,
            "chunks": 0,
            "entities": 0,
            "relations": 0,
            "evidence": 0,
            "vector_refs": 0,
        },
        "warning_codes": [],
    }
    if storage_relative_path is not None:
        metadata["storage_relative_path"] = storage_relative_path
    return metadata


def _record_counts_total(record_counts: Mapping[str, object]) -> int:
    total = 0
    for value in record_counts.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            total += value
    return total


def _bundle_checkpoint_metadata(
    bundle: _ObjectBundle,
    *,
    source_kind: Literal["code", "doc"],
    relative_path: str,
    storage_relative_path: str | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "operation": "apply",
        "source_kind": source_kind,
        "relative_path": relative_path,
        "record_counts": {
            "files": 1,
            "chunks": len(bundle.chunks),
            "entities": len(bundle.entities),
            "relations": len(bundle.relations),
            "evidence": len(bundle.evidence),
            "vector_refs": len(bundle.vectors),
        },
        "warning_codes": list(warnings),
    }
    if storage_relative_path is not None:
        metadata["storage_relative_path"] = storage_relative_path
    return metadata


def _vector_checkpoint_metadata(
    indexed: IndexedDocuments,
    *,
    relative_path: str,
    storage_relative_path: str,
) -> dict[str, object]:
    writes = _vector_writes_for_path(indexed, storage_relative_path=storage_relative_path)
    records = tuple(record for record, _embedding in writes)
    embedding_models = sorted({record.embedding_model_version for record in records})
    return {
        "operation": "doc",
        "source_kind": "vector",
        "relative_path": relative_path,
        "storage_relative_path": storage_relative_path,
        "record_counts": {
            "files": 0,
            "chunks": len({record.object_id for record in records}),
            "entities": 0,
            "relations": 0,
            "evidence": 0,
            "vector_refs": len(writes),
        },
        "embedding_models": embedding_models,
        "vector_ref_ids": [record.vector_ref_id for record in records],
        "warning_codes": list(_warning_codes_for_path(indexed.warnings, storage_relative_path)),
    }


def _vector_writes_for_path(
    indexed: IndexedDocuments,
    *,
    storage_relative_path: str,
) -> tuple[tuple[VectorRefRecord, tuple[float, ...]], ...]:
    chunk_paths = {
        record.chunk_id: _chunk_relative_path(record) for record in indexed.chunk_records
    }
    return tuple(
        (write.record, write.embedding)
        for write in indexed.vector_writes
        if chunk_paths.get(write.record.object_id) == storage_relative_path
    )


def _warning_codes_for_path(warnings: Sequence[object], relative_path: str) -> tuple[str, ...]:
    codes = {
        str(code)
        for warning in warnings
        if getattr(warning, "relative_path", None) == relative_path
        for code in (getattr(warning, "code", None),)
        if code is not None
    }
    return tuple(sorted(codes))


def _task_apply_failure_warning(
    item: ApplyBatchItem,
    error: Exception,
) -> IncrementalIndexWarning:
    code_by_phase = {
        "code_apply": "index.code_apply_failed",
        "doc_apply": "index.doc_apply_failed",
        "vectors_apply": "index.vector_apply_failed",
    }
    message_by_phase = {
        "code_apply": "One code apply task failed; other code tasks continued.",
        "doc_apply": "One document apply task failed; other document tasks continued.",
        "vectors_apply": "One vector apply task failed; other vector tasks continued.",
    }
    return IncrementalIndexWarning(
        code=code_by_phase.get(item.phase, "index.apply_task_failed"),
        message=message_by_phase.get(
            item.phase,
            "One incremental apply task failed; other tasks continued.",
        ),
        details={
            "path": item.relative_path,
            "error": str(error),
        },
    )


def _find_index_task(tasks: Sequence[IndexTask], task_key: str) -> IndexTask | None:
    for task in tasks:
        if task.task_key == task_key:
            return task
    return None


def _incremental_doc_paths_to_collect(plan: IncrementalIndexPlan) -> tuple[str, ...]:
    doc_paths_to_collect = set(plan.changed_doc_paths)
    if plan.reindex_all_docs or (plan.rebuild_vectors and not doc_paths_to_collect):
        doc_paths_to_collect.update(plan.current_state.doc_files)
    return tuple(sorted(doc_paths_to_collect))


def _incremental_code_paths_to_collect(plan: IncrementalIndexPlan) -> tuple[str, ...]:
    if plan.reindex_all_code:
        return tuple(sorted(plan.current_state.code_files))
    if not plan.changed_code_paths:
        return ()
    paths = set(plan.changed_code_paths)
    paths.update(
        path
        for path in plan.current_state.code_files
        if Path(path).name == "Makefile" or Path(path).suffix == ".mk"
    )
    return tuple(sorted(paths))


def _incremental_progress_totals(
    plan: IncrementalIndexPlan,
    source: Literal["all", "code", "docs"],
    doc_paths_to_collect: Sequence[str],
) -> dict[str, int]:
    code_collect = 0
    code_apply = 0
    if source in {"all", "code"} and (
        plan.changed_code_paths or plan.deleted_code_paths or plan.reindex_all_code
    ):
        code_include_paths = (
            None if plan.reindex_all_code else _incremental_code_paths_to_collect(plan)
        )
        code_collect = count_indexable_workspace_files(
            plan.workspace_inventory,
            include_paths=code_include_paths,
        )
        code_apply = len(plan.changed_code_paths) + len(plan.deleted_code_paths)

    doc_collect = 0
    doc_apply = 0
    vectors_apply = 0
    if source in {"all", "docs"} and (
        plan.changed_doc_paths
        or plan.deleted_doc_paths
        or plan.reindex_all_docs
        or plan.rebuild_vectors
    ):
        doc_collect = len(doc_paths_to_collect)
        doc_apply = len(plan.deleted_doc_paths) + sum(
            1
            for path in doc_paths_to_collect
            if plan.reindex_all_docs or path in plan.changed_doc_paths
        )
        vectors_apply = sum(
            1
            for path in doc_paths_to_collect
            if plan.rebuild_vectors or plan.reindex_all_docs or path in plan.changed_doc_paths
        )

    profile_relations = int(
        source in {"all", "code"} and plan.rebuild_profile_conditioned_relations
    )
    workspace_map = int(_workspace_map_refresh_required(plan))
    global_total = (
        1
        + code_collect
        + code_apply
        + doc_collect
        + doc_apply
        + vectors_apply
        + profile_relations
        + workspace_map
    )
    return {
        "code_collect": code_collect,
        "code_apply": code_apply,
        "doc_collect": doc_collect,
        "doc_apply": doc_apply,
        "vectors_apply": vectors_apply,
        "profile_relations": profile_relations,
        "workspace_map": workspace_map,
        "global_total": global_total,
    }


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


def _format_discover_counts(files_scanned: int, directories_scanned: int) -> str:
    file_label = "file" if files_scanned == 1 else "files"
    directory_label = "directory" if directories_scanned == 1 else "directories"
    return f"{files_scanned} {file_label}, {directories_scanned} {directory_label}"


def _format_discover_target(
    relative_path: str,
    *,
    root_label: str,
    kind: Literal["directory", "file"],
) -> str:
    if relative_path in {"", "."}:
        return root_label
    parts = PurePosixPath(relative_path).parts
    if kind == "directory":
        summary_parts = parts[-2:]
    elif len(parts) <= 3:
        summary_parts = parts
    else:
        summary_parts = parts[-3:]
    return "/".join(summary_parts) or root_label


def _format_workspace_discover_message(progress: WorkspaceScanProgress) -> str:
    target = _format_discover_target(
        progress.relative_path,
        root_label="workspace root",
        kind=progress.kind,
    )
    counts = _format_discover_counts(progress.files_scanned, progress.directories_scanned)
    return f"Scanning workspace inventory: {target} ({counts})"


def _format_source_docs_discover_message(progress: SourceDocsScanProgress) -> str:
    target = _format_discover_target(
        progress.relative_path,
        root_label="source docs root",
        kind=progress.kind,
    )
    counts = _format_discover_counts(progress.files_scanned, progress.directories_scanned)
    return f"Scanning source documents: {target} ({counts})"


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


def _merge_result_section(
    result_metadata: dict[str, object],
    *,
    section_name: str,
    section_metadata: Mapping[str, object],
) -> None:
    timings = result_metadata.get("timings")
    assert isinstance(timings, dict)
    diagnostics = result_metadata.get("diagnostics")
    assert isinstance(diagnostics, dict)
    result_metadata[f"{section_name}_metadata"] = dict(section_metadata)
    section_timings = section_metadata.get("timings", {})
    if isinstance(section_timings, Mapping):
        timings["parser_seconds"] = round(
            float(timings.get("parser_seconds", 0.0))
            + float(section_timings.get("parser_seconds", 0.0)),
            6,
        )
        timings["embedding_seconds"] = round(
            float(timings.get("embedding_seconds", 0.0))
            + float(section_timings.get("embedding_seconds", 0.0)),
            6,
        )
    section_diagnostics = section_metadata.get("diagnostics", {})
    if isinstance(section_diagnostics, Mapping):
        slowest = section_diagnostics.get("slowest_items", ())
        if isinstance(slowest, Sequence):
            existing = diagnostics.get("slowest_items", [])
            assert isinstance(existing, list)
            existing.extend(dict(item) for item in slowest if isinstance(item, Mapping))
            diagnostics["slowest_items"] = list(_top_slowest_items(existing))


def _record_elapsed(
    result_metadata: dict[str, object],
    *,
    timing_key: str,
    elapsed_seconds: float,
    path: str,
    stage: str,
) -> None:
    timings = result_metadata.get("timings")
    assert isinstance(timings, dict)
    timings[timing_key] = round(float(timings.get(timing_key, 0.0)) + elapsed_seconds, 6)
    diagnostics = result_metadata.get("diagnostics")
    assert isinstance(diagnostics, dict)
    existing = diagnostics.get("slowest_items", [])
    assert isinstance(existing, list)
    existing.append(
        {
            "path": path,
            "stage": stage,
            "elapsed_seconds": round(elapsed_seconds, 6),
        }
    )
    diagnostics["slowest_items"] = list(_top_slowest_items(existing))


def _add_timing(
    result_metadata: dict[str, object],
    *,
    timing_key: str,
    elapsed_seconds: float,
) -> None:
    if elapsed_seconds <= 0:
        return
    timings = result_metadata.get("timings")
    assert isinstance(timings, dict)
    timings[timing_key] = round(float(timings.get(timing_key, 0.0)) + elapsed_seconds, 6)


def _build_apply_batches(
    items: Sequence[ApplyBatchItem],
    *,
    max_files_per_transaction: int,
    max_records_per_transaction: int,
    commit_interval_ms: int,
) -> tuple[ApplyBatch, ...]:
    if not items:
        return ()
    batches: list[ApplyBatch] = []
    current_items: list[ApplyBatchItem] = []
    current_records = 0
    for item in items:
        item_records = max(1, item.record_total)
        if current_items and (
            len(current_items) >= max_files_per_transaction
            or current_records + item_records > max_records_per_transaction
        ):
            batches.append(
                ApplyBatch(
                    phase=current_items[0].phase,
                    items=tuple(current_items),
                    max_files_per_transaction=max_files_per_transaction,
                    max_records_per_transaction=max_records_per_transaction,
                    commit_interval_ms=commit_interval_ms,
                )
            )
            current_items = []
            current_records = 0
        current_items.append(item)
        current_records += item_records
    if current_items:
        batches.append(
            ApplyBatch(
                phase=current_items[0].phase,
                items=tuple(current_items),
                max_files_per_transaction=max_files_per_transaction,
                max_records_per_transaction=max_records_per_transaction,
                commit_interval_ms=commit_interval_ms,
            )
        )
    return tuple(batches)


def _top_slowest_items(
    items: Sequence[Mapping[str, object]],
    *,
    limit: int = 5,
) -> tuple[dict[str, object], ...]:
    ranked = sorted(
        (dict(item) for item in items if isinstance(item.get("elapsed_seconds"), (int, float))),
        key=lambda item: float(item["elapsed_seconds"]),
        reverse=True,
    )
    return tuple(ranked[:limit])
