"""Document indexing boundary."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeVar

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.source_docs import (
    SourceDocEntry,
    SourceDocsConnector,
    SourceDocsManifest,
    SourceDocsWarning,
)
from active_knowledge_server.indexing.embeddings import (
    EMBEDDING_PREPARATION_SCHEMA_VERSION,
    EmbeddingInput,
    EmbeddingPreparationResult,
    embed_text_locally,
    prepare_embedding_inputs,
)
from active_knowledge_server.indexing.parallel import (
    parallel_map_ordered,
    resolve_indexing_workers,
)
from active_knowledge_server.indexing.progress import (
    IndexProgressCallback,
    IndexProgressEvent,
    noop_progress_callback,
    utc_timestamp,
)
from active_knowledge_server.indexing.snapshot import CURRENT_SNAPSHOT_ID
from active_knowledge_server.parsers.api_docs import parse_api_doc
from active_knowledge_server.parsers.markdown import (
    DocumentParseWarning,
    ParsedChunk,
    ParsedDocument,
    ParsedFrontMatter,
    parse_source_document,
)
from active_knowledge_server.parsers.widget_docs import parse_widget_doc
from active_knowledge_server.security.secret_scan import SecretScanner
from active_knowledge_server.storage import (
    ALL_SCOPE,
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    SourceRecord,
    StorageWriter,
    VectorRefRecord,
    VectorStoreWriter,
)

DOC_INDEXER_SCHEMA_VERSION: Final = "doc_indexer.v1"
_SOURCE_ID_PREFIX: Final = "knowledge-"
_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_]+")
_T = TypeVar("_T")
_FRESHNESS_FRONT_MATTER_KEYS: Final[tuple[str, ...]] = (
    "freshness_ts",
    "freshness",
    "updated_at",
    "last_updated",
)
_CATEGORY_DOMAIN_OVERRIDES: Final[dict[str, str]] = {
    "api": "engineering",
    "widgets": "engineering",
}


@dataclass(frozen=True)
class DocumentIndexingWarning:
    """Structured non-fatal warning emitted by the document indexer."""

    code: str
    message: str
    relative_path: str
    level: str = "caution"
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable warning."""

        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "relative_path": self.relative_path,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class VectorWrite:
    """One prepared vector payload paired with its metadata record."""

    record: VectorRefRecord
    embedding: tuple[float, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable vector write summary."""

        return {
            "vector_ref_id": self.record.vector_ref_id,
            "object_type": self.record.object_type,
            "object_id": self.record.object_id,
            "chunk_id": self.record.chunk_id,
            "embedding_model_version": self.record.embedding_model_version,
            "dimensions": len(self.embedding),
        }


@dataclass(frozen=True)
class IndexedDocuments:
    """Collected document records ready for storage writes."""

    schema_version: str
    snapshot_id: str
    source_manifest: SourceDocsManifest
    source_records: tuple[SourceRecord, ...]
    file_records: tuple[FileRecord, ...]
    chunk_records: tuple[ChunkRecord, ...]
    entity_records: tuple[EntityRecord, ...]
    evidence_records: tuple[EvidenceRecord, ...]
    vector_writes: tuple[VectorWrite, ...] = ()
    embedding_preparation: EmbeddingPreparationResult = field(
        default_factory=lambda: EmbeddingPreparationResult(
            schema_version=EMBEDDING_PREPARATION_SCHEMA_VERSION,
            accepted_inputs=(),
            skipped_reports=(),
        )
    )
    warnings: tuple[DocumentIndexingWarning, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def vector_refs(self) -> tuple[VectorRefRecord, ...]:
        """Return the vector metadata rows prepared for writing."""

        return tuple(write.record for write in self.vector_writes)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable result summary."""

        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_manifest_hash": self.source_manifest.manifest_hash,
            "source_count": len(self.source_records),
            "file_count": len(self.file_records),
            "chunk_count": len(self.chunk_records),
            "entity_count": len(self.entity_records),
            "evidence_count": len(self.evidence_records),
            "vector_count": len(self.vector_writes),
            "embedding_skips": [
                report.to_dict() for report in self.embedding_preparation.skipped_reports
            ],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class _CollectedDocumentEntry:
    """Records collected from one source document."""

    storage_relative_path: str
    file_records: tuple[FileRecord, ...] = ()
    chunk_records: tuple[ChunkRecord, ...] = ()
    entity_records: tuple[EntityRecord, ...] = ()
    evidence_records: tuple[EvidenceRecord, ...] = ()
    embedding_inputs: tuple[EmbeddingInput, ...] = ()
    warnings: tuple[DocumentIndexingWarning, ...] = ()


class DocumentIndexer:
    """Parse source docs into storage records and optional vector payloads."""

    def __init__(
        self,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
        source_docs_connector: SourceDocsConnector | None = None,
        secret_scanner: SecretScanner | None = None,
    ) -> None:
        self._config = config
        self._cwd = (cwd or Path.cwd()).expanduser()
        self._connector = source_docs_connector or SourceDocsConnector.from_config(
            config, cwd=self._cwd
        )
        self._secret_scanner = secret_scanner or SecretScanner.from_config(config)
        self._markdown_enabled = config.indexing.docs.enable_markdown
        self._html_enabled = config.indexing.docs.enable_html
        self._embeddings_enabled = config.indexing.embeddings.enabled
        self.embedding_model_version = config.indexing.embeddings.model
        self.embedding_provider = config.indexing.embeddings.provider
        self.embedding_dimensions = 24

    @classmethod
    def from_config(
        cls,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
        source_docs_connector: SourceDocsConnector | None = None,
        secret_scanner: SecretScanner | None = None,
    ) -> DocumentIndexer:
        """Build a document indexer from validated runtime config."""

        return cls(
            config,
            cwd=cwd,
            source_docs_connector=source_docs_connector,
            secret_scanner=secret_scanner,
        )

    def collect(
        self,
        *,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
        source_docs_manifest: SourceDocsManifest | None = None,
        progress_callback: IndexProgressCallback | None = None,
    ) -> IndexedDocuments:
        """Collect document records and prepared vectors without persisting them."""

        callback = progress_callback or noop_progress_callback
        manifest = source_docs_manifest or self._connector.scan()
        warnings: list[DocumentIndexingWarning] = [
            _warning_from_source_docs_warning(item) for item in manifest.warnings
        ]
        source_records = self._build_source_records(manifest)
        file_records: list[FileRecord] = []
        chunk_records: list[ChunkRecord] = []
        entity_records: list[EntityRecord] = []
        evidence_records: list[EvidenceRecord] = []
        embedding_inputs: list[EmbeddingInput] = []
        stage_total = len(manifest.files)
        started_at = utc_timestamp()
        workers = resolve_indexing_workers(
            self._config.indexing.workers,
            task_count=stage_total,
            phase="docs",
        )
        collect_message = (
            f"Collecting source documents with {workers.workers} "
            f"worker{'s' if workers.workers != 1 else ''}"
        )
        if stage_total:
            callback(
                IndexProgressEvent(
                    phase="doc_collect",
                    stage_total=stage_total,
                    stage_done=0,
                    message=collect_message,
                    warnings_count=len(warnings),
                    started_at=started_at,
                    updated_at=started_at,
                )
            )

        def report_finalize(message: str) -> None:
            if not stage_total:
                return
            callback(
                IndexProgressEvent(
                    phase="doc_finalize",
                    stage_total=stage_total,
                    stage_done=stage_total,
                    message=message,
                    warnings_count=len(warnings),
                    started_at=started_at,
                    updated_at=utc_timestamp(),
                )
            )

        def report_progress(path: str, done: int) -> None:
            callback(
                IndexProgressEvent(
                    phase="doc_collect",
                    stage_total=stage_total,
                    stage_done=done,
                    current_path=path,
                    message=collect_message,
                    warnings_count=len(warnings),
                    started_at=started_at,
                    updated_at=utc_timestamp(),
                )
            )

        results = parallel_map_ordered(
            manifest.files,
            key=lambda entry: _storage_relative_path(manifest, entry),
            mapper=lambda entry: self._collect_document_entry(
                snapshot_id=snapshot_id,
                manifest=manifest,
                entry=entry,
            ),
            workers=workers,
            callback=report_progress,
        )
        for result in results:
            if result.error is not None:
                warnings.append(
                    DocumentIndexingWarning(
                        code="docs.collect_failed",
                        message="A source document failed during collect and was skipped.",
                        relative_path=result.key,
                        details={"error": str(result.error)},
                    )
                )
                continue
            if result.value is None:
                continue
            file_records.extend(result.value.file_records)
            chunk_records.extend(result.value.chunk_records)
            entity_records.extend(result.value.entity_records)
            evidence_records.extend(result.value.evidence_records)
            embedding_inputs.extend(result.value.embedding_inputs)
            warnings.extend(result.value.warnings)

        report_finalize(
            f"Collected {len(file_records)}/{stage_total} source documents; sorting records"
        )
        file_records.sort(key=lambda record: record.relative_path)
        chunk_records.sort(
            key=lambda record: (
                str(record.metadata.get("path", "")),
                record.ordinal,
                record.chunk_id,
            )
        )
        entity_records.sort(
            key=lambda record: (record.path or record.qualified_name, record.entity_id)
        )
        evidence_records.sort(
            key=lambda record: (
                str(record.metadata.get("path", "")),
                record.start_line or 0,
                record.evidence_id,
            )
        )
        embedding_inputs.sort(key=lambda item: (item.source_path, item.object_id))

        report_finalize(
            f"Collected {len(file_records)}/{stage_total} source documents; preparing embedding batches"
        )
        embedding_preparation = (
            prepare_embedding_inputs(embedding_inputs, secret_scanner=self._secret_scanner)
            if self._embeddings_enabled
            else EmbeddingPreparationResult(
                schema_version=EMBEDDING_PREPARATION_SCHEMA_VERSION,
                accepted_inputs=(),
                skipped_reports=(),
            )
        )
        chunk_lookup = {chunk.chunk_id: chunk for chunk in chunk_records}
        vector_writes = tuple(
            self._build_vector_write(chunk_lookup[item.object_id])
            for item in embedding_preparation.accepted_inputs
            if item.object_type == "chunk" and item.object_id in chunk_lookup
        )

        report_finalize("Finalizing document index bundle for overlay apply")

        return IndexedDocuments(
            schema_version=DOC_INDEXER_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            source_manifest=manifest,
            source_records=source_records,
            file_records=tuple(file_records),
            chunk_records=tuple(chunk_records),
            entity_records=tuple(entity_records),
            evidence_records=tuple(evidence_records),
            vector_writes=vector_writes,
            embedding_preparation=embedding_preparation,
            warnings=tuple(warnings),
            metadata={"collect_workers": workers.to_dict()},
        )

    def _collect_document_entry(
        self,
        *,
        snapshot_id: str,
        manifest: SourceDocsManifest,
        entry: SourceDocEntry,
    ) -> _CollectedDocumentEntry:
        absolute_path = Path(manifest.source_docs_root) / entry.relative_path
        storage_relative_path = _storage_relative_path(manifest, entry)
        warnings: list[DocumentIndexingWarning] = []
        if not self._format_enabled(entry.format):
            warnings.append(
                DocumentIndexingWarning(
                    code="docs.format_unsupported",
                    message="Skipping source document because no parser is enabled for its format.",
                    relative_path=storage_relative_path,
                    details={"format": entry.format},
                )
            )
            return _CollectedDocumentEntry(
                storage_relative_path=storage_relative_path,
                warnings=tuple(warnings),
            )

        parsed = self._parse_document(absolute_path, entry)
        warnings.extend(
            _warning_from_parse_warning(storage_relative_path, item) for item in parsed.warnings
        )

        doc_metadata = _build_doc_metadata(absolute_path, entry, parsed)
        if doc_metadata["doc_type"] == "api" and doc_metadata["version"] is None:
            warnings.append(
                DocumentIndexingWarning(
                    code="docs.version_missing",
                    message="API document is missing front matter version metadata.",
                    relative_path=storage_relative_path,
                    details={
                        "doc_type": doc_metadata["doc_type"],
                        "freshness_ts": doc_metadata["freshness_ts"],
                    },
                )
            )

        source_id = _source_id_for_category(entry.category)
        source_scope = entry.category
        file_id = _stable_id("file", snapshot_id, source_id, storage_relative_path)
        file_record = FileRecord(
            file_id=file_id,
            snapshot_id=snapshot_id,
            source_id=source_id,
            relative_path=storage_relative_path,
            content_hash=entry.content_hash
            or _hash_text(absolute_path.read_text(encoding="utf-8")),
            source_scope=source_scope,
            profile_id=ALL_SCOPE,
            language=parsed.format,
            metadata=doc_metadata,
        )

        base_chunk_records = tuple(
            self._build_parsed_chunk_record(
                snapshot_id=snapshot_id,
                file_record=file_record,
                parsed_chunk=parsed_chunk,
                entry=entry,
                parsed=parsed,
                doc_metadata=doc_metadata,
                storage_relative_path=storage_relative_path,
            )
            for parsed_chunk in parsed.chunks
        )
        chunk_by_ordinal = {record.ordinal: record for record in base_chunk_records}

        entities: list[EntityRecord] = []
        evidences: list[EvidenceRecord] = []
        document_entity, document_evidence = self._build_document_entity_and_evidence(
            snapshot_id=snapshot_id,
            file_record=file_record,
            entry=entry,
            parsed=parsed,
            doc_metadata=doc_metadata,
            storage_relative_path=storage_relative_path,
            chunk_by_ordinal=chunk_by_ordinal,
        )
        if document_entity is not None:
            entities.append(document_entity)
        if document_evidence is not None:
            evidences.append(document_evidence)

        synthetic_chunks, item_entities, item_evidences = self._build_item_records(
            snapshot_id=snapshot_id,
            file_record=file_record,
            entry=entry,
            parsed=parsed,
            doc_metadata=doc_metadata,
            storage_relative_path=storage_relative_path,
            base_chunks=base_chunk_records,
        )
        entities.extend(item_entities)
        evidences.extend(item_evidences)

        embedding_inputs = tuple(
            EmbeddingInput(
                object_id=chunk_record.chunk_id,
                object_type="chunk",
                source_path=storage_relative_path,
                content=chunk_record.text,
                metadata={
                    "title": doc_metadata["title"],
                    "doc_type": doc_metadata["doc_type"],
                    "domain": doc_metadata["domain"],
                    "source_scope": source_scope,
                },
            )
            for chunk_record in (*base_chunk_records, *synthetic_chunks)
        )
        return _CollectedDocumentEntry(
            storage_relative_path=storage_relative_path,
            file_records=(file_record,),
            chunk_records=(*base_chunk_records, *synthetic_chunks),
            entity_records=tuple(entities),
            evidence_records=tuple(evidences),
            embedding_inputs=embedding_inputs,
            warnings=tuple(warnings),
        )

    def collect_and_store(
        self,
        writer: StorageWriter,
        *,
        vector_writer: VectorStoreWriter | None = None,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
        source_docs_manifest: SourceDocsManifest | None = None,
        progress_callback: IndexProgressCallback | None = None,
    ) -> IndexedDocuments:
        """Collect document records and persist them through storage writers."""

        indexed = self.collect(
            snapshot_id=snapshot_id,
            source_docs_manifest=source_docs_manifest,
            progress_callback=progress_callback,
        )
        batch_size = self._config.indexing.writer.batch_size
        commit_interval_ms = self._config.indexing.writer.commit_interval_ms
        _write_in_batches(
            indexed.source_records,
            batch_size=batch_size,
            commit_interval_ms=commit_interval_ms,
            transaction=writer.transaction,
            write_one=writer.upsert_source,
        )
        _write_in_batches(
            indexed.file_records,
            batch_size=batch_size,
            commit_interval_ms=commit_interval_ms,
            transaction=writer.transaction,
            write_one=writer.upsert_file,
        )
        _write_in_batches(
            indexed.chunk_records,
            batch_size=batch_size,
            commit_interval_ms=commit_interval_ms,
            transaction=writer.transaction,
            write_one=writer.upsert_chunk,
        )
        _write_in_batches(
            indexed.entity_records,
            batch_size=batch_size,
            commit_interval_ms=commit_interval_ms,
            transaction=writer.transaction,
            write_one=writer.upsert_entity,
        )
        _write_in_batches(
            indexed.evidence_records,
            batch_size=batch_size,
            commit_interval_ms=commit_interval_ms,
            transaction=writer.transaction,
            write_one=writer.upsert_evidence,
        )
        writer.flush()

        if vector_writer is not None:
            vector_writer.upsert_vectors(
                (write.record, write.embedding) for write in indexed.vector_writes
            )
            vector_writer.flush()

        return indexed

    def embed_text(self, text: str) -> tuple[float, ...]:
        """Return a deterministic local embedding for one text payload."""

        return embed_text_locally(text, dimensions=self.embedding_dimensions)

    def _build_source_records(self, manifest: SourceDocsManifest) -> tuple[SourceRecord, ...]:
        root = Path(manifest.source_docs_root)
        records: list[SourceRecord] = []
        for category in manifest.categories:
            if category.file_count == 0:
                continue
            records.append(
                SourceRecord(
                    source_id=_source_id_for_category(category.name),
                    source_type="source_docs",
                    display_name=f"knowledge-sources/{category.name}",
                    root_path=(root / category.relative_path).as_posix(),
                    revision=manifest.manifest_hash,
                    metadata={
                        "category": category.name,
                        "file_count": category.file_count,
                        "directory_count": category.directory_count,
                        "manifest_hash": manifest.manifest_hash,
                    },
                )
            )
        return tuple(records)

    def _parse_document(self, absolute_path: Path, entry: SourceDocEntry) -> ParsedDocument:
        if entry.category == "api":
            return parse_api_doc(absolute_path)
        if entry.category == "widgets":
            return parse_widget_doc(absolute_path)
        return parse_source_document(absolute_path, category=entry.category)

    def _format_enabled(self, format_name: str | None) -> bool:
        if format_name == "markdown":
            return self._markdown_enabled
        if format_name == "html":
            return self._html_enabled
        return False

    def _build_parsed_chunk_record(
        self,
        *,
        snapshot_id: str,
        file_record: FileRecord,
        parsed_chunk: ParsedChunk,
        entry: SourceDocEntry,
        parsed: ParsedDocument,
        doc_metadata: Mapping[str, object],
        storage_relative_path: str,
    ) -> ChunkRecord:
        metadata = {
            **dict(doc_metadata),
            "path": storage_relative_path,
            "heading_path": list(parsed_chunk.heading_path),
            "anchor": parsed_chunk.anchor,
            "chunk_format": parsed.format,
            "chunk_metadata": dict(parsed_chunk.metadata),
        }
        chunk_hash = _hash_jsonable(
            {
                "path": storage_relative_path,
                "chunk_type": parsed_chunk.chunk_type,
                "ordinal": parsed_chunk.ordinal,
                "text": parsed_chunk.text,
                "heading_path": list(parsed_chunk.heading_path),
            }
        )
        return ChunkRecord(
            chunk_id=_stable_id(
                "chunk",
                file_record.file_id,
                parsed_chunk.chunk_type,
                parsed_chunk.ordinal,
                chunk_hash,
            ),
            snapshot_id=snapshot_id,
            file_id=file_record.file_id,
            content_hash=chunk_hash,
            chunk_type=parsed_chunk.chunk_type,
            ordinal=parsed_chunk.ordinal,
            text=parsed_chunk.text,
            source_scope=entry.category,
            profile_id=ALL_SCOPE,
            start_line=parsed_chunk.start_line,
            end_line=parsed_chunk.end_line,
            metadata=metadata,
        )

    def _build_document_entity_and_evidence(
        self,
        *,
        snapshot_id: str,
        file_record: FileRecord,
        entry: SourceDocEntry,
        parsed: ParsedDocument,
        doc_metadata: Mapping[str, object],
        storage_relative_path: str,
        chunk_by_ordinal: Mapping[int, ChunkRecord],
    ) -> tuple[EntityRecord | None, EvidenceRecord | None]:
        primary_chunk = _select_primary_chunk(parsed.chunks, chunk_by_ordinal)
        title = str(doc_metadata["title"])
        path = (
            storage_relative_path
            if primary_chunk is None
            else _path_with_anchor(storage_relative_path, primary_chunk.metadata.get("anchor"))
        )
        entity = EntityRecord(
            entity_id=_stable_id("entity", file_record.file_id, "Document", storage_relative_path),
            snapshot_id=snapshot_id,
            file_id=file_record.file_id,
            entity_type="Document",
            name=title,
            qualified_name=storage_relative_path,
            path=path,
            source_scope=entry.category,
            profile_id=ALL_SCOPE,
            start_line=None if primary_chunk is None else primary_chunk.start_line,
            end_line=None if primary_chunk is None else primary_chunk.end_line,
            metadata={
                **dict(doc_metadata),
                "summary": _summary_from_text(
                    None if primary_chunk is None else primary_chunk.text
                ),
            },
        )
        evidence = None
        if primary_chunk is not None:
            evidence = self._make_evidence(
                snapshot_id=snapshot_id,
                object_type="entity",
                object_id=entity.entity_id,
                file_record=file_record,
                chunk_record=primary_chunk,
                entry=entry,
                storage_relative_path=storage_relative_path,
                citation_title=title,
            )
        return entity, evidence

    def _build_item_records(
        self,
        *,
        snapshot_id: str,
        file_record: FileRecord,
        entry: SourceDocEntry,
        parsed: ParsedDocument,
        doc_metadata: Mapping[str, object],
        storage_relative_path: str,
        base_chunks: Sequence[ChunkRecord],
    ) -> tuple[tuple[ChunkRecord, ...], tuple[EntityRecord, ...], tuple[EvidenceRecord, ...]]:
        synthetic_chunks: list[ChunkRecord] = []
        entities: list[EntityRecord] = []
        evidences: list[EvidenceRecord] = []
        front_matter = parsed.front_matter
        next_ordinal = len(base_chunks)
        selected_chunks = {chunk.ordinal: chunk for chunk in base_chunks}
        primary_chunk = _select_primary_chunk(parsed.chunks, selected_chunks)

        if front_matter is None:
            return (), (), ()

        for item in _iter_api_items(front_matter):
            matched_parsed = _select_chunk_for_item(item, parsed.chunks) or _fallback_parsed_chunk(
                parsed.chunks
            )
            base_chunk = (
                None if matched_parsed is None else selected_chunks.get(matched_parsed.ordinal)
            )
            if base_chunk is None:
                base_chunk = primary_chunk
            if base_chunk is None:
                continue
            synthetic_chunk = self._make_synthetic_chunk(
                snapshot_id=snapshot_id,
                file_record=file_record,
                entry=entry,
                base_chunk=base_chunk,
                item_name=item,
                item_type="api_item",
                ordinal=next_ordinal,
                doc_metadata=doc_metadata,
                storage_relative_path=storage_relative_path,
            )
            next_ordinal += 1
            synthetic_chunks.append(synthetic_chunk)
            entity = EntityRecord(
                entity_id=_stable_id("entity", file_record.file_id, "API", item),
                snapshot_id=snapshot_id,
                file_id=file_record.file_id,
                entity_type="API",
                name=item,
                qualified_name=_qualified_api_name(front_matter, item),
                path=_path_with_anchor(storage_relative_path, _anchor_from_name(item)),
                source_scope=entry.category,
                profile_id=ALL_SCOPE,
                start_line=synthetic_chunk.start_line,
                end_line=synthetic_chunk.end_line,
                metadata={
                    **dict(doc_metadata),
                    "aliases": [item.replace("_", " ")],
                    "summary": _summary_from_text(synthetic_chunk.text),
                    "source_chunk_id": base_chunk.chunk_id,
                },
            )
            entities.append(entity)
            evidences.append(
                self._make_evidence(
                    snapshot_id=snapshot_id,
                    object_type="entity",
                    object_id=entity.entity_id,
                    file_record=file_record,
                    chunk_record=synthetic_chunk,
                    entry=entry,
                    storage_relative_path=storage_relative_path,
                    citation_title=f"{doc_metadata['title']} > {item}",
                )
            )

        for item in _iter_widget_items(front_matter):
            matched_parsed = _select_chunk_for_item(item, parsed.chunks) or _fallback_parsed_chunk(
                parsed.chunks
            )
            base_chunk = (
                None if matched_parsed is None else selected_chunks.get(matched_parsed.ordinal)
            )
            if base_chunk is None:
                base_chunk = primary_chunk
            if base_chunk is None:
                continue
            synthetic_chunk = self._make_synthetic_chunk(
                snapshot_id=snapshot_id,
                file_record=file_record,
                entry=entry,
                base_chunk=base_chunk,
                item_name=item,
                item_type="widget_item",
                ordinal=next_ordinal,
                doc_metadata=doc_metadata,
                storage_relative_path=storage_relative_path,
            )
            next_ordinal += 1
            synthetic_chunks.append(synthetic_chunk)
            entity = EntityRecord(
                entity_id=_stable_id("entity", file_record.file_id, "Widget", item),
                snapshot_id=snapshot_id,
                file_id=file_record.file_id,
                entity_type="Widget",
                name=item,
                qualified_name=_qualified_widget_name(front_matter, item),
                path=_path_with_anchor(storage_relative_path, _anchor_from_name(item)),
                source_scope=entry.category,
                profile_id=ALL_SCOPE,
                start_line=synthetic_chunk.start_line,
                end_line=synthetic_chunk.end_line,
                metadata={
                    **dict(doc_metadata),
                    "summary": _summary_from_text(synthetic_chunk.text),
                    "source_chunk_id": base_chunk.chunk_id,
                },
            )
            entities.append(entity)
            evidences.append(
                self._make_evidence(
                    snapshot_id=snapshot_id,
                    object_type="entity",
                    object_id=entity.entity_id,
                    file_record=file_record,
                    chunk_record=synthetic_chunk,
                    entry=entry,
                    storage_relative_path=storage_relative_path,
                    citation_title=f"{doc_metadata['title']} > {item}",
                )
            )

        return tuple(synthetic_chunks), tuple(entities), tuple(evidences)

    def _make_synthetic_chunk(
        self,
        *,
        snapshot_id: str,
        file_record: FileRecord,
        entry: SourceDocEntry,
        base_chunk: ChunkRecord,
        item_name: str,
        item_type: str,
        ordinal: int,
        doc_metadata: Mapping[str, object],
        storage_relative_path: str,
    ) -> ChunkRecord:
        content_hash = _hash_jsonable(
            {
                "base_chunk_id": base_chunk.chunk_id,
                "item_name": item_name,
                "item_type": item_type,
                "text": base_chunk.text,
            }
        )
        metadata = {
            **dict(base_chunk.metadata),
            **dict(doc_metadata),
            "path": storage_relative_path,
            "item_name": item_name,
            "item_type": item_type,
            "source_chunk_id": base_chunk.chunk_id,
        }
        return ChunkRecord(
            chunk_id=_stable_id("chunk", file_record.file_id, item_type, item_name),
            snapshot_id=snapshot_id,
            file_id=file_record.file_id,
            content_hash=content_hash,
            chunk_type=f"doc.{item_type}",
            ordinal=ordinal,
            text=base_chunk.text,
            source_scope=entry.category,
            profile_id=ALL_SCOPE,
            start_line=base_chunk.start_line,
            end_line=base_chunk.end_line,
            metadata=metadata,
        )

    def _make_evidence(
        self,
        *,
        snapshot_id: str,
        object_type: str,
        object_id: str,
        file_record: FileRecord,
        chunk_record: ChunkRecord,
        entry: SourceDocEntry,
        storage_relative_path: str,
        citation_title: str,
    ) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=_stable_id("evidence", object_type, object_id, chunk_record.chunk_id),
            snapshot_id=snapshot_id,
            object_type=object_type,
            object_id=object_id,
            file_id=file_record.file_id,
            source_scope=entry.category,
            profile_id=ALL_SCOPE,
            chunk_id=chunk_record.chunk_id,
            excerpt=self._secret_scanner.sanitize_excerpt(
                _summary_from_text(chunk_record.text, limit=220)
            ),
            citation_label=f"{citation_title} [{storage_relative_path}:{chunk_record.start_line}]",
            start_line=chunk_record.start_line,
            end_line=chunk_record.end_line,
            metadata={
                "path": storage_relative_path,
                "title": citation_title,
                "doc_type": chunk_record.metadata.get("doc_type"),
                "domain": chunk_record.metadata.get("domain"),
            },
        )

    def _build_vector_write(self, chunk_record: ChunkRecord) -> VectorWrite:
        return VectorWrite(
            record=VectorRefRecord(
                vector_ref_id=_stable_id(
                    "vector_ref", chunk_record.chunk_id, self.embedding_model_version
                ),
                object_type="chunk",
                object_id=chunk_record.chunk_id,
                chunk_id=chunk_record.chunk_id,
                embedding_model_version=self.embedding_model_version,
                content_hash=chunk_record.content_hash,
                source_scope=chunk_record.source_scope,
                profile_id=chunk_record.profile_id,
                metadata={
                    "provider": self.embedding_provider,
                    "doc_type": chunk_record.metadata.get("doc_type"),
                    "domain": chunk_record.metadata.get("domain"),
                    "title": chunk_record.metadata.get("title"),
                },
            ),
            embedding=self.embed_text(chunk_record.text),
        )


def _source_id_for_category(category: str) -> str:
    return f"{_SOURCE_ID_PREFIX}{category}"


def _storage_relative_path(manifest: SourceDocsManifest, entry: SourceDocEntry) -> str:
    root_name = Path(manifest.source_docs_root).name or "knowledge-sources"
    return f"{root_name}/{entry.relative_path}"


def _warning_from_source_docs_warning(warning: SourceDocsWarning) -> DocumentIndexingWarning:
    return DocumentIndexingWarning(
        code=warning.code,
        message=warning.message,
        relative_path=warning.display_path,
        level="caution",
        details=warning.details,
    )


def _warning_from_parse_warning(
    relative_path: str,
    warning: DocumentParseWarning,
) -> DocumentIndexingWarning:
    details = dict(warning.details)
    if warning.line_number is not None:
        details["line_number"] = warning.line_number
    return DocumentIndexingWarning(
        code=warning.code,
        message=warning.message,
        relative_path=relative_path,
        level="caution",
        details=details,
    )


def _build_doc_metadata(
    absolute_path: Path,
    entry: SourceDocEntry,
    parsed: ParsedDocument,
) -> dict[str, object]:
    front_matter = parsed.front_matter
    title = _string_from_front_matter(front_matter, "title") or parsed.title or absolute_path.stem
    authority_level = _string_from_front_matter(front_matter, "authority_level") or "source_doc"
    version = _string_from_front_matter(front_matter, "version")
    profiles = _string_list_from_front_matter(front_matter, "profiles")
    tags = _string_list_from_front_matter(front_matter, "tags")
    doc_type = _doc_type_for_category(entry.category)
    domain = _domain_for_category(entry.category, front_matter)
    freshness_ts, freshness_source = _resolve_freshness(absolute_path, front_matter)
    metadata: dict[str, object] = {
        "title": title,
        "authority_level": authority_level,
        "version": version,
        "profiles": profiles,
        "tags": tags,
        "doc_type": doc_type,
        "domain": domain,
        "freshness_ts": freshness_ts,
        "freshness_source": freshness_source,
        "category": entry.category,
        "source_size_bytes": entry.size_bytes,
        "format": parsed.format,
    }
    if front_matter is not None:
        for key, value in front_matter.known_fields.items():
            if key in metadata:
                continue
            metadata[key] = _storage_safe_value(value)
        for key, value in front_matter.extension_fields.items():
            metadata[key] = _storage_safe_value(value)
    return metadata


def _doc_type_for_category(category: str) -> str:
    if category == "widgets":
        return "widget"
    return category


def _domain_for_category(category: str, front_matter: ParsedFrontMatter | None) -> str:
    if front_matter is not None:
        extension_value = front_matter.extension_fields.get("domain")
        if isinstance(extension_value, str) and extension_value.strip():
            return extension_value.strip()
    return _CATEGORY_DOMAIN_OVERRIDES.get(category, category)


def _resolve_freshness(
    absolute_path: Path,
    front_matter: ParsedFrontMatter | None,
) -> tuple[str, str]:
    if front_matter is not None:
        for key in _FRESHNESS_FRONT_MATTER_KEYS:
            value = front_matter.known_fields.get(key, front_matter.extension_fields.get(key))
            if isinstance(value, str) and value.strip():
                return value.strip(), "front_matter"
    stat = absolute_path.stat()
    freshness = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat().replace("+00:00", "Z")
    return freshness, "mtime"


def _storage_safe_value(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_storage_safe_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _storage_safe_value(item) for key, item in value.items()}
    return str(value)


def _string_from_front_matter(front_matter: ParsedFrontMatter | None, key: str) -> str | None:
    if front_matter is None:
        return None
    value = front_matter.known_fields.get(key)
    return value if isinstance(value, str) and value else None


def _string_list_from_front_matter(front_matter: ParsedFrontMatter | None, key: str) -> list[str]:
    if front_matter is None:
        return []
    value = front_matter.known_fields.get(key)
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _iter_api_items(front_matter: ParsedFrontMatter) -> tuple[str, ...]:
    value = front_matter.known_fields.get("code_symbols")
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _iter_widget_items(front_matter: ParsedFrontMatter) -> tuple[str, ...]:
    value = front_matter.known_fields.get("widget")
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _qualified_api_name(front_matter: ParsedFrontMatter, item: str) -> str:
    module = front_matter.known_fields.get("module")
    if isinstance(module, str) and module.strip():
        return f"{module.strip()}.{item}"
    return item


def _qualified_widget_name(front_matter: ParsedFrontMatter, item: str) -> str:
    ui_framework = front_matter.known_fields.get("ui_framework")
    if isinstance(ui_framework, str) and ui_framework.strip():
        return f"{ui_framework.strip()}.{item}"
    return item


def _fallback_parsed_chunk(chunks: Sequence[ParsedChunk]) -> ParsedChunk | None:
    if not chunks:
        return None
    non_lead = [chunk for chunk in chunks if not chunk.chunk_type.endswith(".lead")]
    if non_lead:
        return non_lead[0]
    return chunks[0]


def _select_primary_chunk(
    parsed_chunks: Sequence[ParsedChunk],
    chunk_by_ordinal: Mapping[int, ChunkRecord],
) -> ChunkRecord | None:
    selected = _fallback_parsed_chunk(parsed_chunks)
    if selected is None:
        return None
    return chunk_by_ordinal.get(selected.ordinal)


def _select_chunk_for_item(item_name: str, chunks: Sequence[ParsedChunk]) -> ParsedChunk | None:
    normalized_item = _normalize_text(item_name)
    if not normalized_item:
        return None

    best: tuple[int, int, ParsedChunk] | None = None
    for chunk in chunks:
        score = 0
        if chunk.heading_path:
            heading_tail = _normalize_text(chunk.heading_path[-1])
            if heading_tail == normalized_item:
                score += 6
            elif normalized_item in heading_tail or heading_tail in normalized_item:
                score += 4
        if chunk.anchor is not None and chunk.anchor == _anchor_from_name(item_name):
            score += 4
        normalized_body = _normalize_text(chunk.text)
        if normalized_item in normalized_body:
            score += 2
        if score == 0:
            continue
        candidate = (score, -chunk.ordinal, chunk)
        if best is None or candidate > best:
            best = candidate
    return None if best is None else best[2]


def _normalize_text(text: str) -> str:
    return " ".join(_TOKEN_RE.findall(text.lower()))


def _anchor_from_name(name: str) -> str:
    normalized = _normalize_text(name).replace(" ", "-")
    return normalized or "item"


def _path_with_anchor(path: str, anchor: object) -> str:
    if isinstance(anchor, str) and anchor:
        return f"{path}#{anchor}"
    return path


def _summary_from_text(text: str | None, *, limit: int = 180) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _hash_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _hash_jsonable(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _write_in_batches(
    records: Sequence[_T],
    *,
    batch_size: int,
    commit_interval_ms: int,
    transaction: Callable[[], AbstractContextManager[object]],
    write_one: Callable[[_T], None],
) -> None:
    size = max(batch_size, 1)
    interval_seconds = max(commit_interval_ms, 1) / 1000.0
    active_transaction: AbstractContextManager[object] | None = None
    records_in_transaction = 0
    transaction_started_at = 0.0
    try:
        for record in records:
            if active_transaction is None:
                active_transaction = transaction()
                active_transaction.__enter__()
                records_in_transaction = 0
                transaction_started_at = time.monotonic()
            write_one(record)
            records_in_transaction += 1
            if (
                records_in_transaction >= size
                or time.monotonic() - transaction_started_at >= interval_seconds
            ):
                completed_transaction = active_transaction
                active_transaction = None
                completed_transaction.__exit__(None, None, None)
        if active_transaction is not None:
            completed_transaction = active_transaction
            active_transaction = None
            completed_transaction.__exit__(None, None, None)
    except BaseException as exc:
        if active_transaction is not None:
            active_transaction.__exit__(type(exc), exc, exc.__traceback__)
        raise
