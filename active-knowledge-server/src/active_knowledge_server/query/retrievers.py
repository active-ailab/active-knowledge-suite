"""Symbol-first retrieval primitives for the query service."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Final, Literal, cast

from active_knowledge_server.storage import (
	ALL_SCOPE,
	FTSQuery,
	FileRecord,
	LogicalEntity,
	LogicalEvidence,
	LogicalRelation,
	QueryScope,
	StorageAdapter,
	StorageReader,
)

RequestedSymbolEntityType = Literal[
	"auto",
	"symbol",
	"function",
	"macro",
	"type",
	"file",
	"module",
	"directory",
]
ResolvedSymbolEntityType = Literal["Function", "Macro", "Type", "File", "Module", "Directory"]
SymbolMatchKind = Literal["exact", "fuzzy", "alias", "doc_mention"]

_LOOKUP_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9]+")
_DOC_MENTION_TOP_K: Final = 24
_ENTITY_FTS_TOP_K: Final = 24
_FUZZY_RATIO_THRESHOLD: Final = 0.72
_ENTITY_TYPE_FILTERS: Final[dict[str, tuple[ResolvedSymbolEntityType, ...]]] = {
	"auto": ("Function", "Macro", "Type", "File", "Module", "Directory"),
	"symbol": ("Function", "Macro", "Type"),
	"function": ("Function",),
	"macro": ("Macro",),
	"type": ("Type",),
	"file": ("File",),
	"module": ("Module",),
	"directory": ("Directory",),
}
_ENTITY_PRIORITY: Final[dict[ResolvedSymbolEntityType, int]] = {
	"Function": 0,
	"Macro": 1,
	"Type": 2,
	"File": 3,
	"Module": 4,
	"Directory": 5,
}


@dataclass(frozen=True)
class SymbolSearchRequest:
	"""Stable request for symbol-first retrieval."""

	query: str
	scope: QueryScope = field(default_factory=QueryScope)
	entity_type: RequestedSymbolEntityType | str | None = None
	top_k: int = 8
	include_doc_mentions: bool = True

	def __post_init__(self) -> None:
		if not self.query.strip():
			raise ValueError("query must not be empty")
		if self.top_k < 1:
			raise ValueError("top_k must be >= 1")

	@property
	def normalized_query(self) -> str:
		return normalize_lookup_text(self.query)

	def to_dict(self) -> dict[str, object]:
		return {
			"query": self.query,
			"normalized_query": self.normalized_query,
			"scope": {
				"snapshot_id": self.scope.snapshot_id,
				"profile_id": self.scope.profile_id,
				"source_scope": self.scope.source_scope,
				"path_scope": self.scope.path_scope,
				"include_inactive": self.scope.include_inactive,
			},
			"entity_type": self.entity_type,
			"top_k": self.top_k,
			"include_doc_mentions": self.include_doc_mentions,
		}


@dataclass(frozen=True)
class SymbolCandidate:
	"""One symbol-first candidate with explainable match labels."""

	logical_entity_id: str
	physical_entity_id: str
	source_index: str
	entity_type: ResolvedSymbolEntityType
	name: str
	qualified_name: str
	path: str
	relative_path: str | None
	file_id: str
	profile_id: str
	source_scope: str
	start_line: int | None
	end_line: int | None
	score: float
	match_kinds: tuple[SymbolMatchKind, ...]
	match_reason: str
	disambiguation_key: str
	aliases: tuple[str, ...] = ()
	module_names: tuple[str, ...] = ()
	evidence_ids: tuple[str, ...] = ()
	doc_mention_paths: tuple[str, ...] = ()
	metadata: dict[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		return {
			"logical_entity_id": self.logical_entity_id,
			"physical_entity_id": self.physical_entity_id,
			"source_index": self.source_index,
			"entity_type": self.entity_type,
			"name": self.name,
			"qualified_name": self.qualified_name,
			"path": self.path,
			"relative_path": self.relative_path,
			"file_id": self.file_id,
			"profile_id": self.profile_id,
			"source_scope": self.source_scope,
			"start_line": self.start_line,
			"end_line": self.end_line,
			"score": self.score,
			"match_kinds": list(self.match_kinds),
			"match_reason": self.match_reason,
			"disambiguation_key": self.disambiguation_key,
			"aliases": list(self.aliases),
			"module_names": list(self.module_names),
			"evidence_ids": list(self.evidence_ids),
			"doc_mention_paths": list(self.doc_mention_paths),
			"metadata": dict(self.metadata),
		}


@dataclass(frozen=True)
class SymbolSearchResult:
	"""One symbol retrieval response."""

	request: SymbolSearchRequest
	candidates: tuple[SymbolCandidate, ...]
	total_candidates: int

	@property
	def has_exact_match(self) -> bool:
		return any("exact" in item.match_kinds for item in self.candidates)

	def to_dict(self) -> dict[str, object]:
		return {
			"request": self.request.to_dict(),
			"total_candidates": self.total_candidates,
			"has_exact_match": self.has_exact_match,
			"candidates": [item.to_dict() for item in self.candidates],
		}


class SymbolRetriever:
	"""Resolve code entities from logical entities, FTS, and doc mentions."""

	def __init__(self, reader: StorageReader) -> None:
		self._reader = reader

	@classmethod
	def from_storage(cls, adapter: StorageAdapter) -> SymbolRetriever:
		return cls(adapter.reader())

	def search(self, request: SymbolSearchRequest) -> SymbolSearchResult:
		allowed_types = resolve_entity_filter(request.entity_type)
		logical_entities = tuple(
			item
			for item in self._reader.logical_entities(request.scope)
			if item.record.entity_type in allowed_types
		)
		if not logical_entities:
			return SymbolSearchResult(request=request, candidates=(), total_candidates=0)

		file_records = {record.file_id: record for record in self._reader.iter_files(request.scope)}
		evidence_ids_by_entity = self._evidence_ids_by_entity(request.scope)
		module_names_by_file_id = self._module_names_by_file_id(request.scope, logical_entities)
		fts_rank_by_entity_id = self._entity_fts_ranks(request)
		doc_mentions_by_entity = self._doc_mentions_by_entity(
			request,
			logical_entities=logical_entities,
			file_records=file_records,
		)

		candidates: list[SymbolCandidate] = []
		query_exact = normalize_exact_text(request.query)
		normalized_query = request.normalized_query

		for logical in logical_entities:
			candidate = self._candidate_from_entity(
				logical,
				query_exact=query_exact,
				normalized_query=normalized_query,
				request=request,
				file_records=file_records,
				evidence_ids_by_entity=evidence_ids_by_entity,
				module_names_by_file_id=module_names_by_file_id,
				fts_rank_by_entity_id=fts_rank_by_entity_id,
				doc_mentions_by_entity=doc_mentions_by_entity,
			)
			if candidate is not None:
				candidates.append(candidate)

		ordered = tuple(
			sorted(
				candidates,
				key=lambda item: (
					-item.score,
					_ENTITY_PRIORITY[item.entity_type],
					item.name,
					item.relative_path or item.path,
				),
			)
		)
		return SymbolSearchResult(
			request=request,
			candidates=ordered[: request.top_k],
			total_candidates=len(ordered),
		)

	def _candidate_from_entity(
		self,
		logical: LogicalEntity,
		*,
		query_exact: str,
		normalized_query: str,
		request: SymbolSearchRequest,
		file_records: dict[str, FileRecord],
		evidence_ids_by_entity: dict[str, tuple[str, ...]],
		module_names_by_file_id: dict[str, tuple[str, ...]],
		fts_rank_by_entity_id: dict[str, int],
		doc_mentions_by_entity: dict[str, tuple[str, ...]],
	) -> SymbolCandidate | None:
		record = logical.record
		entity_type = cast(ResolvedSymbolEntityType, record.entity_type)
		aliases = metadata_text_list(record.metadata, "aliases")
		file_record = file_records.get(record.file_id)
		relative_path = None if file_record is None else file_record.relative_path
		module_names = module_names_by_file_id.get(record.file_id, ())

		match_kinds: list[SymbolMatchKind] = []
		reasons: list[str] = []
		score = 0.0

		exact_score, exact_reason = self._exact_match_score(
			query_exact,
			record=record,
			relative_path=relative_path,
		)
		if exact_score > 0.0:
			match_kinds.append("exact")
			reasons.append(exact_reason)
			score = max(score, exact_score)

		alias_score, alias_reason = self._alias_match_score(
			query_exact,
			normalized_query,
			aliases,
		)
		if alias_score > 0.0:
			match_kinds.append("alias")
			reasons.append(alias_reason)
			score = max(score, alias_score)

		fuzzy_score, fuzzy_reason = self._fuzzy_match_score(
			normalized_query,
			record=record,
			relative_path=relative_path,
			aliases=aliases,
			fts_rank=fts_rank_by_entity_id.get(logical.logical_object_id),
		)
		if fuzzy_score > 0.0:
			match_kinds.append("fuzzy")
			reasons.append(fuzzy_reason)
			score = max(score, fuzzy_score)

		doc_mention_paths = doc_mentions_by_entity.get(logical.logical_object_id, ())
		if doc_mention_paths:
			doc_score = 0.62 if not match_kinds else 0.10
			match_kinds.append("doc_mention")
			reasons.append("mentioned by source documents linked through code_symbols")
			score += doc_score

		if not match_kinds:
			return None

		score = round(score, 6)
		return SymbolCandidate(
			logical_entity_id=logical.logical_object_id,
			physical_entity_id=logical.physical_object_id,
			source_index=logical.source_index,
			entity_type=entity_type,
			name=record.name,
			qualified_name=record.qualified_name,
			path=record.path,
			relative_path=relative_path,
			file_id=record.file_id,
			profile_id=record.profile_id,
			source_scope=record.source_scope,
			start_line=record.start_line,
			end_line=record.end_line,
			score=score,
			match_kinds=tuple(dict.fromkeys(match_kinds)),
			match_reason="; ".join(dict.fromkeys(reasons)),
			disambiguation_key=build_disambiguation_key(
				entity_type=entity_type,
				relative_path=relative_path,
				qualified_name=record.qualified_name,
				module_names=module_names,
				profile_id=record.profile_id,
			),
			aliases=aliases,
			module_names=module_names,
			evidence_ids=evidence_ids_by_entity.get(logical.logical_object_id, ()),
			doc_mention_paths=doc_mention_paths,
			metadata={
				"summary": record.metadata.get("summary"),
				"path": record.path,
				"replaced_from": list(logical.replaced_from),
			},
		)

	def _entity_fts_ranks(self, request: SymbolSearchRequest) -> dict[str, int]:
		fts_query = request.normalized_query or request.query
		matches = self._reader.search_fts(
			FTSQuery(
				index_name="entity_fts",
				query=fts_query,
				scope=request.scope,
				top_k=max(request.top_k * 4, _ENTITY_FTS_TOP_K),
			)
		)
		return {match.logical_object_id: rank for rank, match in enumerate(matches, start=1)}

	def _doc_mentions_by_entity(
		self,
		request: SymbolSearchRequest,
		*,
		logical_entities: tuple[LogicalEntity, ...],
		file_records: dict[str, FileRecord],
	) -> dict[str, tuple[str, ...]]:
		if not request.include_doc_mentions:
			return {}

		ids_by_symbol = build_entity_lookup(logical_entities)
		fts_query = request.normalized_query or request.query
		matches = self._reader.search_fts(
			FTSQuery(
				index_name="chunk_fts",
				query=fts_query,
				scope=request.scope,
				top_k=max(request.top_k * 6, _DOC_MENTION_TOP_K),
			)
		)
		mentions: dict[str, list[str]] = defaultdict(list)
		for match in matches:
			chunk_id = match.chunk_id or match.physical_object_id
			if chunk_id is None:
				continue
			chunk = self._reader.get_chunk(chunk_id)
			if chunk is None:
				continue
			file_record = file_records.get(chunk.file_id)
			if file_record is None or file_record.source_id == "workspace":
				continue
			symbol_names = (
				metadata_text_list(chunk.metadata, "code_symbols")
				or metadata_text_list(chunk.metadata, "symbol_names")
				or metadata_text_list(chunk.metadata, "symbols")
			)
			if not symbol_names:
				continue
			for symbol_name in symbol_names:
				for entity_id in ids_by_symbol.get(normalize_exact_text(symbol_name), ()):  # pragma: no branch
					mentions[entity_id].append(file_record.relative_path)
				for entity_id in ids_by_symbol.get(normalize_lookup_text(symbol_name), ()):  # pragma: no branch
					mentions[entity_id].append(file_record.relative_path)
		return {
			entity_id: tuple(dict.fromkeys(paths))
			for entity_id, paths in mentions.items()
		}

	def _evidence_ids_by_entity(self, scope: QueryScope) -> dict[str, tuple[str, ...]]:
		evidence_by_entity: dict[str, list[str]] = defaultdict(list)
		for item in self._reader.logical_evidence(scope):
			if item.record.object_type != "entity":
				continue
			evidence_by_entity[item.record.object_id].append(item.logical_object_id)
		return {
			entity_id: tuple(values)
			for entity_id, values in evidence_by_entity.items()
		}

	def _module_names_by_file_id(
		self,
		scope: QueryScope,
		logical_entities: tuple[LogicalEntity, ...],
	) -> dict[str, tuple[str, ...]]:
		file_entity_ids = {
			item.logical_object_id: item.record.file_id
			for item in logical_entities
			if item.record.entity_type == "File"
		}
		module_names = {
			item.logical_object_id: item.record.qualified_name or item.record.name
			for item in logical_entities
			if item.record.entity_type == "Module"
		}
		modules_by_file_id: dict[str, list[str]] = defaultdict(list)
		for relation in self._reader.logical_relations(scope):
			if relation.record.relation_type != "belongs_to_module":
				continue
			file_id = file_entity_ids.get(relation.record.src_entity_id)
			module_name = module_names.get(relation.record.dst_entity_id)
			if file_id is None or module_name is None:
				continue
			modules_by_file_id[file_id].append(module_name)
		return {
			file_id: tuple(dict.fromkeys(names))
			for file_id, names in modules_by_file_id.items()
		}

	@staticmethod
	def _exact_match_score(
		query_exact: str,
		*,
		record: object,
		relative_path: str | None,
	) -> tuple[float, str]:
		entity = cast(object, record)
		name = cast(str, getattr(entity, "name"))
		qualified_name = cast(str, getattr(entity, "qualified_name"))
		path = cast(str, getattr(entity, "path"))
		if query_exact == normalize_exact_text(name):
			return 1.0, "exact entity name match"
		if query_exact == normalize_exact_text(qualified_name):
			return 0.99, "exact qualified name match"
		if relative_path is not None and query_exact == normalize_exact_text(relative_path):
			return 0.98, "exact relative path match"
		if relative_path is not None and query_exact == normalize_exact_text(PurePosixPath(relative_path).name):
			return 0.97, "exact file name match"
		if query_exact == normalize_exact_text(path):
			return 0.96, "exact entity path match"
		return 0.0, ""

	@staticmethod
	def _alias_match_score(
		query_exact: str,
		normalized_query: str,
		aliases: tuple[str, ...],
	) -> tuple[float, str]:
		for alias in aliases:
			if query_exact == normalize_exact_text(alias) or normalized_query == normalize_lookup_text(alias):
				return 0.94, "matched indexed alias"
		return 0.0, ""

	@staticmethod
	def _fuzzy_match_score(
		normalized_query: str,
		*,
		record: object,
		relative_path: str | None,
		aliases: tuple[str, ...],
		fts_rank: int | None,
	) -> tuple[float, str]:
		entity = cast(object, record)
		targets = [
			normalize_lookup_text(cast(str, getattr(entity, "name"))),
			normalize_lookup_text(cast(str, getattr(entity, "qualified_name"))),
			normalize_lookup_text(cast(str, getattr(entity, "path"))),
		]
		if relative_path is not None:
			targets.append(normalize_lookup_text(relative_path))
		targets.extend(normalize_lookup_text(alias) for alias in aliases)
		ratio = max((fuzzy_similarity(normalized_query, target) for target in targets if target), default=0.0)
		if ratio < _FUZZY_RATIO_THRESHOLD and fts_rank is None:
			return 0.0, ""
		if fts_rank is None:
			return 0.72 + min(0.18, ratio * 0.18), "matched fuzzy token overlap"
		fts_score = 0.70 + (0.12 / float(fts_rank))
		if ratio >= _FUZZY_RATIO_THRESHOLD:
			return max(fts_score, 0.72 + min(0.18, ratio * 0.18)), "matched fuzzy name and entity FTS"
		return fts_score, "matched entity FTS candidate"


def resolve_entity_filter(
	entity_type: RequestedSymbolEntityType | str | None,
) -> tuple[ResolvedSymbolEntityType, ...]:
	key = "auto" if entity_type in (None, "") else str(entity_type).strip().lower()
	return _ENTITY_TYPE_FILTERS.get(key, _ENTITY_TYPE_FILTERS["auto"])


def normalize_exact_text(value: str) -> str:
	return " ".join(value.strip().lower().split())


def normalize_lookup_text(value: str) -> str:
	return " ".join(_LOOKUP_TOKEN_RE.findall(value.lower()))


def fuzzy_similarity(query: str, target: str) -> float:
	if not query or not target:
		return 0.0
	if query == target:
		return 1.0
	query_tokens = set(query.split())
	target_tokens = set(target.split())
	token_overlap = 0.0
	if query_tokens and target_tokens:
		token_overlap = len(query_tokens & target_tokens) / max(len(query_tokens), len(target_tokens))
	contains_bonus = 0.88 if query in target or target in query else 0.0
	return max(token_overlap, contains_bonus, SequenceMatcher(None, query, target).ratio())


def metadata_text_list(metadata: dict[str, object], key: str) -> tuple[str, ...]:
	value = metadata.get(key)
	if isinstance(value, str):
		return (value,) if value else ()
	if isinstance(value, (list, tuple)):
		values = [str(item).strip() for item in value if str(item).strip()]
		return tuple(values)
	return ()


def build_entity_lookup(
	logical_entities: tuple[LogicalEntity, ...],
) -> dict[str, tuple[str, ...]]:
	lookup: dict[str, list[str]] = defaultdict(list)
	for item in logical_entities:
		record = item.record
		terms = {
			normalize_exact_text(record.name),
			normalize_lookup_text(record.name),
			normalize_exact_text(record.qualified_name),
			normalize_lookup_text(record.qualified_name),
		}
		for alias in metadata_text_list(record.metadata, "aliases"):
			terms.add(normalize_exact_text(alias))
			terms.add(normalize_lookup_text(alias))
		for term in terms:
			if term:
				lookup[term].append(item.logical_object_id)
	return {term: tuple(dict.fromkeys(entity_ids)) for term, entity_ids in lookup.items()}


def build_disambiguation_key(
	*,
	entity_type: ResolvedSymbolEntityType,
	relative_path: str | None,
	qualified_name: str,
	module_names: tuple[str, ...],
	profile_id: str,
) -> str:
	parts = [entity_type, relative_path or qualified_name]
	if module_names:
		parts.append(f"module={module_names[0]}")
	if profile_id != ALL_SCOPE:
		parts.append(f"profile={profile_id}")
	return " | ".join(parts)
