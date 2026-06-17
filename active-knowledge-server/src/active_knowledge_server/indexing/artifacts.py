"""Durable collect artifact cache for resumable incremental indexing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.source_docs import (
    SOURCE_DOCS_MANIFEST_SCHEMA_VERSION,
    SourceDocEntry,
    SourceDocsCategory,
    SourceDocsManifest,
    SourceDocsWarning,
)
from active_knowledge_server.connectors.workspace import (
    WORKSPACE_INVENTORY_SCHEMA_VERSION,
    FileInventoryEntry,
    RepositoryInfo,
    WorkspaceArea,
    WorkspaceInventory,
    WorkspaceWarning,
)
from active_knowledge_server.indexing.code_indexer import CodeIndexingWarning, IndexedCode
from active_knowledge_server.indexing.doc_indexer import (
    DocumentIndexingWarning,
    IndexedDocuments,
    VectorWrite,
)
from active_knowledge_server.indexing.embeddings import (
    EMBEDDING_PREPARATION_SCHEMA_VERSION,
    EmbeddingInput,
    EmbeddingPreparationResult,
)
from active_knowledge_server.security.secret_scan import SecretScanReportEntry
from active_knowledge_server.storage import (
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    RelationRecord,
    SourceRecord,
    VectorRefRecord,
)
from active_knowledge_server.storage.sqlite_store import utc_now

INDEX_COLLECT_ARTIFACT_SCHEMA_VERSION: Final = "index_collect_artifact.v1"
CollectArtifactKind = Literal["code", "docs"]
_COLLECT_ARTIFACT_DIRNAME: Final = "index-jobs"
_COLLECT_ARTIFACT_PHASE_DIRNAME: Final = "collect"
_COLLECT_ARTIFACT_FILENAME: Final[dict[CollectArtifactKind, str]] = {
    "code": "code.json",
    "docs": "docs.json",
}


@dataclass(frozen=True)
class CollectArtifactRef:
    """One persisted collect artifact reference."""

    kind: CollectArtifactKind
    path: Path
    artifact_hash: str
    collect_paths: tuple[str, ...]
    task_keys: tuple[str, ...]


class IndexCollectArtifactStore:
    """Read and write collect artifacts under one local index job."""

    def __init__(self, local_artifacts_root: Path, *, job_id: str) -> None:
        self._local_artifacts_root = local_artifacts_root.expanduser()
        self._job_id = job_id

    @classmethod
    def from_config(
        cls,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path,
        job_id: str,
    ) -> IndexCollectArtifactStore:
        return cls(
            resolve_runtime_path(config.storage.local_artifacts_root, cwd),
            job_id=job_id,
        )

    @property
    def collect_root(self) -> Path:
        return (
            self._local_artifacts_root
            / _COLLECT_ARTIFACT_DIRNAME
            / self._job_id
            / _COLLECT_ARTIFACT_PHASE_DIRNAME
        )

    def save_code(
        self,
        indexed: IndexedCode,
        *,
        plan_signature: str,
        collect_paths: Sequence[str],
        task_keys: Sequence[str],
    ) -> CollectArtifactRef:
        return self._save(
            kind="code",
            plan_signature=plan_signature,
            collect_paths=collect_paths,
            task_keys=task_keys,
            result_payload=encode_indexed_code_artifact_payload(indexed),
            result_schema_version=indexed.schema_version,
        )

    def load_code(
        self,
        *,
        plan_signature: str,
        expected_paths: Sequence[str],
        expected_schema_version: str,
    ) -> tuple[IndexedCode, CollectArtifactRef] | None:
        loaded = self._load(
            kind="code",
            plan_signature=plan_signature,
            expected_paths=expected_paths,
            expected_schema_version=expected_schema_version,
        )
        if loaded is None:
            return None
        payload, artifact = loaded
        return decode_indexed_code_artifact_payload(payload), artifact

    def save_docs(
        self,
        indexed: IndexedDocuments,
        *,
        plan_signature: str,
        collect_paths: Sequence[str],
        task_keys: Sequence[str],
    ) -> CollectArtifactRef:
        return self._save(
            kind="docs",
            plan_signature=plan_signature,
            collect_paths=collect_paths,
            task_keys=task_keys,
            result_payload=encode_indexed_documents_artifact_payload(indexed),
            result_schema_version=indexed.schema_version,
        )

    def load_docs(
        self,
        *,
        plan_signature: str,
        expected_paths: Sequence[str],
        expected_schema_version: str,
    ) -> tuple[IndexedDocuments, CollectArtifactRef] | None:
        loaded = self._load(
            kind="docs",
            plan_signature=plan_signature,
            expected_paths=expected_paths,
            expected_schema_version=expected_schema_version,
        )
        if loaded is None:
            return None
        payload, artifact = loaded
        return decode_indexed_documents_artifact_payload(payload), artifact

    def _save(
        self,
        *,
        kind: CollectArtifactKind,
        plan_signature: str,
        collect_paths: Sequence[str],
        task_keys: Sequence[str],
        result_payload: Mapping[str, object],
        result_schema_version: str,
    ) -> CollectArtifactRef:
        path = self.collect_root / _COLLECT_ARTIFACT_FILENAME[kind]
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "schema_version": INDEX_COLLECT_ARTIFACT_SCHEMA_VERSION,
            "artifact_kind": kind,
            "job_id": self._job_id,
            "plan_signature": plan_signature,
            "collect_paths": list(dict.fromkeys(str(item) for item in collect_paths)),
            "task_keys": list(dict.fromkeys(str(item) for item in task_keys)),
            "result_schema_version": result_schema_version,
            "created_at": utc_now(),
            "result": dict(result_payload),
        }
        artifact_hash = _stable_hash(payload)
        payload["artifact_hash"] = artifact_hash
        encoded = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
        temporary_path = path.with_name(f"{path.name}.tmp")
        temporary_path.write_text(encoded, encoding="utf-8")
        temporary_path.replace(path)
        return CollectArtifactRef(
            kind=kind,
            path=path,
            artifact_hash=artifact_hash,
            collect_paths=tuple(cast_str_seq(payload["collect_paths"])),
            task_keys=tuple(cast_str_seq(payload["task_keys"])),
        )

    def _load(
        self,
        *,
        kind: CollectArtifactKind,
        plan_signature: str,
        expected_paths: Sequence[str],
        expected_schema_version: str,
    ) -> tuple[Mapping[str, object], CollectArtifactRef] | None:
        path = self.collect_root / _COLLECT_ARTIFACT_FILENAME[kind]
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        artifact_hash = payload.get("artifact_hash")
        if not isinstance(artifact_hash, str):
            return None
        payload_without_hash = dict(payload)
        payload_without_hash.pop("artifact_hash", None)
        if _stable_hash(payload_without_hash) != artifact_hash:
            return None
        if (
            payload.get("schema_version") != INDEX_COLLECT_ARTIFACT_SCHEMA_VERSION
            or payload.get("artifact_kind") != kind
            or payload.get("job_id") != self._job_id
            or payload.get("plan_signature") != plan_signature
            or payload.get("result_schema_version") != expected_schema_version
        ):
            return None
        collect_paths = tuple(cast_str_seq(payload.get("collect_paths")))
        if not set(str(item) for item in expected_paths).issubset(set(collect_paths)):
            return None
        result = payload.get("result")
        if not isinstance(result, Mapping):
            return None
        return (
            result,
            CollectArtifactRef(
                kind=kind,
                path=path,
                artifact_hash=artifact_hash,
                collect_paths=collect_paths,
                task_keys=tuple(cast_str_seq(payload.get("task_keys"))),
            ),
        )


def encode_indexed_code_artifact_payload(indexed: IndexedCode) -> dict[str, object]:
    """Return a JSON-safe collect payload for one code index bundle."""

    return {
        "schema_version": indexed.schema_version,
        "snapshot_id": indexed.snapshot_id,
        "workspace_inventory": asdict(indexed.workspace_inventory),
        "source_records": [asdict(item) for item in indexed.source_records],
        "file_records": [asdict(item) for item in indexed.file_records],
        "chunk_records": [asdict(item) for item in indexed.chunk_records],
        "entity_records": [asdict(item) for item in indexed.entity_records],
        "relation_records": [asdict(item) for item in indexed.relation_records],
        "evidence_records": [asdict(item) for item in indexed.evidence_records],
        "warnings": [asdict(item) for item in indexed.warnings],
        "metadata": dict(indexed.metadata),
    }


def decode_indexed_code_artifact_payload(payload: Mapping[str, object]) -> IndexedCode:
    """Decode one JSON-safe code collect payload."""

    return IndexedCode(
        schema_version=str(payload.get("schema_version", "")),
        snapshot_id=str(payload.get("snapshot_id", "")),
        workspace_inventory=_decode_workspace_inventory(payload.get("workspace_inventory")),
        source_records=tuple(
            _decode_source_record(item) for item in cast_mapping_seq(payload.get("source_records"))
        ),
        file_records=tuple(
            _decode_file_record(item) for item in cast_mapping_seq(payload.get("file_records"))
        ),
        chunk_records=tuple(
            _decode_chunk_record(item) for item in cast_mapping_seq(payload.get("chunk_records"))
        ),
        entity_records=tuple(
            _decode_entity_record(item)
            for item in cast_mapping_seq(payload.get("entity_records"))
        ),
        relation_records=tuple(
            _decode_relation_record(item)
            for item in cast_mapping_seq(payload.get("relation_records"))
        ),
        evidence_records=tuple(
            _decode_evidence_record(item)
            for item in cast_mapping_seq(payload.get("evidence_records"))
        ),
        warnings=tuple(
            _decode_code_warning(item) for item in cast_mapping_seq(payload.get("warnings"))
        ),
        metadata=_mapping_dict(payload.get("metadata")),
    )


def encode_indexed_documents_artifact_payload(indexed: IndexedDocuments) -> dict[str, object]:
    """Return a JSON-safe collect payload for one document index bundle."""

    return {
        "schema_version": indexed.schema_version,
        "snapshot_id": indexed.snapshot_id,
        "source_manifest": asdict(indexed.source_manifest),
        "source_records": [asdict(item) for item in indexed.source_records],
        "file_records": [asdict(item) for item in indexed.file_records],
        "chunk_records": [asdict(item) for item in indexed.chunk_records],
        "entity_records": [asdict(item) for item in indexed.entity_records],
        "evidence_records": [asdict(item) for item in indexed.evidence_records],
        "vector_writes": [asdict(item) for item in indexed.vector_writes],
        "embedding_preparation": asdict(indexed.embedding_preparation),
        "warnings": [asdict(item) for item in indexed.warnings],
        "metadata": dict(indexed.metadata),
    }


def decode_indexed_documents_artifact_payload(payload: Mapping[str, object]) -> IndexedDocuments:
    """Decode one JSON-safe document collect payload."""

    return IndexedDocuments(
        schema_version=str(payload.get("schema_version", "")),
        snapshot_id=str(payload.get("snapshot_id", "")),
        source_manifest=_decode_source_docs_manifest(payload.get("source_manifest")),
        source_records=tuple(
            _decode_source_record(item) for item in cast_mapping_seq(payload.get("source_records"))
        ),
        file_records=tuple(
            _decode_file_record(item) for item in cast_mapping_seq(payload.get("file_records"))
        ),
        chunk_records=tuple(
            _decode_chunk_record(item) for item in cast_mapping_seq(payload.get("chunk_records"))
        ),
        entity_records=tuple(
            _decode_entity_record(item)
            for item in cast_mapping_seq(payload.get("entity_records"))
        ),
        evidence_records=tuple(
            _decode_evidence_record(item)
            for item in cast_mapping_seq(payload.get("evidence_records"))
        ),
        vector_writes=tuple(
            _decode_vector_write(item) for item in cast_mapping_seq(payload.get("vector_writes"))
        ),
        embedding_preparation=_decode_embedding_preparation(
            payload.get("embedding_preparation")
        ),
        warnings=tuple(
            _decode_document_warning(item) for item in cast_mapping_seq(payload.get("warnings"))
        ),
        metadata=_mapping_dict(payload.get("metadata")),
    )


def _decode_workspace_inventory(payload: object) -> WorkspaceInventory:
    raw = _mapping_dict(payload)
    return WorkspaceInventory(
        schema_version=str(raw.get("schema_version", WORKSPACE_INVENTORY_SCHEMA_VERSION)),
        workspace_root=str(raw.get("workspace_root", "")),
        workspace_display_path=str(raw.get("workspace_display_path", "")),
        include=tuple(cast_str_seq(raw.get("include"))),
        exclude=tuple(cast_str_seq(raw.get("exclude"))),
        areas=tuple(
            WorkspaceArea(
                name=str(item.get("name", "")),
                relative_path=str(item.get("relative_path", "")),
                display_path=str(item.get("display_path", "")),
                file_count=int(item.get("file_count", 0) or 0),
                directory_count=int(item.get("directory_count", 0) or 0),
            )
            for item in cast_mapping_seq(raw.get("areas"))
        ),
        repositories=tuple(
            RepositoryInfo(
                relative_path=str(item.get("relative_path", "")),
                display_path=str(item.get("display_path", "")),
                commit=_optional_str(item.get("commit")),
                branch=_optional_str(item.get("branch")),
                dirty=_optional_bool(item.get("dirty")),
                boundary_kind=str(item.get("boundary_kind", "directory")),
                is_workspace_root=bool(item.get("is_workspace_root", False)),
                error=_optional_str(item.get("error")),
            )
            for item in cast_mapping_seq(raw.get("repositories"))
        ),
        files=tuple(
            FileInventoryEntry(
                relative_path=str(item.get("relative_path", "")),
                display_path=str(item.get("display_path", "")),
                size_bytes=int(item.get("size_bytes", 0) or 0),
                content_hash=_optional_str(item.get("content_hash")),
                repo_relative_path=_optional_str(item.get("repo_relative_path")),
                area=_optional_str(item.get("area")),
                language=_optional_str(item.get("language")),
                is_symlink=bool(item.get("is_symlink", False)),
            )
            for item in cast_mapping_seq(raw.get("files"))
        ),
        inventory_hash=str(raw.get("inventory_hash", "")),
        warnings=tuple(
            WorkspaceWarning(
                code=str(item.get("code", "")),
                message=str(item.get("message", "")),
                display_path=str(item.get("display_path", "")),
                details=_mapping_dict(item.get("details")),
            )
            for item in cast_mapping_seq(raw.get("warnings"))
        ),
    )


def _decode_source_docs_manifest(payload: object) -> SourceDocsManifest:
    raw = _mapping_dict(payload)
    return SourceDocsManifest(
        schema_version=str(raw.get("schema_version", SOURCE_DOCS_MANIFEST_SCHEMA_VERSION)),
        source_docs_root=str(raw.get("source_docs_root", "")),
        source_docs_display_path=str(raw.get("source_docs_display_path", "")),
        supported_categories=tuple(cast_str_seq(raw.get("supported_categories"))),
        categories=tuple(
            SourceDocsCategory(
                name=str(item.get("name", "")),
                relative_path=str(item.get("relative_path", "")),
                display_path=str(item.get("display_path", "")),
                exists=bool(item.get("exists", False)),
                file_count=int(item.get("file_count", 0) or 0),
                directory_count=int(item.get("directory_count", 0) or 0),
            )
            for item in cast_mapping_seq(raw.get("categories"))
        ),
        files=tuple(
            SourceDocEntry(
                relative_path=str(item.get("relative_path", "")),
                display_path=str(item.get("display_path", "")),
                category=str(item.get("category", "")),
                size_bytes=int(item.get("size_bytes", 0) or 0),
                content_hash=_optional_str(item.get("content_hash")),
                format=_optional_str(item.get("format")),
                is_symlink=bool(item.get("is_symlink", False)),
            )
            for item in cast_mapping_seq(raw.get("files"))
        ),
        manifest_hash=str(raw.get("manifest_hash", "")),
        warnings=tuple(
            SourceDocsWarning(
                code=str(item.get("code", "")),
                message=str(item.get("message", "")),
                display_path=str(item.get("display_path", "")),
                details=_mapping_dict(item.get("details")),
            )
            for item in cast_mapping_seq(raw.get("warnings"))
        ),
    )


def _decode_source_record(payload: Mapping[str, object]) -> SourceRecord:
    return SourceRecord(
        source_id=str(payload.get("source_id", "")),
        source_type=str(payload.get("source_type", "")),
        display_name=str(payload.get("display_name", "")),
        root_path=str(payload.get("root_path", "")),
        revision=_optional_str(payload.get("revision")),
        metadata=_mapping_dict(payload.get("metadata")),
    )


def _decode_file_record(payload: Mapping[str, object]) -> FileRecord:
    return FileRecord(
        file_id=str(payload.get("file_id", "")),
        snapshot_id=str(payload.get("snapshot_id", "")),
        source_id=str(payload.get("source_id", "")),
        relative_path=str(payload.get("relative_path", "")),
        content_hash=str(payload.get("content_hash", "")),
        source_scope=str(payload.get("source_scope", "all")),
        profile_id=str(payload.get("profile_id", "all")),
        language=_optional_str(payload.get("language")),
        metadata=_mapping_dict(payload.get("metadata")),
    )


def _decode_chunk_record(payload: Mapping[str, object]) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=str(payload.get("chunk_id", "")),
        snapshot_id=str(payload.get("snapshot_id", "")),
        file_id=str(payload.get("file_id", "")),
        content_hash=str(payload.get("content_hash", "")),
        chunk_type=str(payload.get("chunk_type", "")),
        ordinal=int(payload.get("ordinal", 0) or 0),
        text=str(payload.get("text", "")),
        source_scope=str(payload.get("source_scope", "all")),
        profile_id=str(payload.get("profile_id", "all")),
        start_line=_optional_int(payload.get("start_line")),
        end_line=_optional_int(payload.get("end_line")),
        metadata=_mapping_dict(payload.get("metadata")),
    )


def _decode_entity_record(payload: Mapping[str, object]) -> EntityRecord:
    return EntityRecord(
        entity_id=str(payload.get("entity_id", "")),
        snapshot_id=str(payload.get("snapshot_id", "")),
        file_id=str(payload.get("file_id", "")),
        entity_type=str(payload.get("entity_type", "")),
        name=str(payload.get("name", "")),
        qualified_name=str(payload.get("qualified_name", "")),
        path=str(payload.get("path", "")),
        source_scope=str(payload.get("source_scope", "all")),
        profile_id=str(payload.get("profile_id", "all")),
        start_line=_optional_int(payload.get("start_line")),
        end_line=_optional_int(payload.get("end_line")),
        metadata=_mapping_dict(payload.get("metadata")),
    )


def _decode_relation_record(payload: Mapping[str, object]) -> RelationRecord:
    return RelationRecord(
        relation_id=str(payload.get("relation_id", "")),
        snapshot_id=str(payload.get("snapshot_id", "")),
        relation_type=str(payload.get("relation_type", "")),
        src_entity_id=str(payload.get("src_entity_id", "")),
        dst_entity_id=str(payload.get("dst_entity_id", "")),
        source_scope=str(payload.get("source_scope", "all")),
        profile_id=str(payload.get("profile_id", "all")),
        metadata=_mapping_dict(payload.get("metadata")),
    )


def _decode_evidence_record(payload: Mapping[str, object]) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=str(payload.get("evidence_id", "")),
        snapshot_id=str(payload.get("snapshot_id", "")),
        object_type=str(payload.get("object_type", "")),
        object_id=str(payload.get("object_id", "")),
        file_id=str(payload.get("file_id", "")),
        source_scope=str(payload.get("source_scope", "all")),
        profile_id=str(payload.get("profile_id", "all")),
        chunk_id=_optional_str(payload.get("chunk_id")),
        excerpt=_optional_str(payload.get("excerpt")),
        citation_label=_optional_str(payload.get("citation_label")),
        start_line=_optional_int(payload.get("start_line")),
        end_line=_optional_int(payload.get("end_line")),
        metadata=_mapping_dict(payload.get("metadata")),
    )


def _decode_vector_ref_record(payload: Mapping[str, object]) -> VectorRefRecord:
    return VectorRefRecord(
        vector_ref_id=str(payload.get("vector_ref_id", "")),
        object_type=str(payload.get("object_type", "")),
        object_id=str(payload.get("object_id", "")),
        chunk_id=_optional_str(payload.get("chunk_id")),
        embedding_model_version=str(payload.get("embedding_model_version", "")),
        content_hash=str(payload.get("content_hash", "")),
        source_scope=str(payload.get("source_scope", "all")),
        profile_id=str(payload.get("profile_id", "all")),
        metadata=_mapping_dict(payload.get("metadata")),
    )


def _decode_vector_write(payload: Mapping[str, object]) -> VectorWrite:
    return VectorWrite(
        record=_decode_vector_ref_record(_mapping_dict(payload.get("record"))),
        embedding=tuple(float(item) for item in cast_number_seq(payload.get("embedding"))),
    )


def _decode_embedding_preparation(payload: object) -> EmbeddingPreparationResult:
    raw = _mapping_dict(payload)
    return EmbeddingPreparationResult(
        schema_version=str(raw.get("schema_version", EMBEDDING_PREPARATION_SCHEMA_VERSION)),
        accepted_inputs=tuple(
            EmbeddingInput(
                object_id=str(item.get("object_id", "")),
                object_type=str(item.get("object_type", "chunk")),
                source_path=str(item.get("source_path", "")),
                content=str(item.get("content", "")),
                metadata=_mapping_dict(item.get("metadata")),
            )
            for item in cast_mapping_seq(raw.get("accepted_inputs"))
        ),
        skipped_reports=tuple(
            SecretScanReportEntry(
                source_path=str(item.get("source_path", "")),
                finding_count=int(item.get("finding_count", 0) or 0),
                reasons=tuple(cast_str_seq(item.get("reasons"))),
                secret_kinds=tuple(cast_str_seq(item.get("secret_kinds"))),
                line_numbers=tuple(
                    int(number) for number in cast_number_seq(item.get("line_numbers"))
                ),
                skip_embedding=bool(item.get("skip_embedding", True)),
            )
            for item in cast_mapping_seq(raw.get("skipped_reports"))
        ),
    )


def _decode_code_warning(payload: Mapping[str, object]) -> CodeIndexingWarning:
    return CodeIndexingWarning(
        code=str(payload.get("code", "")),
        message=str(payload.get("message", "")),
        relative_path=str(payload.get("relative_path", "")),
        level=str(payload.get("level", "caution")),
        details=_mapping_dict(payload.get("details")),
    )


def _decode_document_warning(payload: Mapping[str, object]) -> DocumentIndexingWarning:
    return DocumentIndexingWarning(
        code=str(payload.get("code", "")),
        message=str(payload.get("message", "")),
        relative_path=str(payload.get("relative_path", "")),
        level=str(payload.get("level", "caution")),
        details=_mapping_dict(payload.get("details")),
    )


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _mapping_dict(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(key, str)}


def cast_mapping_seq(payload: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in payload if isinstance(item, Mapping))


def cast_str_seq(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
        return ()
    return tuple(str(item) for item in payload if item is not None)


def cast_number_seq(payload: object) -> tuple[float, ...]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
        return ()
    return tuple(
        float(item)
        for item in payload
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
