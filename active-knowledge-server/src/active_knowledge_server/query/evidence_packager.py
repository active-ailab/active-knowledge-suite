"""Evidence packaging helpers for query results and evidence bundle flows."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.models.evidence import EvidenceRef
from active_knowledge_server.query.rerank import FusionCandidate
from active_knowledge_server.security.secret_scan import SecretScanner
from active_knowledge_server.storage import (
	ChunkRecord,
	EntityRecord,
	FileRecord,
	LogicalEvidence,
	QueryScope,
	StorageAdapter,
	StorageReader,
)

_DEFAULT_EXCERPT_LIMIT: int = 220


@dataclass(frozen=True)
class _EvidenceCatalog:
	by_evidence_id: dict[str, tuple[LogicalEvidence, ...]]
	by_chunk_id: dict[str, tuple[LogicalEvidence, ...]]
	by_object_id: dict[str, tuple[LogicalEvidence, ...]]
	by_file_id: dict[str, tuple[LogicalEvidence, ...]]


class EvidencePackager:
	"""Resolve stable, short evidence references from ranked query candidates."""

	def __init__(
		self,
		*,
		reader: StorageReader | None = None,
		secret_scanner: SecretScanner | None = None,
		max_evidence_items: int = 20,
		max_excerpt_chars: int = _DEFAULT_EXCERPT_LIMIT,
	) -> None:
		self._reader = reader
		self._secret_scanner = secret_scanner or SecretScanner(enabled=True)
		self._max_evidence_items = max(1, max_evidence_items)
		self._max_excerpt_chars = max(32, max_excerpt_chars)

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		metadata_adapter: StorageAdapter | None = None,
		secret_scanner: SecretScanner | None = None,
	) -> EvidencePackager:
		reader = None if metadata_adapter is None else metadata_adapter.reader()
		return cls(
			reader=reader,
			secret_scanner=secret_scanner or SecretScanner.from_config(config),
			max_evidence_items=config.query.max_evidence_items,
		)

	def bundle_for_query(
		self,
		*,
		scope: QueryScope,
		candidates: Sequence[FusionCandidate],
	) -> tuple[tuple[EvidenceRef, ...], list[dict[str, object]]]:
		if not candidates:
			return (), []

		catalog = self._logical_evidence_catalog(scope)
		evidence_by_key: dict[tuple[str, str, int | None, int | None], EvidenceRef] = {}
		evidence_trace_by_key: dict[tuple[str, str, int | None, int | None], dict[str, object]] = {}

		for candidate in candidates:
			candidate_refs = self._candidate_evidence_refs(
				candidate=candidate,
				scope=scope,
				catalog=catalog,
			)
			if not candidate_refs:
				fallback = self._fallback_candidate_ref(candidate)
				candidate_refs = () if fallback is None else (fallback,)

			for evidence_ref in candidate_refs:
				dedupe_key = (
					evidence_ref.type,
					evidence_ref.path,
					evidence_ref.start_line,
					evidence_ref.end_line,
				)
				if dedupe_key not in evidence_by_key:
					evidence_by_key[dedupe_key] = evidence_ref
					evidence_trace_by_key[dedupe_key] = {
						"evidence_id": evidence_ref.evidence_id,
						"type": evidence_ref.type,
						"path": evidence_ref.path,
						"score": round(candidate.rerank_score, 6),
						"retrieval_sources": sorted(
							{signal.retriever for signal in candidate.retrieval_signals}
						),
						"candidate_ids": [candidate.candidate_id],
					}
					continue
				trace = evidence_trace_by_key[dedupe_key]
				trace["score"] = max(float(trace["score"]), round(candidate.rerank_score, 6))
				trace["retrieval_sources"] = sorted(
					set(trace["retrieval_sources"])
					| {signal.retriever for signal in candidate.retrieval_signals}
				)
				trace["candidate_ids"] = sorted(
					set(trace["candidate_ids"]) | {candidate.candidate_id}
				)

		limited_refs = tuple(evidence_by_key.values())[: self._max_evidence_items]
		limited_trace = list(evidence_trace_by_key.values())[: self._max_evidence_items]
		return limited_refs, limited_trace

	def bundle_for_entity(
		self,
		*,
		scope: QueryScope,
		entity_id: str,
	) -> tuple[EvidenceRef, ...]:
		if not entity_id.strip():
			return ()
		catalog = self._logical_evidence_catalog(scope)
		refs: list[EvidenceRef] = []
		seen: set[tuple[str, str, int | None, int | None]] = set()
		for logical in self._catalog_entries(catalog, entity_id):
			evidence_ref = self._evidence_ref_from_logical(logical)
			if evidence_ref is None:
				continue
			key = (
				evidence_ref.type,
				evidence_ref.path,
				evidence_ref.start_line,
				evidence_ref.end_line,
			)
			if key in seen:
				continue
			seen.add(key)
			refs.append(evidence_ref)
			if len(refs) >= self._max_evidence_items:
				return tuple(refs)
		fallback = self._fallback_entity_ref(entity_id)
		if fallback is not None and len(refs) < self._max_evidence_items:
			key = (fallback.type, fallback.path, fallback.start_line, fallback.end_line)
			if key not in seen:
				refs.append(fallback)
		return tuple(refs)

	def _candidate_evidence_refs(
		self,
		*,
		candidate: FusionCandidate,
		scope: QueryScope,
		catalog: _EvidenceCatalog | None,
	) -> tuple[EvidenceRef, ...]:
		refs: list[EvidenceRef] = []
		seen_ids: set[str] = set()
		seen_keys: set[tuple[str, str, int | None, int | None]] = set()

		for evidence_key in candidate.evidence_keys:
			for logical in self._catalog_entries(catalog, evidence_key):
				if logical.logical_object_id in seen_ids:
					continue
				evidence_ref = self._evidence_ref_from_logical(logical)
				if evidence_ref is None:
					continue
				seen_ids.add(logical.logical_object_id)
				key = (
					evidence_ref.type,
					evidence_ref.path,
					evidence_ref.start_line,
					evidence_ref.end_line,
				)
				if key in seen_keys:
					continue
				seen_keys.add(key)
				refs.append(evidence_ref)
				if len(refs) >= self._max_evidence_items:
					return tuple(refs)

			fallback = self._fallback_key_ref(evidence_key=evidence_key, candidate=candidate, scope=scope)
			if fallback is None:
				continue
			key = (fallback.type, fallback.path, fallback.start_line, fallback.end_line)
			if key in seen_keys:
				continue
			seen_keys.add(key)
			refs.append(fallback)
			if len(refs) >= self._max_evidence_items:
				return tuple(refs)

		return tuple(refs)

	def _logical_evidence_catalog(self, scope: QueryScope) -> _EvidenceCatalog | None:
		reader = self._reader
		logical_evidence = getattr(reader, "logical_evidence", None)
		if reader is None or not callable(logical_evidence):
			return None
		by_evidence_id: dict[str, list[LogicalEvidence]] = defaultdict(list)
		by_chunk_id: dict[str, list[LogicalEvidence]] = defaultdict(list)
		by_object_id: dict[str, list[LogicalEvidence]] = defaultdict(list)
		by_file_id: dict[str, list[LogicalEvidence]] = defaultdict(list)
		for logical in logical_evidence(scope):
			by_evidence_id[logical.logical_object_id].append(logical)
			record = logical.record
			if record.chunk_id:
				by_chunk_id[record.chunk_id].append(logical)
			by_object_id[record.object_id].append(logical)
			by_file_id[record.file_id].append(logical)
		return _EvidenceCatalog(
			by_evidence_id={key: tuple(value) for key, value in by_evidence_id.items()},
			by_chunk_id={key: tuple(value) for key, value in by_chunk_id.items()},
			by_object_id={key: tuple(value) for key, value in by_object_id.items()},
			by_file_id={key: tuple(value) for key, value in by_file_id.items()},
		)

	def _catalog_entries(
		self,
		catalog: _EvidenceCatalog | None,
		evidence_key: str,
	) -> tuple[LogicalEvidence, ...]:
		if catalog is None:
			return ()
		entries: list[LogicalEvidence] = []
		seen: set[str] = set()
		for bucket in (
			catalog.by_evidence_id.get(evidence_key, ()),
			catalog.by_chunk_id.get(evidence_key, ()),
			catalog.by_object_id.get(evidence_key, ()),
			catalog.by_file_id.get(evidence_key, ()),
		):
			for logical in bucket:
				if logical.logical_object_id in seen:
					continue
				seen.add(logical.logical_object_id)
				entries.append(logical)
		return tuple(entries)

	def _evidence_ref_from_logical(self, logical: LogicalEvidence) -> EvidenceRef | None:
		record = logical.record
		chunk = self._safe_get(record.chunk_id, getter_name="get_chunk") if record.chunk_id else None
		file_record = self._safe_get(record.file_id, getter_name="get_file")
		entity = None
		if record.object_type == "entity":
			entity = self._safe_get(record.object_id, getter_name="get_entity")

		path = (
			_mapping_text(record.metadata, "path")
			or (None if file_record is None else file_record.relative_path)
			or record.citation_label
		)
		if path is None:
			return None

		start_line = record.start_line
		if start_line is None and chunk is not None:
			start_line = chunk.start_line
		if start_line is None and entity is not None:
			start_line = entity.start_line

		end_line = record.end_line
		if end_line is None and chunk is not None:
			end_line = chunk.end_line
		if end_line is None and entity is not None:
			end_line = entity.end_line

		excerpt_source = record.excerpt
		if excerpt_source is None and chunk is not None:
			excerpt_source = chunk.text
		excerpt = self._finalize_excerpt(excerpt_source)
		content_hash = (
			_mapping_text(record.metadata, "content_hash")
			or (None if chunk is None else chunk.content_hash)
			or (None if file_record is None else file_record.content_hash)
			or _hash_text(excerpt)
		)
		return EvidenceRef(
			evidence_id=record.evidence_id,
			type=_evidence_type(path=path, object_type=record.object_type),
			path=path,
			start_line=start_line,
			end_line=end_line,
			authority_level=_authority_level(path=path, metadata=record.metadata),
			excerpt=excerpt,
			content_hash=content_hash,
			source_index=logical.source_index,
		)

	def _fallback_key_ref(
		self,
		*,
		evidence_key: str,
		candidate: FusionCandidate,
		scope: QueryScope,
	) -> EvidenceRef | None:
		chunk = self._safe_get(evidence_key, getter_name="get_chunk")
		if chunk is not None:
			return self._chunk_ref(
				evidence_id=evidence_key,
				chunk=chunk,
				candidate=candidate,
			)
		entity = self._safe_get(evidence_key, getter_name="get_entity")
		if entity is not None:
			return self._entity_ref(
				evidence_id=evidence_key,
				entity=entity,
				candidate=candidate,
			)
		file_record = self._safe_get(evidence_key, getter_name="get_file")
		if file_record is not None:
			return self._file_ref(
				evidence_id=evidence_key,
				file_record=file_record,
				candidate=candidate,
			)
		return None

	def _fallback_candidate_ref(self, candidate: FusionCandidate) -> EvidenceRef | None:
		path = (
			candidate.relative_path
			or _mapping_text(candidate.metadata, "path")
			or candidate.title
			or candidate.candidate_id
		)
		excerpt = self._finalize_excerpt(
			candidate.snippet
			or _mapping_text(candidate.metadata, "summary")
			or _mapping_text(candidate.metadata, "qualified_name")
			or candidate.title
		)
		if not path:
			return None
		return EvidenceRef(
			evidence_id=(
				candidate.evidence_keys[0]
				if candidate.evidence_keys
				else f"synthetic:{candidate.candidate_id}"
			),
			type=_evidence_type(path=path, object_type=candidate.object_type),
			path=path,
			start_line=_mapping_int(candidate.metadata, "start_line"),
			end_line=_mapping_int(candidate.metadata, "end_line"),
			authority_level=candidate.authority_level,
			excerpt=excerpt,
			content_hash=(
				_mapping_text(candidate.metadata, "content_hash") or _hash_text(excerpt)
			),
			source_index=(
				candidate.source_index
				if candidate.source_index in {"baseline", "overlay", "merged"}
				else None
			),
		)

	def _fallback_entity_ref(self, entity_id: str) -> EvidenceRef | None:
		entity = self._safe_get(entity_id, getter_name="get_entity")
		if entity is None:
			return None
		return self._entity_ref(
			evidence_id=f"synthetic:{entity.entity_id}",
			entity=entity,
			candidate=None,
		)

	def _chunk_ref(
		self,
		*,
		evidence_id: str,
		chunk: ChunkRecord,
		candidate: FusionCandidate | None,
	) -> EvidenceRef:
		file_record = self._safe_get(chunk.file_id, getter_name="get_file")
		path = (
			None if file_record is None else file_record.relative_path
		) or (None if candidate is None else candidate.relative_path) or evidence_id
		return EvidenceRef(
			evidence_id=evidence_id,
			type=_evidence_type(path=path, object_type=chunk.chunk_type),
			path=path,
			start_line=chunk.start_line,
			end_line=chunk.end_line,
			authority_level=_authority_level(
				path=path,
				metadata=chunk.metadata,
				fallback=(None if candidate is None else candidate.authority_level),
			),
			excerpt=self._finalize_excerpt(chunk.text),
			content_hash=chunk.content_hash,
		)

	def _entity_ref(
		self,
		*,
		evidence_id: str,
		entity: EntityRecord,
		candidate: FusionCandidate | None,
	) -> EvidenceRef:
		file_record = self._safe_get(entity.file_id, getter_name="get_file")
		path = (
			None if file_record is None else file_record.relative_path
		) or entity.path or (None if candidate is None else candidate.relative_path) or entity.entity_id
		excerpt = self._finalize_excerpt(
			_mapping_text(entity.metadata, "summary")
			or (None if candidate is None else candidate.snippet)
			or entity.qualified_name
		)
		content_hash = None if file_record is None else file_record.content_hash
		if content_hash is None:
			content_hash = _hash_text(excerpt)
		return EvidenceRef(
			evidence_id=evidence_id,
			type=_evidence_type(path=path, object_type=entity.entity_type),
			path=path,
			start_line=entity.start_line,
			end_line=entity.end_line,
			authority_level=_authority_level(
				path=path,
				metadata=entity.metadata,
				fallback=(None if candidate is None else candidate.authority_level),
			),
			excerpt=excerpt,
			content_hash=content_hash,
		)

	def _file_ref(
		self,
		*,
		evidence_id: str,
		file_record: FileRecord,
		candidate: FusionCandidate | None,
	) -> EvidenceRef:
		excerpt = self._finalize_excerpt(
			(None if candidate is None else candidate.snippet)
			or _mapping_text(file_record.metadata, "summary")
			or file_record.relative_path
		)
		return EvidenceRef(
			evidence_id=evidence_id,
			type=_evidence_type(path=file_record.relative_path, object_type="file"),
			path=file_record.relative_path,
			authority_level=_authority_level(
				path=file_record.relative_path,
				metadata=file_record.metadata,
				fallback=(None if candidate is None else candidate.authority_level),
			),
			excerpt=excerpt,
			content_hash=file_record.content_hash,
		)

	def _finalize_excerpt(self, excerpt: str | None) -> str | None:
		summary = _summary_from_text(excerpt, limit=self._max_excerpt_chars)
		if not summary:
			return None
		sanitized = self._secret_scanner.sanitize_excerpt(summary)
		if len(sanitized) <= self._max_excerpt_chars:
			return sanitized
		return _summary_from_text(sanitized, limit=self._max_excerpt_chars)

	def _safe_get(self, object_id: str, *, getter_name: str) -> Any | None:
		reader = self._reader
		getter = None if reader is None else getattr(reader, getter_name, None)
		if not callable(getter):
			return None
		return getter(object_id)


def _evidence_type(*, path: str, object_type: str) -> str:
	lowered = path.lower()
	if lowered.endswith(".config") or lowered.endswith("defconfig"):
		return "config"
	if lowered.startswith("knowledge-sources/"):
		return "doc"
	if object_type == "relation":
		return "graph"
	if object_type in {"layer", "feature", "workspace"}:
		return "workspace"
	if object_type == "profile":
		return "profile"
	return "code"


def _authority_level(
	*,
	path: str,
	metadata: Mapping[str, object],
	fallback: str | None = None,
) -> str:
	authority = _mapping_text(metadata, "authority_level")
	if authority is not None:
		return authority
	if fallback is not None and fallback.strip():
		return fallback.strip()
	if path.startswith("knowledge-sources/"):
		return "source_doc"
	if path.endswith(".config") or path.endswith("defconfig"):
		return "profile_config"
	return "workspace_code"


def _mapping_text(metadata: Mapping[str, object], key: str) -> str | None:
	value = metadata.get(key)
	if isinstance(value, str) and value.strip():
		return value.strip()
	return None


def _mapping_int(metadata: Mapping[str, object], key: str) -> int | None:
	value = metadata.get(key)
	if isinstance(value, int) and value > 0:
		return value
	return None


def _summary_from_text(text: str | None, *, limit: int) -> str:
	if not text:
		return ""
	compact = " ".join(text.split())
	if len(compact) <= limit:
		return compact
	return f"{compact[: limit - 3].rstrip()}..."


def _hash_text(text: str | None) -> str | None:
	if not text:
		return None
	payload = json.dumps(text, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
	return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"