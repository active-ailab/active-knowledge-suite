"""Stable storage contracts for metadata, logical views, and write guards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from active_knowledge_server.config.schema import ActiveKnowledgeConfig

StorageScalar: TypeAlias = str | int | float | bool | None
StorageValue: TypeAlias = StorageScalar | list["StorageValue"] | dict[str, "StorageValue"]
StorageMetadata: TypeAlias = dict[str, StorageValue]
StorageObjectType = Literal[
    "source",
    "snapshot",
    "profile",
    "file",
    "chunk",
    "entity",
    "relation",
    "evidence",
    "job",
    "fts_row",
    "vector_ref",
]
StorageSourceIndex = Literal["baseline", "overlay", "merged"]
StorageFTSTable = Literal["chunk_fts", "entity_fts", "doc_fts", "code_fts"]
StorageWriteTarget = Literal["overlay", "baseline"]
StorageOperationMode = Literal["normal", "baseline_publish"]
StorageWarningLevel = Literal["info", "caution", "degraded", "blocked"]
JobStatus = Literal[
    "pending",
    "discovering",
    "parsing",
    "extracting",
    "embedding",
    "reporting",
    "ready",
    "failed",
    "partial_ready",
]

ALL_SCOPE = "all"


@dataclass(frozen=True)
class QueryScope:
    """Logical query scope shared by metadata, FTS, and vector filtering."""

    snapshot_id: str = "current"
    profile_id: str = ALL_SCOPE
    source_scope: str = ALL_SCOPE
    path_scope: str | None = None
    include_inactive: bool = False


@dataclass(frozen=True)
class FTSQuery:
    """Stable full-text search request understood by the storage layer."""

    index_name: StorageFTSTable
    query: str
    scope: QueryScope = field(default_factory=QueryScope)
    top_k: int = 12
    domain: str | None = None
    doc_type: str | None = None
    source_index: StorageSourceIndex | None = None


@dataclass(frozen=True)
class FTSMatch:
    """One merged FTS candidate after logical-view filtering."""

    index_name: StorageFTSTable
    logical_object_id: str
    physical_object_id: str
    object_type: Literal["chunk", "entity"]
    source_index: StorageSourceIndex
    score: float
    match_source: Literal["fts"] = "fts"
    file_id: str | None = None
    relative_path: str | None = None
    chunk_id: str | None = None
    entity_id: str | None = None
    profile_id: str = ALL_SCOPE
    source_scope: str = ALL_SCOPE
    domain: str | None = None
    doc_type: str | None = None
    title: str | None = None
    snippet: str | None = None
    replaced_from: tuple[str, ...] = ()
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class StorageWarning:
    """One structured storage-layer warning that can flow into query diagnostics."""

    code: str
    message: str
    level: StorageWarningLevel = "degraded"
    details: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class VectorRefRecord:
    """One metadata-side reference to a vector payload stored out-of-band."""

    vector_ref_id: str
    object_type: Literal["chunk", "entity", "evidence"]
    object_id: str
    chunk_id: str | None
    embedding_model_version: str
    content_hash: str
    source_scope: str = ALL_SCOPE
    profile_id: str = ALL_SCOPE
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class VectorQuery:
    """Stable semantic-search request understood by the vector storage layer."""

    embedding: tuple[float, ...]
    scope: QueryScope = field(default_factory=QueryScope)
    top_k: int = 12
    object_types: tuple[Literal["chunk", "entity", "evidence"], ...] = ("chunk",)
    embedding_model_version: str | None = None
    source_index: StorageSourceIndex | None = None


@dataclass(frozen=True)
class VectorMatch:
    """One merged vector candidate after logical-view filtering."""

    logical_object_id: str
    physical_object_id: str
    vector_ref_id: str
    object_type: Literal["chunk", "entity", "evidence"]
    source_index: StorageSourceIndex
    score: float
    embedding_model_version: str
    content_hash: str
    chunk_id: str | None = None
    profile_id: str = ALL_SCOPE
    source_scope: str = ALL_SCOPE
    match_source: Literal["vector"] = "vector"
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class VectorSearchResult:
    """One vector search response with candidates and degradations."""

    matches: tuple[VectorMatch, ...] = ()
    warnings: tuple[StorageWarning, ...] = ()


@dataclass(frozen=True)
class SourceRecord:
    """One physical source root or document collection."""

    source_id: str
    source_type: str
    display_name: str
    root_path: str
    revision: str | None = None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotRecord:
    """One indexed code/document snapshot."""

    snapshot_id: str
    workspace_revision: str
    baseline_id: str | None = None
    manifest_version: str | None = None
    created_at: str | None = None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class ProfileRecord:
    """One resolved profile record bound to a snapshot."""

    profile_record_id: str
    snapshot_id: str
    profile_id: str
    defconfig_hash: str | None = None
    dotconfig_hash: str | None = None
    defconfig_path: str | None = None
    dotconfig_path: str | None = None
    app: str | None = None
    board: str | None = None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class FileRecord:
    """One physical file or document inside a snapshot."""

    file_id: str
    snapshot_id: str
    source_id: str
    relative_path: str
    content_hash: str
    source_scope: str = ALL_SCOPE
    profile_id: str = ALL_SCOPE
    language: str | None = None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class ChunkRecord:
    """One chunked text/code segment."""

    chunk_id: str
    snapshot_id: str
    file_id: str
    content_hash: str
    chunk_type: str
    ordinal: int
    text: str
    source_scope: str = ALL_SCOPE
    profile_id: str = ALL_SCOPE
    start_line: int | None = None
    end_line: int | None = None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class EntityRecord:
    """One indexed code/document entity."""

    entity_id: str
    snapshot_id: str
    file_id: str
    entity_type: str
    name: str
    qualified_name: str
    path: str
    source_scope: str = ALL_SCOPE
    profile_id: str = ALL_SCOPE
    start_line: int | None = None
    end_line: int | None = None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class RelationRecord:
    """One graph edge between entities or runtime nodes."""

    relation_id: str
    snapshot_id: str
    relation_type: str
    src_entity_id: str
    dst_entity_id: str
    source_scope: str = ALL_SCOPE
    profile_id: str = ALL_SCOPE
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceRecord:
    """One evidence item attached to a query result or entity."""

    evidence_id: str
    snapshot_id: str
    object_type: StorageObjectType
    object_id: str
    file_id: str
    source_scope: str = ALL_SCOPE
    profile_id: str = ALL_SCOPE
    chunk_id: str | None = None
    excerpt: str | None = None
    citation_label: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class JobRecord:
    """One indexing, migration, or validation job."""

    job_id: str
    job_type: str
    status: JobStatus
    write_target: StorageWriteTarget
    created_at: str
    updated_at: str
    snapshot_id: str | None = None
    profile_id: str | None = None
    error_summary: str | None = None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class TombstoneRecord:
    """Overlay-only delete/disable marker for a logical object."""

    tombstone_id: str
    object_type: StorageObjectType
    object_id: str
    reason: str
    created_by_job: str
    snapshot_id: str
    profile_id: str = ALL_SCOPE
    source_scope: str = ALL_SCOPE
    baseline_id: str | None = None
    created_at: str | None = None
    active: bool = True
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class ReplacementRecord:
    """Overlay-only replacement mapping from one logical object ID to another."""

    replacement_id: str
    object_type: StorageObjectType
    old_object_id: str
    new_object_id: str
    reason: str
    created_by_job: str
    scope: QueryScope
    baseline_id: str | None = None
    created_at: str | None = None
    active: bool = True
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class ReplacementResolution:
    """Resolved object identity after following active replacement rules."""

    requested_object_id: str
    resolved_object_id: str
    replaced: bool
    chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationValidationIssue:
    """One relation consistency issue discovered by logical validation."""

    issue_code: str
    relation_id: str
    source_index: StorageSourceIndex
    level: StorageWarningLevel
    message: str
    src_entity_id: str
    dst_entity_id: str
    resolved_src_entity_id: str | None = None
    resolved_dst_entity_id: str | None = None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class LogicalChunk:
    """Chunk returned from the merged logical view."""

    logical_object_id: str
    physical_object_id: str
    source_index: StorageSourceIndex
    record: ChunkRecord
    replaced_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class LogicalEntity:
    """Entity returned from the merged logical view."""

    logical_object_id: str
    physical_object_id: str
    source_index: StorageSourceIndex
    record: EntityRecord
    replaced_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class LogicalRelation:
    """Relation returned from the merged logical view."""

    logical_object_id: str
    physical_object_id: str
    source_index: StorageSourceIndex
    record: RelationRecord
    replaced_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class LogicalEvidence:
    """Evidence item returned from the merged logical view."""

    logical_object_id: str
    physical_object_id: str
    source_index: StorageSourceIndex
    record: EvidenceRecord
    replaced_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class StorageWriteRequest:
    """Explicit write intent used by adapters to gate baseline writes."""

    target: StorageWriteTarget
    operation_mode: StorageOperationMode = "normal"


class StorageAccessError(ValueError):
    """Raised when a write request violates storage safety rules."""


class BaselineWriteBlockedError(StorageAccessError):
    """Raised when a normal run attempts to write team-shared baseline data."""


def default_write_request(
    config: ActiveKnowledgeConfig,
    *,
    operation_mode: StorageOperationMode = "normal",
) -> StorageWriteRequest:
    """Build the default write request from validated config."""

    target: StorageWriteTarget = (
        "overlay" if config.indexing.write_target == "local_overlay" else "baseline"
    )
    request = StorageWriteRequest(target=target, operation_mode=operation_mode)
    validate_write_request(config, request)
    return request


def validate_write_request(
    config: ActiveKnowledgeConfig,
    request: StorageWriteRequest,
) -> None:
    """Enforce local-overlay defaults and explicit baseline publish rules."""

    if request.target == "baseline":
        if request.operation_mode != "baseline_publish":
            raise BaselineWriteBlockedError(
                "Baseline writes are blocked for normal runs. "
                "Use operation_mode=baseline_publish with writable baseline stores."
            )
        require_writable_store(config.storage.metadata.mode, "storage.metadata.mode")
        require_writable_store(config.storage.vector.mode, "storage.vector.mode")
    else:
        require_writable_store(config.storage.overlay.mode, "storage.overlay.mode")
        require_writable_store(config.storage.vector_delta.mode, "storage.vector_delta.mode")

    require_writable_store(config.storage.jobs.mode, "storage.jobs.mode")


def require_writable_store(mode: str, field_name: str) -> None:
    """Require a store to be writable for the requested operation."""

    if mode != "readwrite":
        raise StorageAccessError(f"{field_name} must be readwrite for this operation.")


def make_tombstone_id(
    object_type: StorageObjectType,
    object_id: str,
    *,
    scope: QueryScope,
    reason: str,
    baseline_id: str | None = None,
) -> str:
    """Return a stable tombstone ID for idempotent local delete jobs."""

    payload = {
        "baseline_id": baseline_id,
        "object_id": object_id,
        "object_type": object_type,
        "profile_id": scope.profile_id,
        "reason": reason,
        "snapshot_id": scope.snapshot_id,
        "source_scope": scope.source_scope,
    }
    digest = hashlib.sha1(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"ts:{digest}"


def make_replacement_id(
    object_type: StorageObjectType,
    old_object_id: str,
    new_object_id: str,
    *,
    scope: QueryScope,
    reason: str,
) -> str:
    """Return a stable replacement ID for idempotent local change jobs."""

    payload = {
        "new_object_id": new_object_id,
        "object_type": object_type,
        "old_object_id": old_object_id,
        "path_scope": scope.path_scope,
        "profile_id": scope.profile_id,
        "reason": reason,
        "snapshot_id": scope.snapshot_id,
        "source_scope": scope.source_scope,
    }
    digest = hashlib.sha1(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"rp:{digest}"


@runtime_checkable
class StorageReader(Protocol):
    """Read contract for physical records and merged logical views."""

    def get_source(self, source_id: str) -> SourceRecord | None:
        """Return one source record by ID."""

    def iter_sources(self) -> Iterable[SourceRecord]:
        """Iterate configured or indexed sources."""

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord | None:
        """Return one snapshot by ID."""

    def iter_snapshots(self) -> Iterable[SnapshotRecord]:
        """Iterate snapshots."""

    def get_profile(self, profile_record_id: str) -> ProfileRecord | None:
        """Return one profile record by physical profile record ID."""

    def iter_profiles(self, snapshot_id: str | None = None) -> Iterable[ProfileRecord]:
        """Iterate profiles, optionally scoped to one snapshot."""

    def get_file(self, file_id: str) -> FileRecord | None:
        """Return one file by ID."""

    def iter_files(self, scope: QueryScope) -> Iterable[FileRecord]:
        """Iterate physical files matching a scope."""

    def get_chunk(self, chunk_id: str) -> ChunkRecord | None:
        """Return one chunk by ID."""

    def iter_chunks(self, scope: QueryScope) -> Iterable[ChunkRecord]:
        """Iterate physical chunks matching a scope."""

    def get_entity(self, entity_id: str) -> EntityRecord | None:
        """Return one entity by ID."""

    def iter_entities(self, scope: QueryScope) -> Iterable[EntityRecord]:
        """Iterate physical entities matching a scope."""

    def get_relation(self, relation_id: str) -> RelationRecord | None:
        """Return one relation by ID."""

    def iter_relations(self, scope: QueryScope) -> Iterable[RelationRecord]:
        """Iterate physical relations matching a scope."""

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        """Return one evidence record by ID."""

    def iter_evidence(self, scope: QueryScope) -> Iterable[EvidenceRecord]:
        """Iterate physical evidence matching a scope."""

    def get_vector_ref(self, vector_ref_id: str) -> VectorRefRecord | None:
        """Return one vector reference record by ID."""

    def iter_vector_refs(self, scope: QueryScope) -> Iterable[VectorRefRecord]:
        """Iterate physical vector references matching a scope."""

    def get_job(self, job_id: str) -> JobRecord | None:
        """Return one job by ID."""

    def iter_jobs(self, status: str | None = None) -> Iterable[JobRecord]:
        """Iterate jobs, optionally filtered by status."""

    def logical_chunks(self, scope: QueryScope) -> Iterable[LogicalChunk]:
        """Iterate merged chunks after tombstone and replacement filtering."""

    def logical_entities(self, scope: QueryScope) -> Iterable[LogicalEntity]:
        """Iterate merged entities after tombstone and replacement filtering."""

    def logical_relations(self, scope: QueryScope) -> Iterable[LogicalRelation]:
        """Iterate merged relations with endpoint replacement resolution applied."""

    def logical_evidence(self, scope: QueryScope) -> Iterable[LogicalEvidence]:
        """Iterate merged evidence after tombstone and replacement filtering."""

    def validate_relations(self, scope: QueryScope) -> Iterable[RelationValidationIssue]:
        """Return relation consistency issues such as orphan endpoints."""

    def resolve_replacement(
        self,
        object_type: StorageObjectType,
        object_id: str,
        scope: QueryScope,
    ) -> ReplacementResolution:
        """Resolve one logical object ID through active replacement mappings."""

    def is_tombstoned(
        self,
        object_type: StorageObjectType,
        object_id: str,
        scope: QueryScope,
    ) -> bool:
        """Return whether one logical object is hidden by an active tombstone."""

    def search_fts(self, request: FTSQuery) -> Iterable[FTSMatch]:
        """Run merged FTS search over baseline and overlay logical views."""


@runtime_checkable
class StorageWriter(Protocol):
    """Write contract for metadata objects, tombstones, replacements, and jobs."""

    @property
    def request(self) -> StorageWriteRequest:
        """Return the explicit write intent bound to this writer."""

    def upsert_source(self, record: SourceRecord) -> None:
        """Insert or update one source record."""

    def upsert_snapshot(self, record: SnapshotRecord) -> None:
        """Insert or update one snapshot record."""

    def upsert_profile(self, record: ProfileRecord) -> None:
        """Insert or update one profile record."""

    def upsert_file(self, record: FileRecord) -> None:
        """Insert or update one file record."""

    def upsert_chunk(self, record: ChunkRecord) -> None:
        """Insert or update one chunk record."""

    def upsert_entity(self, record: EntityRecord) -> None:
        """Insert or update one entity record."""

    def upsert_relation(self, record: RelationRecord) -> None:
        """Insert or update one relation record."""

    def upsert_evidence(self, record: EvidenceRecord) -> None:
        """Insert or update one evidence record."""

    def upsert_vector_ref(self, record: VectorRefRecord) -> None:
        """Insert or update one vector reference record."""

    def upsert_job(self, record: JobRecord) -> None:
        """Insert or update one job record."""

    def upsert_tombstone(self, record: TombstoneRecord) -> None:
        """Insert or update one tombstone record."""

    def upsert_replacement(self, record: ReplacementRecord) -> None:
        """Insert or update one replacement record."""

    def tombstone_file(
        self,
        file_id: str,
        *,
        scope: QueryScope,
        reason: str,
        created_by_job: str,
    ) -> tuple[TombstoneRecord, ...]:
        """Write overlay tombstones for a file and its indexed dependents."""

    def tombstone_chunk(
        self,
        chunk_id: str,
        *,
        scope: QueryScope,
        reason: str,
        created_by_job: str,
    ) -> tuple[TombstoneRecord, ...]:
        """Write overlay tombstones for a chunk and attached evidence/vectors."""

    def replace_object(
        self,
        object_type: StorageObjectType,
        old_object_id: str,
        new_object_id: str,
        *,
        scope: QueryScope,
        reason: str,
        created_by_job: str,
        baseline_id: str | None = None,
        metadata: StorageMetadata | None = None,
    ) -> ReplacementRecord:
        """Write an overlay replacement from an old logical object to a new one."""

    def flush(self) -> None:
        """Persist pending writes and make them visible to subsequent readers."""


@runtime_checkable
class StorageAdapter(Protocol):
    """Factory contract for one metadata storage backend implementation."""

    def reader(self) -> StorageReader:
        """Return a reader over physical and logical metadata views."""

    def writer(self, request: StorageWriteRequest) -> StorageWriter:
        """Return a writer scoped to one explicit baseline or overlay target."""

    def close(self) -> None:
        """Release adapter resources."""


@runtime_checkable
class VectorStoreReader(Protocol):
    """Read contract for baseline+delta vector stores."""

    def search(self, request: VectorQuery) -> VectorSearchResult:
        """Run merged vector search over baseline and delta collections."""


@runtime_checkable
class VectorStoreWriter(Protocol):
    """Write contract for vector payloads and synchronized metadata refs."""

    @property
    def request(self) -> StorageWriteRequest:
        """Return the explicit write intent bound to this writer."""

    def upsert_vector(self, record: VectorRefRecord, embedding: Iterable[float]) -> VectorRefRecord:
        """Insert or update one vector payload and synchronized metadata ref."""

    def delete_object_vectors(
        self,
        object_type: Literal["chunk", "entity", "evidence"],
        object_ids: Iterable[str],
    ) -> int:
        """Delete vector payloads for the given logical objects from the writable target."""

    def flush(self) -> None:
        """Persist pending writes and make them visible to subsequent readers."""


@runtime_checkable
class VectorStoreAdapter(Protocol):
    """Factory contract for one vector-store backend implementation."""

    def reader(self) -> VectorStoreReader:
        """Return a reader over baseline and delta vector collections."""

    def writer(self, request: StorageWriteRequest) -> VectorStoreWriter:
        """Return a writer scoped to one explicit baseline or overlay target."""

    def close(self) -> None:
        """Release adapter resources."""
