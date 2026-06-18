"""LanceDB-compatible baseline/delta vector adapter with local-file fallback."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage.base import (
    ALL_SCOPE,
    BaselineWriteBlockedError,
    LogicalChunk,
    LogicalEntity,
    LogicalEvidence,
    QueryScope,
    StorageAdapter,
    StorageMetadata,
    StorageSourceIndex,
    StorageWarning,
    StorageWriteRequest,
    VectorMatch,
    VectorQuery,
    VectorRefRecord,
    VectorSearchResult,
    validate_write_request,
)
from active_knowledge_server.storage.sqlite_store import SQLiteStorageAdapter

VectorObjectType = Literal["chunk", "entity", "evidence"]

LATEST_VECTOR_SCHEMA_VERSION: Final = "1.1.0"
_SOURCE_PRIORITY: Final[dict[StorageSourceIndex, int]] = {
    "baseline": 1,
    "merged": 2,
    "overlay": 3,
}
_COLLECTIONS: Final[tuple[VectorObjectType, ...]] = ("chunk", "entity", "evidence")
_SEGMENT_FILE_SUFFIX: Final = ".jsonl"
_SEGMENT_DISCOVERY_GLOB: Final = f"*{_SEGMENT_FILE_SUFFIX}"
_DEFAULT_SEGMENT_JOB_ID: Final = "adhoc"
_COMPACTION_SEGMENT_THRESHOLD: Final = 8
_COMPACTION_ROW_THRESHOLD: Final = 1024


@dataclass(frozen=True)
class _VectorRow:
    vector_ref_id: str
    object_type: VectorObjectType
    object_id: str
    chunk_id: str | None
    embedding_model_version: str
    content_hash: str
    source_scope: str
    profile_id: str
    embedding: tuple[float, ...]
    metadata: StorageMetadata


@dataclass(frozen=True)
class _LiveObject:
    logical_object_id: str
    physical_object_id: str
    source_index: StorageSourceIndex
    content_hash: str | None
    profile_id: str
    source_scope: str
    chunk_id: str | None = None


@dataclass(frozen=True)
class _VectorSegmentWrite:
    filename: str
    row_count: int
    embedding_model_versions: tuple[str, ...]


def configured_lancedb_paths(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
) -> dict[Literal["baseline", "overlay"], Path]:
    """Resolve configured baseline and delta vector-store roots."""

    return {
        "baseline": resolve_runtime_path(config.storage.vector.path, cwd),
        "overlay": resolve_runtime_path(config.storage.vector_delta.path, cwd),
    }


class LanceDBVectorAdapter:
    """Baseline+delta vector adapter backed by local directory stores."""

    def __init__(
        self,
        *,
        baseline_vector_path: Path,
        delta_vector_path: Path,
        metadata_adapter: StorageAdapter,
        config: ActiveKnowledgeConfig | None = None,
        owns_metadata_adapter: bool = False,
    ) -> None:
        self._baseline_vector_path = baseline_vector_path
        self._delta_vector_path = delta_vector_path
        self._metadata_adapter = metadata_adapter
        self._config = config
        self._owns_metadata_adapter = owns_metadata_adapter

    @classmethod
    def from_config(
        cls,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path,
        metadata_adapter: StorageAdapter | None = None,
    ) -> LanceDBVectorAdapter:
        """Build a vector adapter from validated config."""

        paths = configured_lancedb_paths(config, cwd=cwd)
        owns_metadata_adapter = metadata_adapter is None
        adapter = metadata_adapter or SQLiteStorageAdapter.from_config(config, cwd=cwd)
        return cls(
            baseline_vector_path=paths["baseline"],
            delta_vector_path=paths["overlay"],
            metadata_adapter=adapter,
            config=config,
            owns_metadata_adapter=owns_metadata_adapter,
        )

    def reader(self) -> LanceDBVectorReader:
        """Return a reader over baseline and delta vector collections."""

        default_model = self._config.indexing.embeddings.model if self._config is not None else None
        return LanceDBVectorReader(
            baseline_vector_path=self._baseline_vector_path,
            delta_vector_path=self._delta_vector_path,
            metadata_reader=self._metadata_adapter.reader(),
            default_embedding_model_version=default_model,
        )

    def writer(self, request: StorageWriteRequest) -> LanceDBVectorWriter:
        """Return a writer scoped to one explicit baseline or overlay target."""

        if self._config is not None:
            validate_write_request(self._config, request)
        elif request.target == "baseline" and request.operation_mode != "baseline_publish":
            raise BaselineWriteBlockedError(
                "Baseline writes are blocked for normal runs. "
                "Use operation_mode=baseline_publish with writable baseline stores."
            )
        return LanceDBVectorWriter(
            baseline_vector_path=self._baseline_vector_path,
            delta_vector_path=self._delta_vector_path,
            metadata_writer=self._metadata_adapter.writer(request),
            request=request,
        )

    def close(self) -> None:
        """Release adapter resources."""

        if self._owns_metadata_adapter:
            self._metadata_adapter.close()


class LanceDBVectorReader:
    """Read and merge baseline/delta vector candidates with logical filtering."""

    def __init__(
        self,
        *,
        baseline_vector_path: Path,
        delta_vector_path: Path,
        metadata_reader: Any,
        default_embedding_model_version: str | None = None,
    ) -> None:
        self._baseline_vector_path = baseline_vector_path
        self._delta_vector_path = delta_vector_path
        self._metadata_reader = metadata_reader
        self._default_embedding_model_version = default_embedding_model_version

    def search(self, request: VectorQuery) -> VectorSearchResult:
        """Run merged vector search over baseline and delta collections."""

        expected_model = request.embedding_model_version or self._default_embedding_model_version
        live_vector_refs = {
            item.vector_ref_id: item for item in self._metadata_reader.iter_vector_refs(request.scope)
        }
        live_chunks = {
            item.logical_object_id: live_chunk(item)
            for item in self._metadata_reader.logical_chunks(request.scope)
        }
        live_entities = {
            item.logical_object_id: live_entity(item)
            for item in self._metadata_reader.logical_entities(request.scope)
        }
        live_evidence_objects = {
            item.logical_object_id: live_evidence(item)
            for item in self._metadata_reader.logical_evidence(request.scope)
        }

        warnings: list[StorageWarning] = []
        candidates: dict[str, VectorMatch] = {}
        for source_index, root in selected_vector_sources(
            request.source_index,
            baseline_root=self._baseline_vector_path,
            delta_root=self._delta_vector_path,
        ):
            rows = load_store_rows(root, request.object_types)
            if expected_model is not None:
                available_versions = {row.embedding_model_version for row in rows}
                if rows and expected_model not in available_versions:
                    warnings.append(
                        StorageWarning(
                            code="embedding.version_mismatch",
                            message=(
                                f"{source_index} vector store uses "
                                f"{sorted(available_versions)}; expected {expected_model}."
                            ),
                            level="degraded",
                            details=cast(
                                StorageMetadata,
                                {
                                    "source_index": source_index,
                                    "expected_embedding_model_version": expected_model,
                                    "available_embedding_model_versions": sorted(
                                        available_versions
                                    ),
                                },
                            ),
                        )
                    )
                    continue

            for row in rows:
                if expected_model is not None and row.embedding_model_version != expected_model:
                    continue
                match = self._build_match(
                    row=row,
                    request=request,
                    source_index=source_index,
                    live_vector_refs=live_vector_refs,
                    live_chunks=live_chunks,
                    live_entities=live_entities,
                    live_evidence=live_evidence_objects,
                )
                if match is None:
                    continue
                current = candidates.get(match.logical_object_id)
                candidates[match.logical_object_id] = (
                    match if current is None else prefer_vector_match(current, match)
                )

        ordered = sorted(
            candidates.values(),
            key=lambda item: (
                -item.score,
                -_SOURCE_PRIORITY[item.source_index],
                item.logical_object_id,
            ),
        )
        return VectorSearchResult(
            matches=tuple(ordered[: request.top_k]),
            warnings=tuple(warnings),
        )

    def _build_match(
        self,
        *,
        row: _VectorRow,
        request: VectorQuery,
        source_index: Literal["baseline", "overlay"],
        live_vector_refs: dict[str, VectorRefRecord],
        live_chunks: dict[str, _LiveObject],
        live_entities: dict[str, _LiveObject],
        live_evidence: dict[str, _LiveObject],
    ) -> VectorMatch | None:
        if request.scope.profile_id != ALL_SCOPE and row.profile_id not in (
            ALL_SCOPE,
            request.scope.profile_id,
        ):
            return None
        if request.scope.source_scope != ALL_SCOPE and row.source_scope not in (
            ALL_SCOPE,
            request.scope.source_scope,
        ):
            return None
        row_scope = QueryScope(
            snapshot_id=request.scope.snapshot_id,
            profile_id=row.profile_id,
            source_scope=row.source_scope,
            path_scope=request.scope.path_scope,
            include_inactive=request.scope.include_inactive,
        )
        if self._metadata_reader.is_tombstoned(row.object_type, row.object_id, row_scope):
            return None
        if self._metadata_reader.is_tombstoned("vector_ref", row.vector_ref_id, row_scope):
            return None
        vector_ref = live_vector_refs.get(row.vector_ref_id)
        if vector_ref is None:
            return None
        resolution = self._metadata_reader.resolve_replacement(
            row.object_type,
            row.object_id,
            row_scope,
        )
        if resolution.replaced:
            return None

        live_object = resolve_live_object(
            row.object_type,
            row.object_id,
            live_chunks=live_chunks,
            live_entities=live_entities,
            live_evidence=live_evidence,
        )
        if live_object is None:
            return None
        if vector_ref.object_type != row.object_type or vector_ref.object_id != row.object_id:
            return None
        if vector_ref.content_hash != row.content_hash:
            return None
        if live_object.content_hash is not None and row.content_hash != live_object.content_hash:
            return None

        score = cosine_similarity(request.embedding, row.embedding)
        if math.isnan(score):
            return None
        metadata = dict(row.metadata)
        metadata["vector_ref_id"] = row.vector_ref_id
        metadata["similarity"] = score
        return VectorMatch(
            logical_object_id=live_object.logical_object_id,
            physical_object_id=row.object_id,
            vector_ref_id=row.vector_ref_id,
            object_type=row.object_type,
            source_index=source_index,
            score=score,
            embedding_model_version=row.embedding_model_version,
            content_hash=row.content_hash,
            chunk_id=row.chunk_id or live_object.chunk_id,
            profile_id=row.profile_id,
            source_scope=row.source_scope,
            metadata=metadata,
        )


class LanceDBVectorWriter:
    """Write vector payloads to one target while syncing metadata refs."""

    def __init__(
        self,
        *,
        baseline_vector_path: Path,
        delta_vector_path: Path,
        metadata_writer: Any,
        request: StorageWriteRequest,
    ) -> None:
        if request.target == "baseline" and request.operation_mode != "baseline_publish":
            raise BaselineWriteBlockedError(
                "Baseline writes are blocked for normal runs. "
                "Use operation_mode=baseline_publish with writable baseline stores."
            )
        self._baseline_vector_path = baseline_vector_path
        self._delta_vector_path = delta_vector_path
        self._metadata_writer = metadata_writer
        self._request = request
        self._dirty_object_types: set[VectorObjectType] = set()

    @property
    def request(self) -> StorageWriteRequest:
        return self._request

    def upsert_vector(self, record: VectorRefRecord, embedding: Iterable[float]) -> VectorRefRecord:
        return self.upsert_vectors(((record, embedding),))[0]

    def upsert_vectors(
        self,
        records: Iterable[tuple[VectorRefRecord, Iterable[float]]],
    ) -> tuple[VectorRefRecord, ...]:
        rows_by_type: dict[VectorObjectType, list[_VectorRow]] = {}
        normalized_records: list[VectorRefRecord] = []
        for record, embedding in records:
            normalized_record = normalize_vector_ref(record)
            normalized_records.append(normalized_record)
            rows_by_type.setdefault(normalized_record.object_type, []).append(
                _VectorRow(
                    vector_ref_id=normalized_record.vector_ref_id,
                    object_type=normalized_record.object_type,
                    object_id=normalized_record.object_id,
                    chunk_id=normalized_record.chunk_id,
                    embedding_model_version=normalized_record.embedding_model_version,
                    content_hash=normalized_record.content_hash,
                    source_scope=normalized_record.source_scope,
                    profile_id=normalized_record.profile_id,
                    embedding=tuple(float(value) for value in embedding),
                    metadata=dict(normalized_record.metadata),
                )
            )
        if not normalized_records:
            return ()

        root = self._target_root
        for object_type, new_rows in rows_by_type.items():
            if self._request.target == "baseline" and self._request.operation_mode == "baseline_publish":
                rows = list(load_collection_rows(root, object_type))
                row_index = {row.vector_ref_id: index for index, row in enumerate(rows)}
                for row in new_rows:
                    existing_index = row_index.get(row.vector_ref_id)
                    if existing_index is None:
                        row_index[row.vector_ref_id] = len(rows)
                        rows.append(row)
                    else:
                        rows[existing_index] = row
                write_collection_rows(root, object_type, rows)
            else:
                append_collection_segment(
                    root,
                    object_type,
                    new_rows,
                    job_id=self._request.job_id,
                )
                self._dirty_object_types.add(object_type)
            validate_collection_rows_written(root, object_type, new_rows)

        with self._metadata_writer.transaction():
            self._metadata_writer.upsert_vector_refs(normalized_records)
        return tuple(normalized_records)

    def delete_object_vectors(
        self,
        object_type: VectorObjectType,
        object_ids: Iterable[str],
    ) -> int:
        target_ids = {object_id for object_id in object_ids}
        if not target_ids:
            return 0
        root = self._target_root
        rows = list(load_collection_rows(root, object_type))
        kept = [row for row in rows if row.object_id not in target_ids]
        write_collection_rows(root, object_type, kept)
        self._dirty_object_types.discard(object_type)
        return len(rows) - len(kept)

    def flush(self) -> None:
        for object_type in tuple(sorted(self._dirty_object_types)):
            maybe_compact_collection(
                self._target_root,
                object_type,
                requested_by_job_id=self._request.job_id,
            )
        self._dirty_object_types.clear()

    @property
    def _target_root(self) -> Path:
        return (
            self._delta_vector_path
            if self._request.target == "overlay"
            else self._baseline_vector_path
        )


def normalize_vector_ref(record: VectorRefRecord) -> VectorRefRecord:
    chunk_id = record.chunk_id
    if record.object_type == "chunk" and chunk_id is None:
        chunk_id = record.object_id
    return VectorRefRecord(
        vector_ref_id=record.vector_ref_id,
        object_type=record.object_type,
        object_id=record.object_id,
        chunk_id=chunk_id,
        embedding_model_version=record.embedding_model_version,
        content_hash=record.content_hash,
        source_scope=record.source_scope,
        profile_id=record.profile_id,
        metadata=dict(record.metadata),
    )


def selected_vector_sources(
    source_index: StorageSourceIndex | None,
    *,
    baseline_root: Path,
    delta_root: Path,
) -> tuple[tuple[Literal["overlay", "baseline"], Path], ...]:
    if source_index == "overlay":
        return (("overlay", delta_root),)
    if source_index == "baseline":
        return (("baseline", baseline_root),)
    return (("overlay", delta_root), ("baseline", baseline_root))


def prefer_vector_match(current: VectorMatch, candidate: VectorMatch) -> VectorMatch:
    if _SOURCE_PRIORITY[candidate.source_index] > _SOURCE_PRIORITY[current.source_index]:
        return candidate
    if _SOURCE_PRIORITY[candidate.source_index] < _SOURCE_PRIORITY[current.source_index]:
        return current
    if candidate.score > current.score:
        return candidate
    return current


def live_chunk(item: LogicalChunk) -> _LiveObject:
    return _LiveObject(
        logical_object_id=item.logical_object_id,
        physical_object_id=item.physical_object_id,
        source_index=item.source_index,
        content_hash=item.record.content_hash,
        profile_id=item.record.profile_id,
        source_scope=item.record.source_scope,
        chunk_id=item.record.chunk_id,
    )


def live_entity(item: LogicalEntity) -> _LiveObject:
    content_hash = item.record.metadata.get("content_hash")
    return _LiveObject(
        logical_object_id=item.logical_object_id,
        physical_object_id=item.physical_object_id,
        source_index=item.source_index,
        content_hash=str(content_hash) if content_hash is not None else None,
        profile_id=item.record.profile_id,
        source_scope=item.record.source_scope,
    )


def live_evidence(item: LogicalEvidence) -> _LiveObject:
    content_hash = item.record.metadata.get("content_hash")
    return _LiveObject(
        logical_object_id=item.logical_object_id,
        physical_object_id=item.physical_object_id,
        source_index=item.source_index,
        content_hash=str(content_hash) if content_hash is not None else None,
        profile_id=item.record.profile_id,
        source_scope=item.record.source_scope,
        chunk_id=item.record.chunk_id,
    )


def resolve_live_object(
    object_type: VectorObjectType,
    object_id: str,
    *,
    live_chunks: dict[str, _LiveObject],
    live_entities: dict[str, _LiveObject],
    live_evidence: dict[str, _LiveObject],
) -> _LiveObject | None:
    if object_type == "chunk":
        return live_chunks.get(object_id)
    if object_type == "entity":
        return live_entities.get(object_id)
    return live_evidence.get(object_id)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return float("nan")
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return float("nan")
    return numerator / (left_norm * right_norm)


def collection_path(root: Path, object_type: VectorObjectType) -> Path:
    return root / f"{object_type}.json"


def manifest_path(root: Path) -> Path:
    return root / "manifest.json"


def segment_dir(root: Path, object_type: VectorObjectType) -> Path:
    return root / object_type


def load_store_rows(root: Path, object_types: Iterable[VectorObjectType]) -> tuple[_VectorRow, ...]:
    rows: list[_VectorRow] = []
    for object_type in object_types:
        rows.extend(load_collection_rows(root, object_type))
    return tuple(rows)


def load_collection_rows(root: Path, object_type: VectorObjectType) -> tuple[_VectorRow, ...]:
    merged: dict[str, _VectorRow] = {
        row.vector_ref_id: row for row in _load_compacted_collection_rows(root, object_type)
    }
    for path in _segment_paths(root, object_type):
        for row in _load_segment_rows(path):
            merged[row.vector_ref_id] = row
    return tuple(merged.values())


def _load_compacted_collection_rows(
    root: Path,
    object_type: VectorObjectType,
) -> tuple[_VectorRow, ...]:
    path = collection_path(root, object_type)
    if not path.exists():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return ()
    return tuple(vector_row_from_dict(item) for item in raw if isinstance(item, dict))


def write_collection_rows(
    root: Path,
    object_type: VectorObjectType,
    rows: Sequence[_VectorRow],
    *,
    compaction_checkpoint: Mapping[str, Any] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = collection_path(root, object_type)
    payload = [vector_row_to_dict(row) for row in rows]
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
    )
    _delete_segment_files(root, object_type)
    _update_manifest_collection(
        root,
        object_type,
        compacted_rows=rows,
        segments=(),
        segment_row_count=0,
        compaction_checkpoint=compaction_checkpoint,
    )


def append_collection_segment(
    root: Path,
    object_type: VectorObjectType,
    rows: Sequence[_VectorRow],
    *,
    job_id: str | None = None,
) -> _VectorSegmentWrite | None:
    if not rows:
        return None
    root.mkdir(parents=True, exist_ok=True)
    directory = segment_dir(root, object_type)
    directory.mkdir(parents=True, exist_ok=True)
    safe_job_id = _segment_job_id(job_id)
    filename = f"{safe_job_id}-part-{_next_segment_part(root, object_type, safe_job_id):06d}{_SEGMENT_FILE_SUFFIX}"
    path = directory / filename
    lines = [
        json.dumps(vector_row_to_dict(row), ensure_ascii=True, sort_keys=True)
        for row in rows
    ]
    _atomic_write_text(path, "".join(f"{line}\n" for line in lines))
    segment_write = _VectorSegmentWrite(
        filename=filename,
        row_count=len(rows),
        embedding_model_versions=tuple(sorted({row.embedding_model_version for row in rows})),
    )
    manifest = _read_manifest(root)
    collection = _manifest_collection_entry(manifest, object_type)
    segments = list(_manifest_segments(collection))
    segments.append(filename)
    _set_manifest_collection(
        manifest,
        object_type,
        compacted_rows=_load_compacted_collection_rows(root, object_type),
        segments=segments,
        segment_row_count=int(collection.get("segment_row_count", 0)) + len(rows),
        embedding_model_versions=tuple(
            sorted(
                {
                    *(
                        str(item)
                        for item in collection.get("embedding_model_versions", [])
                        if str(item)
                    ),
                    *segment_write.embedding_model_versions,
                }
            )
        ),
        last_compaction=(
            collection.get("last_compaction")
            if isinstance(collection.get("last_compaction"), dict)
            else None
        ),
    )
    _write_manifest_payload(root, manifest)
    return segment_write


def validate_collection_rows_written(
    root: Path,
    object_type: VectorObjectType,
    expected_rows: Sequence[_VectorRow],
) -> None:
    """Verify vector payload rows before committing metadata vector refs."""

    persisted = {row.vector_ref_id: row for row in load_collection_rows(root, object_type)}
    missing_or_mismatched = [
        row.vector_ref_id for row in expected_rows if persisted.get(row.vector_ref_id) != row
    ]
    if missing_or_mismatched:
        raise RuntimeError(
            "vector payload validation failed for refs: " + ", ".join(sorted(missing_or_mismatched))
        )


def write_manifest(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    previous_manifest = _read_manifest(root)
    collections: dict[str, dict[str, Any]] = {}
    for object_type in _COLLECTIONS:
        compacted_rows = _load_compacted_collection_rows(root, object_type)
        segments = [path.name for path in _segment_paths(root, object_type)]
        segment_row_count = sum(len(_load_segment_rows(path)) for path in _segment_paths(root, object_type))
        if not compacted_rows and not segments:
            continue
        last_compaction = _manifest_collection_entry(previous_manifest, object_type).get(
            "last_compaction"
        )
        collections[object_type] = _build_manifest_collection(
            compacted_rows=compacted_rows,
            segments=segments,
            segment_row_count=segment_row_count,
            embedding_model_versions=tuple(
                sorted(
                    {
                        *(row.embedding_model_version for row in compacted_rows),
                        *(
                            row.embedding_model_version
                            for path in _segment_paths(root, object_type)
                            for row in _load_segment_rows(path)
                        ),
                    }
                )
            ),
            last_compaction=last_compaction if isinstance(last_compaction, dict) else None,
        )
    payload = {
        "schema_version": LATEST_VECTOR_SCHEMA_VERSION,
        "backend": "lancedb-fallback",
        "collections": collections,
    }
    _write_manifest_payload(root, payload)


def maybe_compact_collection(
    root: Path,
    object_type: VectorObjectType,
    *,
    requested_by_job_id: str | None = None,
) -> bool:
    manifest = _read_manifest(root)
    collection = _manifest_collection_entry(manifest, object_type)
    segment_paths = _segment_paths(root, object_type)
    if not segment_paths:
        return False
    segment_row_count = int(collection.get("segment_row_count", 0))
    if len(segment_paths) < _COMPACTION_SEGMENT_THRESHOLD and segment_row_count < _COMPACTION_ROW_THRESHOLD:
        return False
    rows = load_collection_rows(root, object_type)
    write_collection_rows(
        root,
        object_type,
        rows,
        compaction_checkpoint={
            "compacted_at": utc_now(),
            "requested_by_job_id": requested_by_job_id,
            "segment_count": len(segment_paths),
            "segment_row_count": segment_row_count,
            "row_count": len(rows),
        },
    )
    return True


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_segment_rows(path: Path) -> tuple[_VectorRow, ...]:
    if not path.exists():
        return ()
    rows: list[_VectorRow] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(vector_row_from_dict(payload))
    return tuple(rows)


def _segment_paths(root: Path, object_type: VectorObjectType) -> tuple[Path, ...]:
    directory = segment_dir(root, object_type)
    if not directory.exists():
        return ()
    manifest = _read_manifest(root)
    collection = _manifest_collection_entry(manifest, object_type)
    listed = _manifest_segments(collection)
    ordered: list[Path] = []
    seen: set[str] = set()
    for filename in listed:
        path = directory / filename
        if path.exists():
            ordered.append(path)
            seen.add(filename)
    for path in sorted(directory.glob(_SEGMENT_DISCOVERY_GLOB)):
        if path.name in seen:
            continue
        ordered.append(path)
    return tuple(ordered)


def _delete_segment_files(root: Path, object_type: VectorObjectType) -> None:
    directory = segment_dir(root, object_type)
    if not directory.exists():
        return
    for path in directory.glob(_SEGMENT_DISCOVERY_GLOB):
        path.unlink()
    try:
        directory.rmdir()
    except OSError:
        return


def _next_segment_part(root: Path, object_type: VectorObjectType, job_id: str) -> int:
    highest = 0
    prefix = f"{job_id}-part-"
    for path in segment_dir(root, object_type).glob(f"{prefix}*{_SEGMENT_FILE_SUFFIX}"):
        suffix = path.stem
        if not suffix.startswith(prefix):
            continue
        part = suffix[len(prefix) :]
        if part.isdigit():
            highest = max(highest, int(part))
    return highest + 1


def _segment_job_id(job_id: str | None) -> str:
    raw = (job_id or _DEFAULT_SEGMENT_JOB_ID).strip()
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in raw
    )
    return cleaned or _DEFAULT_SEGMENT_JOB_ID


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _read_manifest(root: Path) -> dict[str, Any]:
    path = manifest_path(root)
    if not path.exists():
        return {
            "schema_version": LATEST_VECTOR_SCHEMA_VERSION,
            "backend": "lancedb-fallback",
            "collections": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {
            "schema_version": LATEST_VECTOR_SCHEMA_VERSION,
            "backend": "lancedb-fallback",
            "collections": {},
        }
    collections = payload.get("collections")
    if not isinstance(collections, dict):
        payload["collections"] = {}
    payload["schema_version"] = LATEST_VECTOR_SCHEMA_VERSION
    payload["backend"] = "lancedb-fallback"
    return cast(dict[str, Any], payload)


def _write_manifest_payload(root: Path, payload: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        manifest_path(root),
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
    )


def _manifest_collection_entry(
    manifest: Mapping[str, Any],
    object_type: VectorObjectType,
) -> dict[str, Any]:
    collections = manifest.get("collections", {})
    if not isinstance(collections, dict):
        return {}
    entry = collections.get(object_type, {})
    return cast(dict[str, Any], entry if isinstance(entry, dict) else {})


def _manifest_segments(collection: Mapping[str, Any]) -> tuple[str, ...]:
    segments = collection.get("segments", ())
    if not isinstance(segments, list):
        return ()
    return tuple(str(item) for item in segments if str(item))


def _update_manifest_collection(
    root: Path,
    object_type: VectorObjectType,
    *,
    compacted_rows: Sequence[_VectorRow],
    segments: Sequence[str],
    segment_row_count: int,
    compaction_checkpoint: Mapping[str, Any] | None = None,
) -> None:
    manifest = _read_manifest(root)
    last_compaction = compaction_checkpoint
    previous_collection = _manifest_collection_entry(manifest, object_type)
    if last_compaction is None:
        previous = previous_collection.get("last_compaction")
        last_compaction = previous if isinstance(previous, dict) else None
    _set_manifest_collection(
        manifest,
        object_type,
        compacted_rows=compacted_rows,
        segments=segments,
        segment_row_count=segment_row_count,
        embedding_model_versions=tuple(
            sorted({row.embedding_model_version for row in compacted_rows})
        ),
        last_compaction=last_compaction,
    )
    _write_manifest_payload(root, manifest)


def _set_manifest_collection(
    manifest: dict[str, Any],
    object_type: VectorObjectType,
    *,
    compacted_rows: Sequence[_VectorRow],
    segments: Sequence[str],
    segment_row_count: int,
    embedding_model_versions: Sequence[str],
    last_compaction: Mapping[str, Any] | None,
) -> None:
    collections = manifest.setdefault("collections", {})
    if not isinstance(collections, dict):
        collections = {}
        manifest["collections"] = collections
    if not compacted_rows and not segments:
        collections.pop(object_type, None)
        return
    collections[object_type] = _build_manifest_collection(
        compacted_rows=compacted_rows,
        segments=segments,
        segment_row_count=segment_row_count,
        embedding_model_versions=embedding_model_versions,
        last_compaction=last_compaction,
    )


def _build_manifest_collection(
    *,
    compacted_rows: Sequence[_VectorRow],
    segments: Sequence[str],
    segment_row_count: int,
    embedding_model_versions: Sequence[str],
    last_compaction: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "row_count": len(compacted_rows),
        "segment_count": len(segments),
        "segment_row_count": segment_row_count,
        "segments": list(segments),
        "embedding_model_versions": sorted({str(item) for item in embedding_model_versions}),
        "last_compaction": dict(last_compaction) if last_compaction is not None else None,
    }


def vector_row_from_dict(payload: dict[str, Any]) -> _VectorRow:
    metadata = payload.get("metadata", {})
    return _VectorRow(
        vector_ref_id=str(payload["vector_ref_id"]),
        object_type=cast(VectorObjectType, str(payload["object_type"])),
        object_id=str(payload["object_id"]),
        chunk_id=optional_text(payload.get("chunk_id")),
        embedding_model_version=str(payload["embedding_model_version"]),
        content_hash=str(payload["content_hash"]),
        source_scope=str(payload.get("source_scope", ALL_SCOPE)),
        profile_id=str(payload.get("profile_id", ALL_SCOPE)),
        embedding=tuple(float(value) for value in cast(list[Any], payload.get("embedding", []))),
        metadata=cast(StorageMetadata, metadata if isinstance(metadata, dict) else {}),
    )


def vector_row_to_dict(row: _VectorRow) -> dict[str, Any]:
    return {
        "vector_ref_id": row.vector_ref_id,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "chunk_id": row.chunk_id,
        "embedding_model_version": row.embedding_model_version,
        "content_hash": row.content_hash,
        "source_scope": row.source_scope,
        "profile_id": row.profile_id,
        "embedding": list(row.embedding),
        "metadata": dict(row.metadata),
    }


def optional_text(value: Any) -> str | None:
    return None if value is None else str(value)
