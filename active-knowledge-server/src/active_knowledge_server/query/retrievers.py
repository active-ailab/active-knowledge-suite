"""Retrieval primitives for the query service."""

from __future__ import annotations

import re
from collections.abc import Iterable
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Final, Literal, Protocol, cast

from active_knowledge_server.connectors.workspace import WorkspaceConnector
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing.embeddings import LOCAL_EMBEDDING_DIMENSIONS, embed_text_locally
from active_knowledge_server.indexing.workspace_map import WorkspaceMapBuilder
from active_knowledge_server.models.responses import Warning
from active_knowledge_server.query.graph import traverse_entity_graph
from active_knowledge_server.storage import (
	ALL_SCOPE,
	FTSMatch,
	FTSQuery,
	FileRecord,
	LogicalEntity,
	LogicalEvidence,
	LogicalRelation,
	QueryScope,
	StorageAdapter,
	StorageFTSTable,
	StorageReader,
	StorageSourceIndex,
	StorageWarning,
	VectorMatch,
	VectorQuery,
	VectorStoreAdapter,
	VectorStoreReader,
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
VectorSearchObjectType = Literal["chunk", "entity", "evidence"]
GraphNodeType = Literal["entity", "layer", "feature"]

_LOOKUP_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9]+")
_FTS_LOOKUP_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_]+")
_DOC_MENTION_TOP_K: Final = 24
_ENTITY_FTS_TOP_K: Final = 24
_FULLTEXT_FTS_TOP_K: Final = 24
_VECTOR_SEARCH_TOP_K: Final = 24
_FUZZY_RATIO_THRESHOLD: Final = 0.72
_DEFAULT_GRAPH_RELATION_TYPES: Final[tuple[str, ...]] = (
	"contains",
	"defines",
	"calls",
	"guarded_by_macro",
	"belongs_to_module",
	"enabled_by",
	"disabled_by",
	"unknown_by",
	"belongs_to_layer",
	"implements_feature",
)
_SYNTHETIC_GRAPH_RELATION_TYPES: Final[tuple[str, ...]] = (
	"belongs_to_layer",
	"implements_feature",
)
_DEFAULT_FULLTEXT_INDEXES: Final[tuple[StorageFTSTable, ...]] = (
	"chunk_fts",
	"entity_fts",
	"doc_fts",
	"code_fts",
)
_DEFAULT_VECTOR_OBJECT_TYPES: Final[tuple[VectorSearchObjectType, ...]] = ("chunk",)
_DEFAULT_VECTOR_FALLBACK_INDEXES: Final[tuple[StorageFTSTable, ...]] = ("doc_fts", "chunk_fts")
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
_FULLTEXT_REASON_BY_INDEX: Final[dict[StorageFTSTable, str]] = {
	"chunk_fts": "matched chunk title/text/symbols/tags",
	"entity_fts": "matched entity name/qualified_name/aliases/summary",
	"doc_fts": "matched document title/text",
	"code_fts": "matched code symbol_names/comments/code_text",
}
_VECTOR_MATCH_REASON: Final = "matched semantic similarity over indexed vectors"
_VECTOR_WARNING_ACTIONS: Final[dict[str, str]] = {
	"embedding.disabled": "Enable indexing.embeddings.enabled or rely on FTS fallback results.",
	"embedding.provider_unavailable": "Configure indexing.embeddings.provider=local or supply a compatible query embedder.",
	"embedding.version_mismatch": "Rebuild vectors with the configured embedding model or align the query embedding model.",
	"retrieval.vector_fallback": "Review the FTS fallback results and restore vector availability for semantic recall.",
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


@dataclass(frozen=True)
class FullTextSearchRequest:
	"""Stable request for storage-backed full-text retrieval."""

	query: str
	scope: QueryScope = field(default_factory=QueryScope)
	indexes: tuple[StorageFTSTable, ...] = _DEFAULT_FULLTEXT_INDEXES
	top_k: int = 12
	domain: str | None = None
	doc_type: str | None = None
	module: str | None = None
	source_index: StorageSourceIndex | None = None

	def __post_init__(self) -> None:
		if not self.query.strip():
			raise ValueError("query must not be empty")
		if self.top_k < 1:
			raise ValueError("top_k must be >= 1")
		normalized_indexes = tuple(dict.fromkeys(self.indexes))
		if not normalized_indexes:
			raise ValueError("indexes must not be empty")
		invalid_indexes = [index_name for index_name in normalized_indexes if index_name not in _DEFAULT_FULLTEXT_INDEXES]
		if invalid_indexes:
			raise ValueError(f"unsupported FTS indexes: {', '.join(invalid_indexes)}")
		object.__setattr__(self, "indexes", normalized_indexes)

	@property
	def normalized_query(self) -> str:
		return normalize_fts_lookup_text(self.query)

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
			"indexes": list(self.indexes),
			"top_k": self.top_k,
			"domain": self.domain,
			"doc_type": self.doc_type,
			"module": self.module,
			"source_index": self.source_index,
		}


@dataclass(frozen=True)
class FullTextMatchResult:
	"""One full-text candidate prepared for query-service fusion."""

	logical_object_id: str
	physical_object_id: str
	object_type: Literal["chunk", "entity"]
	primary_index: StorageFTSTable
	matched_indexes: tuple[StorageFTSTable, ...]
	source_index: StorageSourceIndex
	score: float
	match_reason: str
	relative_path: str | None
	title: str | None
	snippet: str | None
	file_id: str | None
	chunk_id: str | None
	entity_id: str | None
	profile_id: str
	source_scope: str
	domain: str | None
	doc_type: str | None
	module_names: tuple[str, ...] = ()
	metadata: dict[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		return {
			"logical_object_id": self.logical_object_id,
			"physical_object_id": self.physical_object_id,
			"object_type": self.object_type,
			"primary_index": self.primary_index,
			"matched_indexes": list(self.matched_indexes),
			"source_index": self.source_index,
			"score": self.score,
			"match_reason": self.match_reason,
			"relative_path": self.relative_path,
			"title": self.title,
			"snippet": self.snippet,
			"file_id": self.file_id,
			"chunk_id": self.chunk_id,
			"entity_id": self.entity_id,
			"profile_id": self.profile_id,
			"source_scope": self.source_scope,
			"domain": self.domain,
			"doc_type": self.doc_type,
			"module_names": list(self.module_names),
			"metadata": dict(self.metadata),
		}


@dataclass(frozen=True)
class FullTextSearchResult:
	"""Stable output of storage-backed full-text retrieval."""

	request: FullTextSearchRequest
	matches: tuple[FullTextMatchResult, ...]

	@property
	def total_matches(self) -> int:
		return len(self.matches)

	def to_dict(self) -> dict[str, object]:
		return {
			"request": self.request.to_dict(),
			"total_matches": self.total_matches,
			"matches": [item.to_dict() for item in self.matches],
		}


@dataclass
class _FullTextAggregate:
	best_match: FTSMatch
	total_score: float
	matched_indexes: list[StorageFTSTable] = field(default_factory=list)
	reasons: list[str] = field(default_factory=list)
	module_names: list[str] = field(default_factory=list)


class QueryEmbedder(Protocol):
	"""Small query-time embedding boundary used by semantic retrievers."""

	provider: str
	model_version: str

	def embed_query(self, text: str) -> tuple[float, ...]:
		"""Return one embedding compatible with indexed vector payloads."""


@dataclass(frozen=True)
class LocalQueryEmbedder:
	"""Deterministic offline embedder compatible with the document indexer."""

	model_version: str = "bge-m3"
	provider: str = "local"
	dimensions: int = LOCAL_EMBEDDING_DIMENSIONS

	def embed_query(self, text: str) -> tuple[float, ...]:
		return embed_text_locally(text, dimensions=self.dimensions)


@dataclass(frozen=True)
class VectorSearchRequest:
	"""Stable request for semantic vector retrieval with FTS fallback."""

	query: str
	scope: QueryScope = field(default_factory=QueryScope)
	top_k: int = 8
	object_types: tuple[VectorSearchObjectType, ...] = _DEFAULT_VECTOR_OBJECT_TYPES
	source_index: StorageSourceIndex | None = None
	embedding_model_version: str | None = None
	domain: str | None = None
	doc_type: str | None = None
	fallback_indexes: tuple[StorageFTSTable, ...] = _DEFAULT_VECTOR_FALLBACK_INDEXES
	use_fallback_on_degraded: bool = True

	def __post_init__(self) -> None:
		if not self.query.strip():
			raise ValueError("query must not be empty")
		if self.top_k < 1:
			raise ValueError("top_k must be >= 1")
		normalized_object_types = tuple(dict.fromkeys(self.object_types))
		if not normalized_object_types:
			raise ValueError("object_types must not be empty")
		invalid_object_types = [
			object_type for object_type in normalized_object_types if object_type not in _DEFAULT_VECTOR_OBJECT_TYPES + ("entity", "evidence")
		]
		if invalid_object_types:
			raise ValueError(f"unsupported vector object_types: {', '.join(invalid_object_types)}")
		object.__setattr__(self, "object_types", normalized_object_types)
		normalized_indexes = tuple(dict.fromkeys(self.fallback_indexes))
		invalid_indexes = [index_name for index_name in normalized_indexes if index_name not in _DEFAULT_FULLTEXT_INDEXES]
		if invalid_indexes:
			raise ValueError(f"unsupported fallback FTS indexes: {', '.join(invalid_indexes)}")
		object.__setattr__(self, "fallback_indexes", normalized_indexes)

	def to_dict(self) -> dict[str, object]:
		return {
			"query": self.query,
			"scope": {
				"snapshot_id": self.scope.snapshot_id,
				"profile_id": self.scope.profile_id,
				"source_scope": self.scope.source_scope,
				"path_scope": self.scope.path_scope,
				"include_inactive": self.scope.include_inactive,
			},
			"top_k": self.top_k,
			"object_types": list(self.object_types),
			"source_index": self.source_index,
			"embedding_model_version": self.embedding_model_version,
			"domain": self.domain,
			"doc_type": self.doc_type,
			"fallback_indexes": list(self.fallback_indexes),
			"use_fallback_on_degraded": self.use_fallback_on_degraded,
		}


@dataclass(frozen=True)
class VectorMatchResult:
	"""One semantic vector candidate prepared for fusion or fallback handling."""

	logical_object_id: str
	physical_object_id: str
	vector_ref_id: str
	object_type: VectorSearchObjectType
	source_index: StorageSourceIndex
	score: float
	match_reason: str
	file_id: str | None
	relative_path: str | None
	title: str | None
	snippet: str | None
	chunk_id: str | None
	entity_id: str | None
	evidence_id: str | None
	profile_id: str
	source_scope: str
	domain: str | None
	doc_type: str | None
	embedding_model_version: str
	content_hash: str
	metadata: dict[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		return {
			"logical_object_id": self.logical_object_id,
			"physical_object_id": self.physical_object_id,
			"vector_ref_id": self.vector_ref_id,
			"object_type": self.object_type,
			"source_index": self.source_index,
			"score": self.score,
			"match_reason": self.match_reason,
			"file_id": self.file_id,
			"relative_path": self.relative_path,
			"title": self.title,
			"snippet": self.snippet,
			"chunk_id": self.chunk_id,
			"entity_id": self.entity_id,
			"evidence_id": self.evidence_id,
			"profile_id": self.profile_id,
			"source_scope": self.source_scope,
			"domain": self.domain,
			"doc_type": self.doc_type,
			"embedding_model_version": self.embedding_model_version,
			"content_hash": self.content_hash,
			"metadata": dict(self.metadata),
		}


@dataclass(frozen=True)
class VectorSearchResult:
	"""Stable output of semantic retrieval plus degraded fallback context."""

	request: VectorSearchRequest
	matches: tuple[VectorMatchResult, ...] = ()
	fallback_matches: tuple[FullTextMatchResult, ...] = ()
	warnings: tuple[Warning, ...] = ()
	retrieval_mode: Literal["vector", "fts_fallback"] = "vector"

	@property
	def total_matches(self) -> int:
		return len(self.fallback_matches) if self.retrieval_mode == "fts_fallback" else len(self.matches)

	def to_dict(self) -> dict[str, object]:
		return {
			"request": self.request.to_dict(),
			"retrieval_mode": self.retrieval_mode,
			"total_matches": self.total_matches,
			"matches": [item.to_dict() for item in self.matches],
			"fallback_matches": [item.to_dict() for item in self.fallback_matches],
			"warnings": [item.to_dict() for item in self.warnings],
		}


@dataclass(frozen=True)
class GraphSearchRequest:
	"""Stable request for bounded graph expansion over live logical relations."""

	seed_entity_ids: tuple[str, ...]
	scope: QueryScope = field(default_factory=QueryScope)
	relation_types: tuple[str, ...] = _DEFAULT_GRAPH_RELATION_TYPES
	max_depth: int = 1

	def __post_init__(self) -> None:
		if not any(entity_id.strip() for entity_id in self.seed_entity_ids):
			raise ValueError("seed_entity_ids must not be empty")
		if self.max_depth < 0:
			raise ValueError("max_depth must be >= 0")

	def to_dict(self) -> dict[str, object]:
		return {
			"seed_entity_ids": list(self.seed_entity_ids),
			"scope": {
				"snapshot_id": self.scope.snapshot_id,
				"profile_id": self.scope.profile_id,
				"source_scope": self.scope.source_scope,
				"path_scope": self.scope.path_scope,
				"include_inactive": self.scope.include_inactive,
			},
			"relation_types": list(self.relation_types),
			"max_depth": self.max_depth,
		}


@dataclass(frozen=True)
class GraphNodeResult:
	"""One graph node resolved from an entity or workspace projection view."""

	node_id: str
	node_type: GraphNodeType
	name: str
	depth: int
	relative_path: str | None = None
	entity_type: str | None = None
	profile_id: str = ALL_SCOPE
	module_names: tuple[str, ...] = ()
	metadata: dict[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		return {
			"node_id": self.node_id,
			"node_type": self.node_type,
			"name": self.name,
			"depth": self.depth,
			"relative_path": self.relative_path,
			"entity_type": self.entity_type,
			"profile_id": self.profile_id,
			"module_names": list(self.module_names),
			"metadata": dict(self.metadata),
		}


@dataclass(frozen=True)
class GraphRelationResult:
	"""One returned edge in a local relation graph."""

	relation_id: str
	relation_type: str
	src_node_id: str
	dst_node_id: str
	depth: int
	source_index: str
	profile_id: str = ALL_SCOPE
	synthetic: bool = False
	metadata: dict[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		return {
			"relation_id": self.relation_id,
			"relation_type": self.relation_type,
			"src_node_id": self.src_node_id,
			"dst_node_id": self.dst_node_id,
			"depth": self.depth,
			"source_index": self.source_index,
			"profile_id": self.profile_id,
			"synthetic": self.synthetic,
			"metadata": dict(self.metadata),
		}


@dataclass(frozen=True)
class GraphSearchResult:
	"""Bounded graph expansion response for query-time retrieval."""

	request: GraphSearchRequest
	nodes: tuple[GraphNodeResult, ...]
	relations: tuple[GraphRelationResult, ...]
	total_nodes: int
	total_relations: int
	skipped_relation_ids: tuple[str, ...] = ()
	warnings: tuple[Warning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		return {
			"request": self.request.to_dict(),
			"total_nodes": self.total_nodes,
			"total_relations": self.total_relations,
			"skipped_relation_ids": list(self.skipped_relation_ids),
			"warnings": [item.to_dict() for item in self.warnings],
			"nodes": [item.to_dict() for item in self.nodes],
			"relations": [item.to_dict() for item in self.relations],
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


class FullTextRetriever:
	"""Query chunk/entity/doc/code FTS indexes through the stable storage contract."""

	def __init__(self, reader: StorageReader) -> None:
		self._reader = reader

	@classmethod
	def from_storage(cls, adapter: StorageAdapter) -> FullTextRetriever:
		return cls(adapter.reader())

	def search(self, request: FullTextSearchRequest) -> FullTextSearchResult:
		logical_entities = tuple(self._reader.logical_entities(request.scope))
		module_names_by_file_id = self._module_names_by_file_id(request.scope, logical_entities)
		aggregates: dict[tuple[str, str], _FullTextAggregate] = {}
		query_text = request.normalized_query or request.query.strip()

		for index_name in request.indexes:
			fts_matches = self._reader.search_fts(
				FTSQuery(
					index_name=index_name,
					query=query_text,
					scope=request.scope,
					top_k=max(request.top_k * 4, _FULLTEXT_FTS_TOP_K),
					domain=request.domain,
					doc_type=request.doc_type,
					source_index=request.source_index,
				)
			)
			for match in fts_matches:
				module_names = self._module_names_for_match(match, module_names_by_file_id)
				if request.module and not module_filter_matches(request.module, module_names):
					continue
				aggregate_key = (match.object_type, match.logical_object_id)
				reason = build_fulltext_match_reason(index_name, request)
				aggregate = aggregates.get(aggregate_key)
				if aggregate is None:
					aggregates[aggregate_key] = _FullTextAggregate(
						best_match=match,
						total_score=match.score,
						matched_indexes=[index_name],
						reasons=[reason],
						module_names=list(module_names),
					)
					continue
				aggregate.best_match = prefer_fulltext_match(aggregate.best_match, match)
				aggregate.total_score += match.score
				aggregate.matched_indexes.append(index_name)
				aggregate.reasons.append(reason)
				aggregate.module_names.extend(module_names)

		matches = tuple(
			sorted(
				(self._to_fulltext_match(item) for item in aggregates.values()),
				key=lambda item: (-item.score, item.primary_index, item.logical_object_id),
			)[: request.top_k]
		)
		return FullTextSearchResult(request=request, matches=matches)

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
		module_names_by_entity_id = {
			item.logical_object_id: item.record.qualified_name or item.record.name
			for item in logical_entities
			if item.record.entity_type == "Module"
		}
		module_names_by_file_id: dict[str, list[str]] = defaultdict(list)
		for relation in self._reader.logical_relations(scope):
			if relation.record.relation_type != "belongs_to_module":
				continue
			file_id = file_entity_ids.get(relation.record.src_entity_id)
			module_name = module_names_by_entity_id.get(relation.record.dst_entity_id)
			if file_id is None or module_name is None:
				continue
			module_names_by_file_id[file_id].append(module_name)
		return {
			file_id: tuple(dict.fromkeys(name for name in names if name))
			for file_id, names in module_names_by_file_id.items()
		}

	def _module_names_for_match(
		self,
		match: FTSMatch,
		module_names_by_file_id: dict[str, tuple[str, ...]],
	) -> tuple[str, ...]:
		module_names: list[str] = []
		if match.file_id is not None:
			module_names.extend(module_names_by_file_id.get(match.file_id, ()))
		if match.object_type == "entity" and match.entity_id is not None:
			record = self._reader.get_entity(match.entity_id)
			if record is not None:
				module_names.extend(metadata_text_list(record.metadata, "module"))
				module_names.extend(metadata_text_list(record.metadata, "modules"))
				if record.entity_type == "Module":
					module_names.extend(
						name
						for name in (
							record.name,
							record.qualified_name,
							*metadata_text_list(record.metadata, "aliases"),
						)
						if name
					)
		if match.object_type == "chunk" and match.chunk_id is not None:
			record = self._reader.get_chunk(match.chunk_id)
			if record is not None:
				module_names.extend(metadata_text_list(record.metadata, "module"))
				module_names.extend(metadata_text_list(record.metadata, "modules"))
		return tuple(dict.fromkeys(name.strip() for name in module_names if name.strip()))

	def _to_fulltext_match(self, aggregate: _FullTextAggregate) -> FullTextMatchResult:
		best_match = aggregate.best_match
		return FullTextMatchResult(
			logical_object_id=best_match.logical_object_id,
			physical_object_id=best_match.physical_object_id,
			object_type=best_match.object_type,
			primary_index=best_match.index_name,
			matched_indexes=tuple(dict.fromkeys(aggregate.matched_indexes)),
			source_index=best_match.source_index,
			score=round(aggregate.total_score, 6),
			match_reason="; ".join(dict.fromkeys(reason for reason in aggregate.reasons if reason)),
			relative_path=best_match.relative_path,
			title=best_match.title,
			snippet=best_match.snippet,
			file_id=best_match.file_id,
			chunk_id=best_match.chunk_id,
			entity_id=best_match.entity_id,
			profile_id=best_match.profile_id,
			source_scope=best_match.source_scope,
			domain=best_match.domain,
			doc_type=best_match.doc_type,
			module_names=tuple(dict.fromkeys(aggregate.module_names)),
			metadata=dict(best_match.metadata),
		)


class VectorRetriever:
	"""Run semantic vector recall with deterministic local embeddings and FTS fallback."""

	def __init__(
		self,
		reader: StorageReader,
		vector_reader: VectorStoreReader,
		*,
		query_embedder: QueryEmbedder | None = None,
		fallback_retriever: FullTextRetriever | None = None,
		embeddings_enabled: bool = True,
		configured_provider: str = "local",
		default_embedding_model_version: str | None = None,
	) -> None:
		self._reader = reader
		self._vector_reader = vector_reader
		self._query_embedder = query_embedder
		self._fallback_retriever = fallback_retriever
		self._embeddings_enabled = embeddings_enabled
		self._configured_provider = configured_provider.strip().lower() or "local"
		self._default_embedding_model_version = default_embedding_model_version

	@classmethod
	def from_adapters(
		cls,
		metadata_adapter: StorageAdapter,
		vector_adapter: VectorStoreAdapter,
		*,
		query_embedder: QueryEmbedder | None = None,
		fallback_retriever: FullTextRetriever | None = None,
		embeddings_enabled: bool = True,
		configured_provider: str = "local",
		default_embedding_model_version: str | None = None,
	) -> VectorRetriever:
		reader = metadata_adapter.reader()
		return cls(
			reader,
			vector_adapter.reader(),
			query_embedder=query_embedder,
			fallback_retriever=fallback_retriever or FullTextRetriever(reader),
			embeddings_enabled=embeddings_enabled,
			configured_provider=configured_provider,
			default_embedding_model_version=default_embedding_model_version,
		)

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		metadata_adapter: StorageAdapter,
		vector_adapter: VectorStoreAdapter,
		query_embedder: QueryEmbedder | None = None,
		fallback_retriever: FullTextRetriever | None = None,
	) -> VectorRetriever:
		provider = config.indexing.embeddings.provider.strip().lower()
		embeddings_enabled = config.indexing.embeddings.enabled
		model_version = config.indexing.embeddings.model
		resolved_embedder = query_embedder
		if resolved_embedder is None and embeddings_enabled and provider == "local":
			resolved_embedder = LocalQueryEmbedder(model_version=model_version)
		return cls.from_adapters(
			metadata_adapter,
			vector_adapter,
			query_embedder=resolved_embedder,
			fallback_retriever=fallback_retriever,
			embeddings_enabled=embeddings_enabled,
			configured_provider=provider,
			default_embedding_model_version=model_version,
		)

	def search(self, request: VectorSearchRequest) -> VectorSearchResult:
		if self._query_embedder is None:
			return self._fallback_result(request, warnings=(self._embedding_unavailable_warning(),))

		try:
			query_embedding = self._query_embedder.embed_query(request.query)
		except Exception as exc:
			warning = build_vector_warning(
				code="embedding.provider_unavailable",
				message="Query embedding provider failed before vector retrieval could run.",
				details={
					"provider": self._configured_provider,
					"error_kind": exc.__class__.__name__,
				},
			)
			return self._fallback_result(request, warnings=(warning,))

		vector_result = self._vector_reader.search(
			VectorQuery(
				embedding=query_embedding,
				scope=request.scope,
				top_k=max(request.top_k * 4, _VECTOR_SEARCH_TOP_K),
				object_types=request.object_types,
				embedding_model_version=(
					request.embedding_model_version
					or self._default_embedding_model_version
					or self._query_embedder.model_version
				),
				source_index=request.source_index,
			)
		)
		warnings = dedupe_query_warnings(
			vector_warning_from_storage_warning(item) for item in vector_result.warnings
		)
		file_records = {record.file_id: record for record in self._reader.iter_files(request.scope)}
		matches = tuple(
			sorted(
				(
					resolved
					for match in vector_result.matches
					for resolved in (self._to_vector_match_result(match, file_records=file_records),)
					if resolved is not None and self._matches_request_filters(resolved, request)
				),
				key=lambda item: (-item.score, -source_priority(item.source_index), item.logical_object_id),
			)[: request.top_k]
		)
		if matches or not request.use_fallback_on_degraded or not should_fallback_to_fts(warnings):
			return VectorSearchResult(
				request=request,
				matches=matches,
				warnings=warnings,
				retrieval_mode="vector",
			)
		return self._fallback_result(request, warnings=warnings)

	def _embedding_unavailable_warning(self) -> Warning:
		if not self._embeddings_enabled:
			return build_vector_warning(
				code="embedding.disabled",
				message="Embeddings are disabled, so semantic vector retrieval fell back to FTS.",
				details={"provider": self._configured_provider},
			)
		return build_vector_warning(
			code="embedding.provider_unavailable",
			message="Configured query embedding provider is unavailable, so semantic retrieval fell back to FTS.",
			details={
				"provider": self._configured_provider,
				"supported_providers": ["local"],
			},
		)

	def _fallback_result(
		self,
		request: VectorSearchRequest,
		*,
		warnings: tuple[Warning, ...],
	) -> VectorSearchResult:
		if not request.use_fallback_on_degraded or self._fallback_retriever is None:
			return VectorSearchResult(
				request=request,
				warnings=dedupe_query_warnings(warnings),
				retrieval_mode="fts_fallback",
			)
		fallback = self._fallback_retriever.search(
			FullTextSearchRequest(
				query=request.query,
				scope=request.scope,
				indexes=request.fallback_indexes,
				top_k=request.top_k,
				domain=request.domain,
				doc_type=request.doc_type,
				source_index=request.source_index,
			)
		)
		all_warnings = dedupe_query_warnings(
			[
				*warnings,
				build_vector_warning(
					code="retrieval.vector_fallback",
					message="Semantic vectors were unavailable, so the retriever returned FTS fallback matches.",
					details={
						"fallback_indexes": list(request.fallback_indexes),
						"provider": self._configured_provider,
					},
				),
			]
		)
		return VectorSearchResult(
			request=request,
			fallback_matches=fallback.matches,
			warnings=all_warnings,
			retrieval_mode="fts_fallback",
		)

	def _to_vector_match_result(
		self,
		match: VectorMatch,
		*,
		file_records: dict[str, FileRecord],
	) -> VectorMatchResult | None:
		if match.object_type == "chunk":
			record = self._reader.get_chunk(match.physical_object_id)
			if record is None:
				return None
			file_record = file_records.get(record.file_id)
			metadata = dict(match.metadata)
			return VectorMatchResult(
				logical_object_id=match.logical_object_id,
				physical_object_id=match.physical_object_id,
				vector_ref_id=match.vector_ref_id,
				object_type="chunk",
				source_index=match.source_index,
				score=round(match.score, 6),
				match_reason=build_vector_match_reason(match),
				file_id=record.file_id,
				relative_path=None if file_record is None else file_record.relative_path,
				title=metadata_text(metadata, "title") or metadata_text(record.metadata, "title") or record.chunk_type,
				snippet=summarize_text(record.text),
				chunk_id=record.chunk_id,
				entity_id=None,
				evidence_id=None,
				profile_id=record.profile_id,
				source_scope=record.source_scope,
				domain=metadata_text(record.metadata, "domain") or metadata_text(metadata, "domain"),
				doc_type=metadata_text(record.metadata, "doc_type") or metadata_text(metadata, "doc_type"),
				embedding_model_version=match.embedding_model_version,
				content_hash=match.content_hash,
				metadata=metadata,
			)
		if match.object_type == "entity":
			record = self._reader.get_entity(match.physical_object_id)
			if record is None:
				return None
			file_record = file_records.get(record.file_id)
			metadata = dict(match.metadata)
			return VectorMatchResult(
				logical_object_id=match.logical_object_id,
				physical_object_id=match.physical_object_id,
				vector_ref_id=match.vector_ref_id,
				object_type="entity",
				source_index=match.source_index,
				score=round(match.score, 6),
				match_reason=build_vector_match_reason(match),
				file_id=record.file_id,
				relative_path=None if file_record is None else file_record.relative_path,
				title=record.name,
				snippet=metadata_text(record.metadata, "summary") or metadata_text(metadata, "summary"),
				chunk_id=None,
				entity_id=record.entity_id,
				evidence_id=None,
				profile_id=record.profile_id,
				source_scope=record.source_scope,
				domain=metadata_text(record.metadata, "domain") or metadata_text(metadata, "domain"),
				doc_type=metadata_text(record.metadata, "doc_type") or metadata_text(metadata, "doc_type"),
				embedding_model_version=match.embedding_model_version,
				content_hash=match.content_hash,
				metadata=metadata,
			)
		record = self._reader.get_evidence(match.physical_object_id)
		if record is None:
			return None
		file_record = file_records.get(record.file_id)
		metadata = dict(match.metadata)
		return VectorMatchResult(
			logical_object_id=match.logical_object_id,
			physical_object_id=match.physical_object_id,
			vector_ref_id=match.vector_ref_id,
			object_type="evidence",
			source_index=match.source_index,
			score=round(match.score, 6),
			match_reason=build_vector_match_reason(match),
			file_id=record.file_id,
			relative_path=None if file_record is None else file_record.relative_path,
			title=record.citation_label,
			snippet=record.excerpt,
			chunk_id=record.chunk_id,
			entity_id=None,
			evidence_id=record.evidence_id,
			profile_id=record.profile_id,
			source_scope=record.source_scope,
			domain=metadata_text(record.metadata, "domain") or metadata_text(metadata, "domain"),
			doc_type=metadata_text(record.metadata, "doc_type") or metadata_text(metadata, "doc_type"),
			embedding_model_version=match.embedding_model_version,
			content_hash=match.content_hash,
			metadata=metadata,
		)

	@staticmethod
	def _matches_request_filters(
		match: VectorMatchResult,
		request: VectorSearchRequest,
	) -> bool:
		if request.domain and match.domain != request.domain:
			return False
		if request.doc_type and match.doc_type != request.doc_type:
			return False
		return True


class GraphRetriever:
	"""Expand a local relation graph from seed entities over live logical views."""

	def __init__(
		self,
		reader: StorageReader,
		*,
		workspace_connector: WorkspaceConnector | None = None,
		workspace_map_builder: WorkspaceMapBuilder | None = None,
	) -> None:
		self._reader = reader
		self._workspace_connector = workspace_connector
		self._workspace_map_builder = workspace_map_builder

	@classmethod
	def from_storage(cls, adapter: StorageAdapter) -> GraphRetriever:
		return cls(adapter.reader())

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		metadata_adapter: StorageAdapter,
		workspace_connector: WorkspaceConnector | None = None,
		workspace_map_builder: WorkspaceMapBuilder | None = None,
	) -> GraphRetriever:
		return cls(
			metadata_adapter.reader(),
			workspace_connector=workspace_connector or WorkspaceConnector.from_config(config),
			workspace_map_builder=workspace_map_builder or WorkspaceMapBuilder.from_config(config),
		)

	def search(self, request: GraphSearchRequest) -> GraphSearchResult:
		seed_entity_ids = tuple(
			dict.fromkeys(
				entity_id.strip()
				for entity_id in request.seed_entity_ids
				if entity_id and entity_id.strip()
			)
		)
		relation_types = tuple(
			dict.fromkeys(
				relation_type.strip()
				for relation_type in request.relation_types
				if relation_type and relation_type.strip()
			)
		)
		live_relation_types = tuple(
			relation_type
			for relation_type in relation_types
			if relation_type not in _SYNTHETIC_GRAPH_RELATION_TYPES
		)
		traversal = traverse_entity_graph(
			self._reader,
			seed_entity_ids,
			scope=request.scope,
			max_depth=request.max_depth,
			relation_types=live_relation_types,
		)
		logical_entities = {
			item.logical_object_id: item
			for item in self._reader.logical_entities(request.scope)
			if item.logical_object_id in traversal.entity_ids
		}
		physical_to_logical_entity_ids = {
			item.physical_object_id: item.logical_object_id for item in logical_entities.values()
		}
		file_records = {record.file_id: record for record in self._reader.iter_files(request.scope)}
		nodes_by_id = {
			logical_entity_id: self._to_graph_entity_node(
				logical_entity,
				depth=traversal.depth_by_entity_id.get(logical_entity_id, 0),
				file_records=file_records,
			)
			for logical_entity_id, logical_entity in logical_entities.items()
		}
		relations = [
			self._to_graph_relation(relation, depth_by_entity_id=traversal.depth_by_entity_id)
			for relation in traversal.relations
			if relation.record.src_entity_id in nodes_by_id and relation.record.dst_entity_id in nodes_by_id
		]
		warnings: list[Warning] = []
		if any(relation_type in _SYNTHETIC_GRAPH_RELATION_TYPES for relation_type in relation_types):
			synthetic_nodes, synthetic_relations, synthetic_warnings = self._build_workspace_projection_context(
				relation_types=relation_types,
				scope=request.scope,
				depth_by_entity_id=traversal.depth_by_entity_id,
				physical_to_logical_entity_ids=physical_to_logical_entity_ids,
				max_depth=request.max_depth,
			)
			for node in synthetic_nodes:
				nodes_by_id[node.node_id] = node
			relations.extend(synthetic_relations)
			warnings.extend(synthetic_warnings)
		sorted_nodes = tuple(
			sorted(nodes_by_id.values(), key=lambda item: (item.depth, item.node_type, item.node_id))
		)
		sorted_relations = tuple(
			sorted(
				relations,
				key=lambda item: (item.depth, item.relation_type, item.src_node_id, item.dst_node_id, item.relation_id),
			)
		)
		return GraphSearchResult(
			request=request,
			nodes=sorted_nodes,
			relations=sorted_relations,
			total_nodes=len(sorted_nodes),
			total_relations=len(sorted_relations),
			skipped_relation_ids=traversal.skipped_relation_ids,
			warnings=dedupe_query_warnings(warnings),
		)

	def _to_graph_entity_node(
		self,
		logical_entity: LogicalEntity,
		*,
		depth: int,
		file_records: dict[str, FileRecord],
	) -> GraphNodeResult:
		record = logical_entity.record
		file_record = file_records.get(record.file_id)
		module_names = list(metadata_text_list(record.metadata, "module"))
		module_names.extend(metadata_text_list(record.metadata, "modules"))
		if record.entity_type == "Module":
			module_names.extend(name for name in (record.qualified_name, record.name) if name)
		return GraphNodeResult(
			node_id=logical_entity.logical_object_id,
			node_type="entity",
			name=record.name,
			depth=depth,
			relative_path=None if file_record is None else file_record.relative_path,
			entity_type=record.entity_type,
			profile_id=record.profile_id,
			module_names=tuple(dict.fromkeys(name.strip() for name in module_names if name.strip())),
			metadata={
				"qualified_name": record.qualified_name,
				"path": record.path,
				"physical_entity_id": logical_entity.physical_object_id,
				"source_index": logical_entity.source_index,
				"source_scope": record.source_scope,
				"file_id": record.file_id,
				"start_line": record.start_line,
				"end_line": record.end_line,
				**dict(record.metadata),
			},
		)

	def _to_graph_relation(
		self,
		relation: LogicalRelation,
		*,
		depth_by_entity_id: dict[str, int],
	) -> GraphRelationResult:
		return GraphRelationResult(
			relation_id=relation.logical_object_id,
			relation_type=relation.record.relation_type,
			src_node_id=relation.record.src_entity_id,
			dst_node_id=relation.record.dst_entity_id,
			depth=max(
				depth_by_entity_id.get(relation.record.src_entity_id, 0),
				depth_by_entity_id.get(relation.record.dst_entity_id, 0),
			),
			source_index=relation.source_index,
			profile_id=relation.record.profile_id,
			metadata=dict(relation.record.metadata),
		)

	def _build_workspace_projection_context(
		self,
		*,
		relation_types: tuple[str, ...],
		scope: QueryScope,
		depth_by_entity_id: dict[str, int],
		physical_to_logical_entity_ids: dict[str, str],
		max_depth: int,
	) -> tuple[tuple[GraphNodeResult, ...], list[GraphRelationResult], tuple[Warning, ...]]:
		if self._workspace_connector is None or self._workspace_map_builder is None:
			return (), [], (
				build_graph_warning(
					code="graph.workspace_context_unavailable",
					message="Workspace context expansion is unavailable, so only live entity relations were returned.",
					details={"requested_relation_types": list(relation_types)},
				),
			)
		try:
			workspace_inventory = self._workspace_connector.scan()
			artifact = self._workspace_map_builder.collect(
				snapshot_id=scope.snapshot_id,
				workspace_inventory=workspace_inventory,
				reader=self._reader,
			)
		except Exception as exc:
			return (), [], (
				build_graph_warning(
					code="graph.workspace_context_unavailable",
					message="Workspace context expansion failed, so only live entity relations were returned.",
					details={
						"requested_relation_types": list(relation_types),
						"error_kind": exc.__class__.__name__,
					},
				),
			)

		nodes_by_id: dict[str, GraphNodeResult] = {}
		relations: list[GraphRelationResult] = []
		layer_view = artifact.views.get("layer")
		if layer_view is not None and "belongs_to_layer" in relation_types:
			self._append_workspace_projection_edges(
				nodes_by_id=nodes_by_id,
				relations=relations,
				relation_type="belongs_to_layer",
				node_type="layer",
				items=layer_view.items,
				depth_by_entity_id=depth_by_entity_id,
				physical_to_logical_entity_ids=physical_to_logical_entity_ids,
				max_depth=max_depth,
			)
		feature_view = artifact.views.get("feature")
		if feature_view is not None and "implements_feature" in relation_types:
			self._append_workspace_projection_edges(
				nodes_by_id=nodes_by_id,
				relations=relations,
				relation_type="implements_feature",
				node_type="feature",
				items=feature_view.items,
				depth_by_entity_id=depth_by_entity_id,
				physical_to_logical_entity_ids=physical_to_logical_entity_ids,
				max_depth=max_depth,
			)
		return tuple(nodes_by_id.values()), relations, ()

	def _append_workspace_projection_edges(
		self,
		*,
		nodes_by_id: dict[str, GraphNodeResult],
		relations: list[GraphRelationResult],
		relation_type: str,
		node_type: Literal["layer", "feature"],
		items: tuple[object, ...],
		depth_by_entity_id: dict[str, int],
		physical_to_logical_entity_ids: dict[str, str],
		max_depth: int,
	) -> None:
		for item in items:
			entity_ids = tuple(
				sorted(
					{
						logical_entity_id
						for physical_entity_id in getattr(item, "entity_ids")
						for logical_entity_id in [physical_to_logical_entity_ids.get(physical_entity_id)]
						if logical_entity_id is not None
						and logical_entity_id in depth_by_entity_id
						and depth_by_entity_id[logical_entity_id] + 1 <= max_depth
					}
				)
			)
			if not entity_ids:
				continue
			node_id = getattr(item, "item_id")
			nodes_by_id[node_id] = GraphNodeResult(
				node_id=node_id,
				node_type=node_type,
				name=getattr(item, "name"),
				depth=min(depth_by_entity_id[entity_id] for entity_id in entity_ids) + 1,
				relative_path=(getattr(item, "source_paths")[0] if len(getattr(item, "source_paths")) == 1 else None),
				module_names=getattr(item, "module_names"),
				metadata={
					"summary": getattr(item, "summary"),
					"source_paths": list(getattr(item, "source_paths")),
					"related_items": list(getattr(item, "related_items")),
					"member_entity_ids": list(entity_ids),
					**dict(getattr(item, "metadata")),
				},
			)
			for entity_id in entity_ids:
				relations.append(
					GraphRelationResult(
						relation_id=f"synthetic:{relation_type}:{entity_id}:{node_id}",
						relation_type=relation_type,
						src_node_id=entity_id,
						dst_node_id=node_id,
						depth=depth_by_entity_id[entity_id] + 1,
						source_index="derived",
						synthetic=True,
						metadata={
							"view_kind": getattr(item, "kind"),
							"source_paths": list(getattr(item, "source_paths")),
						},
					)
				)


def resolve_entity_filter(
	entity_type: RequestedSymbolEntityType | str | None,
) -> tuple[ResolvedSymbolEntityType, ...]:
	key = "auto" if entity_type in (None, "") else str(entity_type).strip().lower()
	return _ENTITY_TYPE_FILTERS.get(key, _ENTITY_TYPE_FILTERS["auto"])


def normalize_exact_text(value: str) -> str:
	return " ".join(value.strip().lower().split())


def normalize_lookup_text(value: str) -> str:
	return " ".join(_LOOKUP_TOKEN_RE.findall(value.lower()))


def normalize_fts_lookup_text(value: str) -> str:
	tokens = _FTS_LOOKUP_TOKEN_RE.findall(value.strip())
	if tokens:
		return " ".join(token for token in tokens if token)
	return " ".join(segment for segment in value.strip().split() if segment)


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


def metadata_text(metadata: dict[str, object], key: str) -> str | None:
	value = metadata.get(key)
	if isinstance(value, str) and value.strip():
		return value.strip()
	return None


def summarize_text(text: str, *, limit: int = 240) -> str:
	normalized = " ".join(text.split())
	return normalized[:limit] if len(normalized) > limit else normalized


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


def build_fulltext_match_reason(
	index_name: StorageFTSTable,
	request: FullTextSearchRequest,
) -> str:
	parts = [_FULLTEXT_REASON_BY_INDEX[index_name]]
	if request.domain:
		parts.append(f"domain={request.domain}")
	if request.doc_type:
		parts.append(f"doc_type={request.doc_type}")
	if request.module:
		parts.append(f"module={request.module}")
	if request.source_index:
		parts.append(f"source_index={request.source_index}")
	if request.scope.profile_id != ALL_SCOPE:
		parts.append(f"profile={request.scope.profile_id}")
	return "; ".join(parts)


def build_vector_match_reason(match: VectorMatch) -> str:
	parts = [_VECTOR_MATCH_REASON]
	provider = metadata_text(dict(match.metadata), "provider")
	if provider:
		parts.append(f"provider={provider}")
	parts.append(f"source_index={match.source_index}")
	parts.append(f"embedding_model={match.embedding_model_version}")
	return "; ".join(parts)


def build_vector_warning(
	*,
	code: str,
	message: str,
	details: dict[str, object],
) -> Warning:
	return Warning(
		level="degraded",
		code=code,
		message=message,
		details=details,
		actionable=True,
		suggested_action=_VECTOR_WARNING_ACTIONS.get(code, "Review retriever availability and retry."),
	)


def build_graph_warning(
	*,
	code: str,
	message: str,
	details: dict[str, object],
) -> Warning:
	return Warning(
		level="caution",
		code=code,
		message=message,
		details=details,
		actionable=False,
	)


def vector_warning_from_storage_warning(warning: StorageWarning) -> Warning:
	return Warning(
		level=warning.level,
		code=warning.code,
		message=warning.message,
		details=dict(warning.details),
		actionable=True,
		suggested_action=_VECTOR_WARNING_ACTIONS.get(
			warning.code,
			"Review vector store status and retry the query.",
		),
	)


def dedupe_query_warnings(warnings: Iterable[Warning]) -> tuple[Warning, ...]:
	ordered: dict[str, Warning] = {}
	for warning in warnings:
		if warning.code not in ordered:
			ordered[warning.code] = warning
	return tuple(ordered.values())


def should_fallback_to_fts(warnings: tuple[Warning, ...]) -> bool:
	return any(
		warning.code in {"embedding.disabled", "embedding.provider_unavailable", "embedding.version_mismatch"}
		for warning in warnings
	)


def module_filter_matches(module_filter: str, module_names: tuple[str, ...]) -> bool:
	if not module_names:
		return False
	filter_exact = normalize_exact_text(module_filter)
	filter_lookup = normalize_lookup_text(module_filter)
	for module_name in module_names:
		normalized_exact = normalize_exact_text(module_name)
		normalized_lookup = normalize_lookup_text(module_name)
		if filter_exact and filter_exact in normalized_exact:
			return True
		if filter_lookup and filter_lookup in normalized_lookup:
			return True
	return False


def source_priority(source_index: StorageSourceIndex) -> int:
	if source_index == "overlay":
		return 3
	if source_index == "merged":
		return 2
	return 1


def bm25_score(match: FTSMatch) -> float:
	value = match.metadata.get("bm25", 0.0)
	if isinstance(value, (int, float)):
		return float(value)
	return 0.0


def prefer_fulltext_match(current: FTSMatch, candidate: FTSMatch) -> FTSMatch:
	if source_priority(candidate.source_index) > source_priority(current.source_index):
		return candidate
	if source_priority(candidate.source_index) < source_priority(current.source_index):
		return current
	if bm25_score(candidate) < bm25_score(current):
		return candidate
	return current
