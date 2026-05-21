"""Query service boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.models.evidence import EvidenceRef
from active_knowledge_server.models.query import QueryIntent, QueryRequest
from active_knowledge_server.models.responses import QueryResult, SuggestedFilter, Warning
from active_knowledge_server.query.evidence_packager import EvidencePackager
from active_knowledge_server.models.routing import RouterDecision
from active_knowledge_server.query.profile_query import (
	build_profile_matrix_result,
	build_profile_resolution_result,
	execution_scope_profile_id,
	reported_profile_id,
)
from active_knowledge_server.query.rerank import (
	CandidateReranker,
	FusionCandidate,
	build_reranker,
	fuse_ranked_candidates,
)
from active_knowledge_server.query.retrievers import (
	GraphRetriever,
	GraphSearchRequest,
	GraphSearchResult,
	SymbolRetriever,
	SymbolSearchRequest,
	SymbolSearchResult,
	FullTextRetriever,
	FullTextSearchRequest,
	FullTextSearchResult,
	VectorRetriever,
	VectorSearchRequest,
	VectorSearchResult,
	dedupe_query_warnings,
)
from active_knowledge_server.query.router import QueryRouter
from active_knowledge_server.storage import ALL_SCOPE, QueryScope, StorageAdapter, VectorStoreAdapter

_DEFAULT_GRAPH_RELATION_TYPES_BY_INTENT: Final[dict[QueryIntent, tuple[str, ...]]] = {
	"code_exact": ("contains", "defines", "belongs_to_module"),
	"code_concept": ("contains", "defines", "belongs_to_module", "belongs_to_layer"),
	"call_trace": ("calls", "contains", "defines", "guarded_by_macro"),
	"runtime_flow": ("calls", "guarded_by_macro", "belongs_to_layer", "implements_feature"),
	"profile_diff": ("guarded_by_macro", "enabled_by", "disabled_by", "unknown_by"),
	"api_lookup": ("defines", "implements_feature"),
	"widget_lookup": ("implements_feature", "belongs_to_layer"),
	"workspace_nav": ("contains", "belongs_to_layer", "implements_feature"),
	"product_context": ("implements_feature", "belongs_to_layer"),
	"project_context": ("implements_feature",),
	"evidence_lookup": ("contains", "defines", "implements_feature"),
	"unknown": ("contains", "defines", "belongs_to_layer", "implements_feature"),
}
_DOC_TYPE_BY_INTENT: Final[dict[QueryIntent, str]] = {
	"api_lookup": "api",
	"widget_lookup": "widget",
}


class QueryService:
	"""Orchestrate hybrid retrieval, fusion, rerank, and context assembly."""

	def __init__(
		self,
		config: ActiveKnowledgeConfig,
		*,
		router: QueryRouter,
		metadata_adapter: StorageAdapter | None = None,
		symbol_retriever: SymbolRetriever | None = None,
		fulltext_retriever: FullTextRetriever | None = None,
		vector_retriever: VectorRetriever | None = None,
		graph_retriever: GraphRetriever | None = None,
		reranker: CandidateReranker | None = None,
		evidence_packager: EvidencePackager | None = None,
	) -> None:
		self._config = config
		self._router = router
		self._metadata_adapter = metadata_adapter
		self._symbol_retriever = symbol_retriever
		self._fulltext_retriever = fulltext_retriever
		self._vector_retriever = vector_retriever
		self._graph_retriever = graph_retriever
		self._reranker = reranker or build_reranker(config.query.hybrid.rerank)
		self._evidence_packager = evidence_packager or EvidencePackager.from_config(
			config,
			metadata_adapter=metadata_adapter,
		)

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		metadata_adapter: StorageAdapter,
		vector_adapter: VectorStoreAdapter | None = None,
		router: QueryRouter | None = None,
		symbol_retriever: SymbolRetriever | None = None,
		fulltext_retriever: FullTextRetriever | None = None,
		vector_retriever: VectorRetriever | None = None,
		graph_retriever: GraphRetriever | None = None,
		reranker: CandidateReranker | None = None,
		evidence_packager: EvidencePackager | None = None,
	) -> QueryService:
		resolved_router = router or QueryRouter.from_config(config)
		resolved_symbol = symbol_retriever or SymbolRetriever.from_storage(metadata_adapter)
		resolved_fulltext = fulltext_retriever or FullTextRetriever.from_storage(metadata_adapter)
		resolved_vector = vector_retriever
		if resolved_vector is None and vector_adapter is not None:
			resolved_vector = VectorRetriever.from_config(
				config,
				metadata_adapter=metadata_adapter,
				vector_adapter=vector_adapter,
				fallback_retriever=resolved_fulltext,
			)
		resolved_graph = graph_retriever or GraphRetriever.from_config(
			config,
			metadata_adapter=metadata_adapter,
		)
		return cls(
			config,
			router=resolved_router,
			metadata_adapter=metadata_adapter,
			symbol_retriever=resolved_symbol,
			fulltext_retriever=resolved_fulltext,
			vector_retriever=resolved_vector,
			graph_retriever=resolved_graph,
			reranker=reranker,
			evidence_packager=evidence_packager,
		)

	def bundle_evidence_for_query(self, request: QueryRequest) -> tuple[EvidenceRef, ...]:
		"""Return the packaged evidence refs for one query request."""

		return self.search(request).evidence_refs

	def bundle_evidence_for_entity(
		self,
		entity_id: str,
		*,
		snapshot_id: str = "current",
		profile_id: str = ALL_SCOPE,
	) -> tuple[EvidenceRef, ...]:
		"""Return packaged evidence refs for one entity ID."""

		return self._evidence_packager.bundle_for_entity(
			scope=QueryScope(snapshot_id=snapshot_id, profile_id=profile_id),
			entity_id=entity_id,
		)

	def search(self, request: QueryRequest) -> QueryResult:
		decision = self._router.route(request)
		result_profile_id = reported_profile_id(decision, request)
		warnings: list[Warning] = list(decision.warnings)
		resolution_result = build_profile_resolution_result(
			request=request,
			decision=decision,
			warnings=warnings,
		)
		if resolution_result is not None:
			return resolution_result
		matrix_result = build_profile_matrix_result(
			config=self._config,
			metadata_adapter=self._metadata_adapter,
			request=request,
			decision=decision,
			warnings=warnings,
		)
		if matrix_result is not None:
			return matrix_result
		scope = QueryScope(
			snapshot_id=request.snapshot_id or "current",
			profile_id=execution_scope_profile_id(decision, request),
		)
		run_trace: list[dict[str, object]] = []
		ranked_lists: dict[str, tuple[FusionCandidate, ...]] = {}
		graph_result: GraphSearchResult | None = None
		symbol_result: SymbolSearchResult | None = None

		if decision.retriever_weights.get("symbol", 0.0) > 0.0 and self._symbol_retriever is not None:
			symbol_result = self._symbol_retriever.search(
				SymbolSearchRequest(
					query=request.query,
					scope=scope,
					entity_type=_primary_arg_text(decision, "entity_type"),
					top_k=self._config.query.default_top_k,
				)
			)
			ranked_lists["symbol"] = self._symbol_candidates_to_fusion(symbol_result)
			run_trace.append(
				{
					"retriever": "symbol",
					"weight": decision.retriever_weights["symbol"],
					"returned": len(symbol_result.candidates),
				}
			)

		if decision.retriever_weights.get("fts", 0.0) > 0.0 and self._fulltext_retriever is not None:
			fulltext_result = self._fulltext_retriever.search(
				FullTextSearchRequest(
					query=request.query,
					scope=scope,
					top_k=self._config.query.default_top_k,
					domain=None if request.domain == "auto" else request.domain,
					doc_type=_resolved_doc_type(request, decision),
					module=_primary_arg_text(decision, "module"),
				)
			)
			ranked_lists["fts"] = self._fulltext_matches_to_fusion(fulltext_result)
			run_trace.append(
				{
					"retriever": "fts",
					"weight": decision.retriever_weights["fts"],
					"returned": len(fulltext_result.matches),
				}
			)

		if decision.retriever_weights.get("vector", 0.0) > 0.0 and self._vector_retriever is not None:
			vector_result = self._vector_retriever.search(
				VectorSearchRequest(
					query=request.query,
					scope=scope,
					top_k=self._config.query.default_top_k,
					domain=None if request.domain == "auto" else request.domain,
					doc_type=_resolved_doc_type(request, decision),
				)
			)
			ranked_lists["vector"] = self._vector_matches_to_fusion(vector_result)
			warnings.extend(vector_result.warnings)
			run_trace.append(
				{
					"retriever": "vector",
					"weight": decision.retriever_weights["vector"],
					"returned": len(ranked_lists["vector"]),
					"warnings": [item.to_dict() for item in vector_result.warnings],
					"retrieval_mode": vector_result.retrieval_mode,
				}
			)

		if decision.retriever_weights.get("graph", 0.0) > 0.0 and self._graph_retriever is not None:
			seed_entity_ids = _graph_seed_entity_ids(symbol_result, request)
			if seed_entity_ids:
				graph_result = self._graph_retriever.search(
					GraphSearchRequest(
						seed_entity_ids=seed_entity_ids,
						scope=scope,
						relation_types=_DEFAULT_GRAPH_RELATION_TYPES_BY_INTENT[decision.intent],
						max_depth=_graph_max_depth(decision.intent),
					)
				)
				ranked_lists["graph"] = self._graph_nodes_to_fusion(graph_result)
				warnings.extend(graph_result.warnings)
				run_trace.append(
					{
						"retriever": "graph",
						"weight": decision.retriever_weights["graph"],
						"seed_entity_ids": list(seed_entity_ids),
						"returned_nodes": len(graph_result.nodes),
						"returned_relations": len(graph_result.relations),
						"warnings": [item.to_dict() for item in graph_result.warnings],
					}
				)
			else:
				run_trace.append(
					{
						"retriever": "graph",
						"weight": decision.retriever_weights["graph"],
						"skipped_reason": "missing_seed_entities",
					}
				)

		fused = fuse_ranked_candidates(ranked_lists, weights=decision.retriever_weights)
		reranked = self._reranker.rerank(
			fused,
			intent=decision.intent,
			requested_profile_id=scope.profile_id,
		)
		limited = reranked[: self._config.query.default_top_k]
		evidence_refs, evidence_trace = self._build_evidence_refs(limited, scope=scope)
		index_status = _extract_partial_ready_index_status(warnings)
		base_confidence = round(
			min(1.0, (decision.confidence * 0.85) + 0.15),
			6,
		)

		if not limited:
			if index_status is None:
				zero_warning = _build_query_warning(
					code="retrieval.zero_result",
					level="caution",
					message="Hybrid fusion did not produce any ranked results.",
					details={"intent": decision.intent},
					actionable=True,
					suggested_action="Add a module, path, symbol, doc_type, or profile_id and retry.",
				)
				warnings.append(zero_warning)
			diagnostics = {
				"route": decision.to_dict(),
				"retrieval_trace": {
					"route_trace": [item.to_dict() for item in decision.route_trace],
					"retriever_runs": run_trace,
					"fusion_strategy": {
						"name": "weighted_rrf",
						"rerank_mode": self._reranker.mode,
						"weights": dict(decision.retriever_weights),
					},
					"ranked_candidates": [],
					"evidence_trace": [],
				},
			}
			result_status = "zero_result"
			summary = "Hybrid retrieval returned no evidence-bearing candidates."
			if index_status is not None:
				result_status = "partial_ready"
				summary = _build_partial_ready_summary(decision, ())
				diagnostics["index_status"] = index_status
			return QueryResult(
				tool_name=decision.tool_plan.primary_tool,
				result_status=result_status,
				confidence=0.0 if result_status == "zero_result" else base_confidence,
				query_intent=decision.intent,
				snapshot_id=scope.snapshot_id,
				profile_id=result_profile_id,
				summary=summary,
				warnings=dedupe_query_warnings(warnings),
				next_queries=_suggest_next_queries(request, decision),
				suggested_filters=(
					SuggestedFilter(field="view", value=decision.selected_view),
				),
				diagnostics=diagnostics,
			)

		items = tuple(self._candidate_to_item(candidate) for candidate in limited)
		result_status = "ok"
		summary = _build_summary(decision, limited)
		confidence = base_confidence
		if index_status is not None:
			result_status = "partial_ready"
			summary = _build_partial_ready_summary(decision, limited)
		elif _should_return_low_confidence(decision, warnings):
			warnings = _ensure_low_confidence_warning(decision, warnings)
			result_status = "low_confidence"
			summary = _build_low_confidence_summary(decision, limited)
			confidence = min(base_confidence, 0.49)
		diagnostics = {
			"route": decision.to_dict(),
			"retrieval_trace": {
				"route_trace": [item.to_dict() for item in decision.route_trace],
				"retriever_runs": run_trace,
				"fusion_strategy": {
					"name": "weighted_rrf",
					"rerank_mode": self._reranker.mode,
					"weights": dict(decision.retriever_weights),
				},
				"ranked_candidates": [candidate.to_dict() for candidate in limited],
				"evidence_trace": evidence_trace,
			},
		}
		if index_status is not None:
			diagnostics["index_status"] = index_status
		return QueryResult(
			tool_name=decision.tool_plan.primary_tool,
			result_status=result_status,
			confidence=confidence,
			query_intent=decision.intent,
			snapshot_id=scope.snapshot_id,
			profile_id=result_profile_id,
			summary=summary,
			items=items,
			entities=()
			if graph_result is None
			else tuple(node.to_dict() for node in graph_result.nodes[: self._config.query.default_top_k]),
			relations=()
			if graph_result is None
			else tuple(relation.to_dict() for relation in graph_result.relations[: self._config.query.default_top_k]),
			evidence_refs=evidence_refs,
			warnings=dedupe_query_warnings(warnings),
			diagnostics=diagnostics,
		)

	def _symbol_candidates_to_fusion(
		self,
		result: SymbolSearchResult,
	) -> tuple[FusionCandidate, ...]:
		return tuple(
			FusionCandidate(
				candidate_id=item.logical_entity_id,
				object_type="entity",
				title=item.name,
				snippet=str(item.metadata.get("summary") or item.qualified_name),
				relative_path=item.relative_path,
				profile_id=item.profile_id,
				raw_score=item.score,
				source_index=item.source_index,
				source_scope=item.source_scope,
				module_names=item.module_names,
				authority_level="workspace_code",
				evidence_keys=item.evidence_ids,
				match_reasons=(item.match_reason,),
				metadata={
					"path": item.path,
					"qualified_name": item.qualified_name,
					"start_line": item.start_line,
					"end_line": item.end_line,
					"aliases": list(item.aliases),
					**dict(item.metadata),
				},
			)
			for item in result.candidates
		)

	def _fulltext_matches_to_fusion(
		self,
		result: FullTextSearchResult,
	) -> tuple[FusionCandidate, ...]:
		return tuple(
			FusionCandidate(
				candidate_id=item.logical_object_id,
				object_type=item.object_type,
				title=item.title,
				snippet=item.snippet,
				relative_path=item.relative_path,
				profile_id=item.profile_id,
				raw_score=item.score,
				source_index=item.source_index,
				source_scope=item.source_scope,
				module_names=item.module_names,
				authority_level=_authority_from_match(item.relative_path, item.metadata),
				freshness_ts=_metadata_text(item.metadata, "freshness_ts"),
				evidence_keys=tuple(
					value
					for value in (item.chunk_id, item.entity_id, item.file_id)
					if isinstance(value, str) and value
				),
				match_reasons=(item.match_reason,),
				metadata=dict(item.metadata),
			)
			for item in result.matches
		)

	def _vector_matches_to_fusion(
		self,
		result: VectorSearchResult,
	) -> tuple[FusionCandidate, ...]:
		matches = result.matches if result.retrieval_mode == "vector" else result.fallback_matches
		candidates: list[FusionCandidate] = []
		for item in matches:
			metadata = dict(item.metadata)
			candidates.append(
				FusionCandidate(
					candidate_id=item.logical_object_id,
					object_type=item.object_type,
					title=item.title,
					snippet=item.snippet,
					relative_path=item.relative_path,
					profile_id=item.profile_id,
					raw_score=item.score,
					source_index=item.source_index,
					source_scope=item.source_scope,
					module_names=tuple(metadata.get("modules", ())) if isinstance(metadata.get("modules"), tuple) else (),
					authority_level=_authority_from_match(item.relative_path, metadata),
					freshness_ts=_metadata_text(metadata, "freshness_ts"),
					graph_proximity=float(metadata.get("graph_proximity", 0.0) or 0.0),
					evidence_keys=tuple(
						value
						for value in (
							getattr(item, "evidence_id", None),
							item.chunk_id,
							item.entity_id,
							item.file_id,
						)
						if isinstance(value, str) and value
					),
					match_reasons=(item.match_reason,),
					metadata=metadata,
				)
			)
		return tuple(candidates)

	def _graph_nodes_to_fusion(
		self,
		result: GraphSearchResult,
	) -> tuple[FusionCandidate, ...]:
		return tuple(
			FusionCandidate(
				candidate_id=item.node_id,
				object_type=item.node_type,
				title=item.name,
				snippet=_metadata_text(item.metadata, "summary") or _metadata_text(item.metadata, "qualified_name"),
				relative_path=item.relative_path,
				profile_id=item.profile_id,
				raw_score=round(1.0 / float(item.depth + 1), 6),
				source_index="derived",
				module_names=item.module_names,
				authority_level="workspace_map" if item.node_type != "entity" else "workspace_code",
				graph_depth=item.depth,
				graph_proximity=round(1.0 / float(item.depth + 1), 6),
				match_reasons=(f"graph expansion depth={item.depth}",),
				metadata=dict(item.metadata),
			)
			for item in result.nodes
		)

	def _candidate_to_item(self, candidate: FusionCandidate) -> dict[str, object]:
		return {
			"candidate_id": candidate.candidate_id,
			"object_type": candidate.object_type,
			"title": candidate.title,
			"snippet": candidate.snippet,
			"relative_path": candidate.relative_path,
			"profile_id": candidate.profile_id,
			"module_names": list(candidate.module_names),
			"score": round(candidate.rerank_score, 6),
			"fused_score": round(candidate.fused_score, 6),
			"authority_level": candidate.authority_level,
			"graph_proximity": round(candidate.graph_proximity, 6),
			"retrieval_sources": sorted(
				{
					signal.retriever
					for signal in candidate.retrieval_signals
				}
			),
			"match_reasons": list(candidate.match_reasons),
			"metadata": dict(candidate.metadata),
		}

	def _build_evidence_refs(
		self,
		candidates: Sequence[FusionCandidate],
		*,
		scope: QueryScope,
	) -> tuple[tuple[EvidenceRef, ...], list[dict[str, object]]]:
		return self._evidence_packager.bundle_for_query(scope=scope, candidates=candidates)


def _resolved_profile_id(decision: RouterDecision, request: QueryRequest) -> str:
	resolved = decision.profile_resolution.get("resolved_profile_id")
	if isinstance(resolved, str) and resolved.strip():
		return resolved.strip()
	if isinstance(request.profile_id, str) and request.profile_id not in {"", "auto"}:
		return request.profile_id
	return ALL_SCOPE


def _resolved_doc_type(request: QueryRequest, decision: RouterDecision) -> str | None:
	primary = _primary_arg_text(decision, "doc_type")
	if primary is not None:
		return primary
	return _DOC_TYPE_BY_INTENT.get(decision.intent)


def _primary_arg_text(decision: RouterDecision, key: str) -> str | None:
	value = decision.tool_plan.primary_args.get(key)
	if isinstance(value, str) and value.strip():
		return value.strip()
	return None


def _graph_seed_entity_ids(
	symbol_result: SymbolSearchResult | None,
	request: QueryRequest,
) -> tuple[str, ...]:
	if symbol_result is not None and symbol_result.candidates:
		return tuple(item.logical_entity_id for item in symbol_result.candidates[:3])
	selected = request.client_context.get("selected_entity_id")
	if isinstance(selected, str) and selected.strip():
		return (selected.strip(),)
	return ()


def _graph_max_depth(intent: QueryIntent) -> int:
	if intent in {"call_trace", "runtime_flow", "workspace_nav", "profile_diff"}:
		return 2
	return 1


def _build_query_warning(
	*,
	code: str,
	level: str,
	message: str,
	details: dict[str, object],
	actionable: bool,
	suggested_action: str | None = None,
) -> Warning:
	return Warning(
		level=level,
		code=code,
		message=message,
		details=details,
		actionable=actionable,
		suggested_action=suggested_action,
	)


def _suggest_next_queries(request: QueryRequest, decision: RouterDecision) -> tuple[str, ...]:
	base = request.query.strip()
	return (
		f"{base} 所在模块是什么？",
		f"{base} 请限定 profile_id 或 doc_type 后重试。",
		f"{base} 的相关证据有哪些？",
	)


def _extract_partial_ready_index_status(
	warnings: Sequence[Warning],
) -> dict[str, object] | None:
	required_keys = {
		"ready_sources",
		"missing_sources",
		"failed_jobs",
		"degradation_chain",
	}
	for warning in warnings:
		if warning.code != "index.partial_ready":
			continue
		if required_keys.issubset(warning.details):
			return dict(warning.details)
	return None


def _should_return_low_confidence(
	decision: RouterDecision,
	warnings: Sequence[Warning],
) -> bool:
	return decision.confidence < 0.50 or any(
		warning.code in {"router.low_confidence", "retrieval.low_confidence"}
		for warning in warnings
	)


def _ensure_low_confidence_warning(
	decision: RouterDecision,
	warnings: Sequence[Warning],
) -> list[Warning]:
	if any(
		warning.code in {"router.low_confidence", "retrieval.low_confidence"}
		for warning in warnings
	):
		return list(warnings)
	return [
		*warnings,
		_build_query_warning(
			code="retrieval.low_confidence",
			level="caution",
			message="Ranked candidates are available, but the supporting evidence remains low-confidence.",
			details={
				"intent": decision.intent,
				"router_confidence": round(decision.confidence, 3),
			},
			actionable=True,
			suggested_action="Add a module, path, symbol, doc_type, or profile_id and retry.",
		),
	]


def _build_summary(decision: RouterDecision, candidates: Sequence[FusionCandidate]) -> str:
	sources = _summary_sources(candidates)
	return (
		f"Hybrid fusion returned {len(candidates)} ranked candidates for intent={decision.intent} "
		f"using {', '.join(sources)}."
	)


def _build_low_confidence_summary(
	decision: RouterDecision,
	candidates: Sequence[FusionCandidate],
) -> str:
	sources = _summary_sources(candidates)
	return (
		f"Low-confidence retrieval returned {len(candidates)} candidate leads for intent={decision.intent} "
		f"using {', '.join(sources)}; verify the attached evidence before treating this as a conclusion."
	)


def _build_partial_ready_summary(
	decision: RouterDecision,
	candidates: Sequence[FusionCandidate],
) -> str:
	if not candidates:
		return "Partial index availability prevented a complete answer; no ranked evidence-bearing candidates were produced."
	sources = _summary_sources(candidates)
	return (
		f"Partial index availability returned {len(candidates)} ranked candidates for intent={decision.intent} "
		f"using {', '.join(sources)}; missing sources may hide additional evidence."
	)


def _summary_sources(candidates: Sequence[FusionCandidate]) -> list[str]:
	return sorted(
		{
			signal.retriever
			for candidate in candidates
			for signal in candidate.retrieval_signals
		}
	)


def _authority_from_match(relative_path: str | None, metadata: Mapping[str, object]) -> str:
	authority = metadata.get("authority_level")
	if isinstance(authority, str) and authority.strip():
		return authority.strip()
	if relative_path and relative_path.startswith("knowledge-sources/"):
		return "source_doc"
	return "derived"


def _metadata_text(metadata: Mapping[str, object], key: str) -> str | None:
	value = metadata.get(key)
	if isinstance(value, str) and value.strip():
		return value.strip()
	return None


def _metadata_int(metadata: Mapping[str, object], key: str) -> int | None:
	value = metadata.get(key)
	if isinstance(value, int) and value > 0:
		return value
	return None


def _evidence_type(candidate: FusionCandidate) -> str:
	if candidate.object_type in {"chunk", "doc", "evidence"}:
		return "doc"
	if candidate.object_type in {"layer", "feature"}:
		return "workspace"
	if candidate.object_type == "profile":
		return "profile"
	if candidate.object_type == "relation":
		return "graph"
	if candidate.relative_path and candidate.relative_path.startswith("knowledge-sources/"):
		return "doc"
	return "code"
