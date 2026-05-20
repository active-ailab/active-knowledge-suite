"""LanceDB-compatible baseline/delta vector adapter with local-file fallback."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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

LATEST_VECTOR_SCHEMA_VERSION: Final = "1.0.0"
_SOURCE_PRIORITY: Final[dict[StorageSourceIndex, int]] = {
    "baseline": 1,
    "merged": 2,
    "overlay": 3,
}
_COLLECTIONS: Final[tuple[VectorObjectType, ...]] = ("chunk", "entity", "evidence")


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

    @property
    def request(self) -> StorageWriteRequest:
        return self._request

    def upsert_vector(self, record: VectorRefRecord, embedding: Iterable[float]) -> VectorRefRecord:
        normalized_record = normalize_vector_ref(record)
        row = _VectorRow(
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
        root = self._target_root
        rows = list(load_collection_rows(root, row.object_type))
        updated = False
        for index, existing in enumerate(rows):
            if existing.vector_ref_id == row.vector_ref_id:
                rows[index] = row
                updated = True
                break
        if not updated:
            rows.append(row)
        write_collection_rows(root, row.object_type, rows)
        self._metadata_writer.upsert_vector_ref(normalized_record)
        return normalized_record

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
        return len(rows) - len(kept)

    def flush(self) -> None:
        return None

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


def load_store_rows(root: Path, object_types: Iterable[VectorObjectType]) -> tuple[_VectorRow, ...]:
    rows: list[_VectorRow] = []
    for object_type in object_types:
        rows.extend(load_collection_rows(root, object_type))
    return tuple(rows)


def load_collection_rows(root: Path, object_type: VectorObjectType) -> tuple[_VectorRow, ...]:
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
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = collection_path(root, object_type)
    payload = [vector_row_to_dict(row) for row in rows]
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    write_manifest(root)


def write_manifest(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    collections: dict[str, dict[str, Any]] = {}
    for object_type in _COLLECTIONS:
        rows = load_collection_rows(root, object_type)
        if not rows:
            continue
        collections[object_type] = {
            "row_count": len(rows),
            "embedding_model_versions": sorted(
                {row.embedding_model_version for row in rows},
            ),
        }
    payload = {
        "schema_version": LATEST_VECTOR_SCHEMA_VERSION,
        "backend": "lancedb-fallback",
        "collections": collections,
    }
    manifest_path(root).write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )


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
