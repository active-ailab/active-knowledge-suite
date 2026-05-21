"""Rule-based query router for Active Knowledge Server."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing import ProfileCollector
from active_knowledge_server.models.query import QueryGranularity, QueryIntent, QueryRequest, QueryView
from active_knowledge_server.models.responses import Warning
from active_knowledge_server.models.routing import (
	MatchedSignal,
	RouteTraceEntry,
	RouterDecision,
	ToolChainStep,
	ToolPlan,
)

_LOW_CONFIDENCE_THRESHOLD: Final = 0.50
_HIGH_CONFIDENCE_THRESHOLD: Final = 0.80
_AMBIGUOUS_MARGIN: Final = 0.15
_PROHIBITED_DEPENDENCIES: Final[tuple[str, ...]] = (
	"ops_tools",
	"sqlite_tables",
	"lancedb_collections",
	"artifact_paths",
)
_PROFILE_SENSITIVE_INTENTS: Final[set[QueryIntent]] = {
	"profile_diff",
	"call_trace",
	"runtime_flow",
}
_CODE_ENTITY_SIGNAL_TYPES: Final[set[str]] = {
	"symbol_like",
	"path_like",
	"macro_name",
	"error_code",
}
_FUNCTION_RE: Final = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\(\)")
_PATH_RE: Final = re.compile(r"(?<!\w)(?:[\w.-]+/)+[\w.-]+(?:\.[A-Za-z0-9]+)?")
_MACRO_RE: Final = re.compile(r"\bCONFIG_[A-Z0-9_]+\b")
_SYMBOL_RE: Final = re.compile(
	r"\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b"
	r"|\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b"
	r"|\b[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*\b"
)
_ERROR_RE: Final = re.compile(r"\b(?:ERR|E)_[A-Z0-9_]+\b|0x[0-9A-Fa-f]+\b")
_DEFAULT_VIEW_BY_INTENT: Final[dict[QueryIntent, QueryView]] = {
	"code_exact": "code",
	"code_concept": "domain",
	"call_trace": "code",
	"runtime_flow": "runtime",
	"profile_diff": "profile",
	"api_lookup": "evidence",
	"widget_lookup": "feature",
	"workspace_nav": "workspace",
	"product_context": "feature",
	"project_context": "evidence",
	"evidence_lookup": "evidence",
	"unknown": "evidence",
}
_DEFAULT_GRANULARITY_BY_INTENT: Final[dict[QueryIntent, QueryGranularity]] = {
	"code_exact": "symbol",
	"code_concept": "module",
	"call_trace": "flow",
	"runtime_flow": "flow",
	"profile_diff": "profile",
	"api_lookup": "doc_section",
	"widget_lookup": "doc_section",
	"workspace_nav": "module",
	"product_context": "feature",
	"project_context": "doc_section",
	"evidence_lookup": "doc_section",
	"unknown": "doc_section",
}
_RAW_RETRIEVER_WEIGHTS: Final[dict[QueryIntent, dict[str, float]]] = {
	"code_exact": {"symbol": 0.55, "fts": 0.30, "vector": 0.05, "graph": 0.10},
	"code_concept": {"symbol": 0.10, "fts": 0.35, "vector": 0.30, "graph": 0.25},
	"call_trace": {"symbol": 0.40, "fts": 0.15, "vector": 0.05, "graph": 0.40},
	"runtime_flow": {"symbol": 0.15, "fts": 0.20, "vector": 0.10, "graph": 0.55},
	"profile_diff": {"symbol": 0.15, "fts": 0.30, "vector": 0.05, "graph": 0.50},
	"api_lookup": {"symbol": 0.10, "fts": 0.50, "vector": 0.30, "graph": 0.10},
	"widget_lookup": {"symbol": 0.10, "fts": 0.45, "vector": 0.30, "graph": 0.15},
	"workspace_nav": {"symbol": 0.10, "fts": 0.25, "vector": 0.10, "graph": 0.55},
	"product_context": {"symbol": 0.05, "fts": 0.45, "vector": 0.35, "graph": 0.15},
	"project_context": {"symbol": 0.05, "fts": 0.50, "vector": 0.30, "graph": 0.15},
	"evidence_lookup": {"symbol": 0.20, "fts": 0.35, "vector": 0.15, "graph": 0.30},
	"unknown": {"symbol": 0.15, "fts": 0.40, "vector": 0.20, "graph": 0.25},
}
_EXACT_LOOKUP_TERMS: Final[tuple[str, ...]] = (
	"在哪里",
	"在哪",
	"定义",
	"声明",
	"谁引用",
	"属于哪个模块",
	"属于哪个",
)
_CONCEPT_TERMS: Final[tuple[str, ...]] = (
	"如何实现",
	"怎么实现",
	"机制",
	"原理",
	"负责什么",
	"主要负责什么",
	"为什么这样",
	"如何把",
)
_CALL_TRACE_TERMS: Final[tuple[str, ...]] = (
	"调用链",
	"调用链怎么走",
	"谁调用",
	"谁调用了",
	"被谁调用",
	"链路怎么走",
	"入口到",
)
_RUNTIME_FLOW_TERMS: Final[tuple[str, ...]] = (
	"中断之后",
	"事件如何进入",
	"初始化顺序",
	"处理链路",
	"事件链",
	"消息链",
	"任务切换",
	"启动时",
)
_PROFILE_TERMS: Final[tuple[str, ...]] = (
	"profile",
	"board",
	"defconfig",
	".config",
	"kconfig",
	"影响哪些模块",
	"哪些 defconfig",
	"哪些 profile",
	"差异",
	"对比",
	"compare_to",
	"启用",
	"裁剪",
)
_API_TERMS: Final[tuple[str, ...]] = (
	"api",
	"接口",
	"参数",
	"返回值",
	"示例",
	"调用方式",
	"怎么用",
	"如何调用",
)
_WIDGET_TERMS: Final[tuple[str, ...]] = (
	"控件",
	"组件",
	"widget",
	"属性",
	"样式",
	"布局",
	"绑定",
	"绑定数据",
	"数据绑定",
	"点击事件",
)
_NAVIGATION_TERMS: Final[tuple[str, ...]] = (
	"在哪看",
	"去哪里改",
	"目录结构",
	"哪个仓",
	"从哪些目录开始",
	"新同事",
	"新人入口",
	"职责边界",
	"该去哪看",
)
_PRODUCT_TERMS: Final[tuple[str, ...]] = (
	"需求",
	"功能范围",
	"产品范围",
	"prd",
	"用户故事",
	"验收",
	"产品定义",
	"为什么要做",
)
_PROJECT_TERMS: Final[tuple[str, ...]] = (
	"里程碑",
	"排期",
	"风险",
	"进度",
	"版本计划",
	"release",
	"issue",
	"owner",
	"阻塞",
	"交付",
	"本周",
)
_RUNTIME_KEYWORDS: Final[tuple[str, ...]] = (
	"isr",
	"task",
	"thread",
	"queue",
	"timer",
	"semaphore",
	"mutex",
	"event",
	"fault",
	"boot",
	"startup",
	"中断",
	"任务",
)
_EVIDENCE_PRIMARY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
	re.compile(r"证据包", re.IGNORECASE),
	re.compile(r"出处是什么", re.IGNORECASE),
	re.compile(r"原文依据", re.IGNORECASE),
	re.compile(r"给我.+证据", re.IGNORECASE),
	re.compile(r"给出.+出处", re.IGNORECASE),
	re.compile(r"引用", re.IGNORECASE),
)
_EVIDENCE_MODIFIER_TERMS: Final[tuple[str, ...]] = (
	"请带证据",
	"带证据",
	"给出处",
	"附上依据",
)
_VAGUE_QUERY_TERMS: Final[tuple[str, ...]] = (
	"这个",
	"这里",
	"那个",
	"那边",
	"东西",
)
_WARNING_ACTIONS: Final[dict[str, str]] = {
	"router.low_confidence": "Provide a module, symbol, doc_type, or profile_id.",
	"router.medium_confidence": "Narrow the query with a module, path, or doc_type.",
	"router.ambiguous_intent": "Clarify whether this is a code, docs, runtime, or profile question.",
	"profile.multiple_candidates": "Specify profile_id or compare_to explicitly.",
	"profile.unresolved": "Provide a profile_id or inspect available profiles before retrying.",
	"profile.invalid": "Use a profile_id that exists for the current snapshot.",
}


@dataclass
class _IntentState:
	score: float = 0.0
	signals: list[MatchedSignal] = field(default_factory=list)
	signal_keys: set[tuple[str, str, str]] = field(default_factory=set)


class QueryRouter:
	"""Classify queries and build the initial tool plan."""

	def __init__(
		self,
		config: ActiveKnowledgeConfig,
		*,
		cwd: Path | None = None,
		profile_collector: ProfileCollector | None = None,
	) -> None:
		self._config = config
		self._cwd = (cwd or Path.cwd()).expanduser()
		self._workspace_root = resolve_runtime_path(config.project.workspace_root, self._cwd)
		self._profile_collector = profile_collector or ProfileCollector.from_config(
			config,
			cwd=self._cwd,
		)

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		cwd: Path | None = None,
		profile_collector: ProfileCollector | None = None,
	) -> QueryRouter:
		"""Build a query router from validated config."""

		return cls(config, cwd=cwd, profile_collector=profile_collector)

	def route(self, request: QueryRequest) -> RouterDecision:
		"""Classify one request and build a stable route decision."""

		normalized_query = normalize_query(request.query)
		states = self._score_intents(request, normalized_query)
		score_map = {intent: min(1.0, state.score) for intent, state in states.items()}
		ranked_intents = sorted(
			score_map.items(),
			key=lambda item: (item[1], item[0]),
			reverse=True,
		)
		top_intent, top_score = ranked_intents[0]
		second_intent, second_score = ranked_intents[1]
		warnings: list[Warning] = []

		selected_intent = top_intent
		matched_signals = self._sorted_signals(states[top_intent].signals)
		if top_score < _LOW_CONFIDENCE_THRESHOLD:
			selected_intent = "unknown"
			matched_signals = self._sorted_signals(
				states[top_intent].signals or states["unknown"].signals
			)
			warnings.append(
				build_router_warning(
					code="router.low_confidence",
					message="Query intent is unclear; returned a hybrid recall route only.",
					details={
						"top_intent": top_intent,
						"top_score": round(top_score, 3),
						"second_intent": second_intent,
						"second_score": round(second_score, 3),
					},
				)
			)
		else:
			if top_score < _HIGH_CONFIDENCE_THRESHOLD:
				warnings.append(
					build_router_warning(
						code="router.medium_confidence",
						message="Router selected an intent with medium confidence.",
						details={
							"intent": top_intent,
							"confidence": round(top_score, 3),
						},
					)
				)
			if top_score < _HIGH_CONFIDENCE_THRESHOLD and (top_score - second_score) < _AMBIGUOUS_MARGIN:
				warnings.append(
					build_router_warning(
						code="router.ambiguous_intent",
						message="Intent scores are close; follow-up disambiguation may be needed.",
						details={
							"primary_intent": top_intent,
							"primary_score": round(top_score, 3),
							"secondary_intent": second_intent,
							"secondary_score": round(second_score, 3),
						},
					)
				)

		selected_view = self._select_view(selected_intent, request, matched_signals)
		selected_granularity = self._select_granularity(
			selected_intent,
			request,
			matched_signals,
		)
		profile_resolution = self._resolve_profile(
			request=request,
			intent=selected_intent,
			selected_view=selected_view,
			matched_signals=matched_signals,
		)
		warnings.extend(self._profile_resolution_warnings(profile_resolution))
		retriever_weights = self._retriever_weights_for_intent(selected_intent)
		tool_plan = self._build_tool_plan(
			request=request,
			intent=selected_intent,
			selected_view=selected_view,
			selected_granularity=selected_granularity,
			matched_signals=matched_signals,
			profile_resolution=profile_resolution,
			confidence=top_score,
		)
		route_trace = (
			RouteTraceEntry(
				stage="input",
				summary="Captured router input.",
				details=request.to_dict(),
			),
			RouteTraceEntry(
				stage="normalization",
				summary="Normalized the query text for rule-based matching.",
				details={"normalized_query": normalized_query},
			),
			RouteTraceEntry(
				stage="intent_scores",
				summary="Calculated per-intent scores from matched signals.",
				details={
					"scores": {intent: round(score, 3) for intent, score in score_map.items()},
				},
			),
			RouteTraceEntry(
				stage="intent_selection",
				summary="Selected the primary intent after confidence and tie-break rules.",
				details={
					"intent": selected_intent,
					"confidence": round(top_score, 3),
					"matched_signals": [signal.to_dict() for signal in matched_signals],
				},
			),
			RouteTraceEntry(
				stage="projection_selection",
				summary="Resolved the default view and granularity.",
				details={
					"selected_view": selected_view,
					"selected_granularity": selected_granularity,
				},
			),
			RouteTraceEntry(
				stage="profile_resolution",
				summary="Resolved the effective profile context for this route.",
				details=profile_resolution,
			),
			RouteTraceEntry(
				stage="tool_plan",
				summary="Built the initial tool plan and fallbacks.",
				details=tool_plan.to_dict(),
			),
		)
		return RouterDecision(
			normalized_query=normalized_query,
			intent=selected_intent,
			confidence=top_score,
			matched_signals=tuple(matched_signals),
			selected_view=selected_view,
			selected_granularity=selected_granularity,
			profile_resolution=profile_resolution,
			warnings=tuple(self._dedupe_warnings(warnings)),
			retriever_weights=retriever_weights,
			tool_plan=tool_plan,
			route_trace=route_trace,
		)

	def _score_intents(
		self,
		request: QueryRequest,
		normalized_query: str,
	) -> dict[QueryIntent, _IntentState]:
		states: dict[QueryIntent, _IntentState] = {
			intent: _IntentState() for intent in _DEFAULT_VIEW_BY_INTENT
		}
		lower_query = normalized_query.lower()
		self._score_explicit_params(states, request)
		self._score_caller_tool(states, request.caller_tool)
		self._score_anchor_patterns(states, normalized_query)
		self._score_phrases(states, normalized_query, lower_query)
		self._score_client_context(states, request.client_context)
		self._apply_conflicts(states, normalized_query, lower_query)
		return states

	def _score_explicit_params(
		self,
		states: dict[QueryIntent, _IntentState],
		request: QueryRequest,
	) -> None:
		if request.domain != "auto":
			domain_map: dict[str, tuple[QueryIntent, float]] = {
				"api": ("api_lookup", 0.55),
				"widget": ("widget_lookup", 0.55),
				"product": ("product_context", 0.55),
				"project": ("project_context", 0.55),
				"code": ("code_exact", 0.20),
				"engineering": ("code_concept", 0.20),
				"docs": ("api_lookup", 0.15),
				"design": ("product_context", 0.15),
			}
			mapped = domain_map.get(request.domain)
			if mapped is not None:
				intent, weight = mapped
				add_signal(
					states,
					intent,
					signal_type=(
						"api_keyword"
						if request.domain == "api"
						else "widget_keyword"
						if request.domain == "widget"
						else "product_keyword"
						if request.domain in {"product", "design"}
						else "project_keyword"
						if request.domain == "project"
						else "client_context_hint"
					),
					value=request.domain,
					weight=weight,
					source="explicit_param",
					reason="explicit domain hint from QueryRequest",
				)
		if request.view != "auto":
			view_map: dict[str, tuple[QueryIntent, float]] = {
				"runtime": ("runtime_flow", 0.50),
				"profile": ("profile_diff", 0.50),
				"workspace": ("workspace_nav", 0.45),
				"layer": ("workspace_nav", 0.35),
				"domain": ("code_concept", 0.25),
				"feature": ("product_context", 0.20),
				"evidence": ("evidence_lookup", 0.40),
				"code": ("code_exact", 0.25),
			}
			mapped = view_map.get(request.view)
			if mapped is not None:
				intent, weight = mapped
				add_signal(
					states,
					intent,
					signal_type="client_context_hint",
					value=request.view,
					weight=weight,
					source="explicit_param",
					reason="explicit view hint from QueryRequest",
				)
		if request.granularity != "auto":
			granularity_map: dict[str, tuple[tuple[QueryIntent, float], ...]] = {
				"symbol": (("code_exact", 0.45),),
				"file": (("code_exact", 0.45),),
				"function": (("code_exact", 0.40),),
				"flow": (("call_trace", 0.35), ("runtime_flow", 0.35)),
				"profile": (("profile_diff", 0.45),),
				"module": (("workspace_nav", 0.30), ("code_concept", 0.25)),
				"directory": (("workspace_nav", 0.35),),
				"workspace": (("workspace_nav", 0.35),),
				"feature": (("product_context", 0.20), ("code_concept", 0.15)),
				"doc_section": (("api_lookup", 0.15), ("widget_lookup", 0.15), ("evidence_lookup", 0.10)),
			}
			for intent, weight in granularity_map.get(request.granularity, ()):  # pragma: no branch
				add_signal(
					states,
					intent,
					signal_type="client_context_hint",
					value=request.granularity,
					weight=weight,
					source="explicit_param",
					reason="explicit granularity hint from QueryRequest",
				)
		if request.profile_id not in (None, "auto", "current"):
			add_signal(
				states,
				"profile_diff",
				signal_type="profile_keyword",
				value=request.profile_id,
				weight=0.35,
				source="explicit_param",
				reason="explicit profile_id hint from QueryRequest",
			)

	def _score_caller_tool(
		self,
		states: dict[QueryIntent, _IntentState],
		caller_tool: str,
	) -> None:
		caller_map: dict[str, tuple[tuple[QueryIntent, float], ...]] = {
			"code_trace": (("call_trace", 0.40), ("runtime_flow", 0.30)),
			"config_impact": (("profile_diff", 0.45),),
			"docs_search": (("api_lookup", 0.15), ("widget_lookup", 0.15), ("product_context", 0.15), ("project_context", 0.15)),
			"workspace_view": (("workspace_nav", 0.35),),
			"evidence_bundle": (("evidence_lookup", 0.45),),
			"code_resolve": (("code_exact", 0.40),),
			"code_context": (("code_concept", 0.35),),
			"kb_search": (("unknown", 0.10),),
		}
		for intent, weight in caller_map.get(caller_tool, ()):  # pragma: no branch
			add_signal(
				states,
				intent,
				signal_type="caller_tool_hint",
				value=caller_tool,
				weight=weight,
				source="caller_tool",
				reason="caller tool prior from QueryRequest",
			)

	def _score_anchor_patterns(
		self,
		states: dict[QueryIntent, _IntentState],
		query: str,
	) -> None:
		function_spans: list[tuple[int, int]] = []
		for value, span in regex_matches(_PATH_RE, query):
			add_signal(
				states,
				"code_exact",
				signal_type="path_like",
				value=value,
				weight=0.35,
				source="query",
				reason="matches repository path syntax",
				span=span,
			)
			add_signal(
				states,
				"workspace_nav",
				signal_type="path_like",
				value=value,
				weight=0.20,
				source="query",
				reason="path-like input may scope workspace navigation",
				span=span,
			)
			add_signal(
				states,
				"code_concept",
				signal_type="path_like",
				value=value,
				weight=0.15,
				source="query",
				reason="path-like input may scope module explanation",
				span=span,
			)
			add_signal(
				states,
				"evidence_lookup",
				signal_type="path_like",
				value=value,
				weight=0.15,
				source="query",
				reason="path-like input can scope evidence collection",
				span=span,
			)
		for value, span in regex_matches(_FUNCTION_RE, query):
			function_spans.append(span)
			add_signal(
				states,
				"code_exact",
				signal_type="symbol_like",
				value=value,
				weight=0.35,
				source="query",
				reason="matches function-like symbol syntax",
				span=span,
			)
			add_signal(
				states,
				"call_trace",
				signal_type="symbol_like",
				value=value,
				weight=0.20,
				source="query",
				reason="function-like symbol can anchor a call trace",
				span=span,
			)
			add_signal(
				states,
				"evidence_lookup",
				signal_type="symbol_like",
				value=value.removesuffix("()"),
				weight=0.15,
				source="query",
				reason="function-like symbol can scope evidence retrieval",
				span=span,
			)
		for value, span in regex_matches(_MACRO_RE, query):
			add_signal(
				states,
				"code_exact",
				signal_type="macro_name",
				value=value,
				weight=0.25,
				source="query",
				reason="matches CONFIG_* macro syntax",
				span=span,
			)
			add_signal(
				states,
				"profile_diff",
				signal_type="macro_name",
				value=value,
				weight=0.25,
				source="query",
				reason="CONFIG_* macro often implies profile impact analysis",
				span=span,
			)
			add_signal(
				states,
				"evidence_lookup",
				signal_type="macro_name",
				value=value,
				weight=0.15,
				source="query",
				reason="CONFIG_* macro can scope evidence retrieval",
				span=span,
			)
		for value, span in regex_matches(_ERROR_RE, query):
			add_signal(
				states,
				"code_exact",
				signal_type="error_code",
				value=value,
				weight=0.25,
				source="query",
				reason="matches error-code or register-like syntax",
				span=span,
			)
		for value, span in regex_matches(_SYMBOL_RE, query):
			if any(start <= span[0] and span[1] <= end for start, end in function_spans):
				continue
			add_signal(
				states,
				"code_exact",
				signal_type="symbol_like",
				value=value,
				weight=0.22,
				source="query",
				reason="matches symbol-like token syntax",
				span=span,
			)
			add_signal(
				states,
				"call_trace",
				signal_type="symbol_like",
				value=value,
				weight=0.15,
				source="query",
				reason="symbol-like token can anchor a trace query",
				span=span,
			)
			add_signal(
				states,
				"evidence_lookup",
				signal_type="symbol_like",
				value=value,
				weight=0.12,
				source="query",
				reason="symbol-like token can scope evidence retrieval",
				span=span,
			)
			if "." in value:
				add_signal(
					states,
					"api_lookup",
					signal_type="symbol_like",
					value=value,
					weight=0.15,
					source="query",
					reason="dotted symbol often indicates an API method name",
					span=span,
				)

	def _score_phrases(
		self,
		states: dict[QueryIntent, _IntentState],
		query: str,
		lower_query: str,
	) -> None:
		self._add_term_signals(
			states,
			query,
			_EXACT_LOOKUP_TERMS,
			intent="code_exact",
			signal_type="lookup_phrase",
			weight=0.35,
			reason="lookup wording indicates exact code entity resolution",
		)
		self._add_term_signals(
			states,
			query,
			_CONCEPT_TERMS,
			intent="code_concept",
			signal_type="concept_phrase",
			weight=0.35,
			reason="concept wording indicates mechanism or responsibility explanation",
		)
		self._add_term_signals(
			states,
			query,
			_CALL_TRACE_TERMS,
			intent="call_trace",
			signal_type="trace_phrase",
			weight=0.45,
			reason="trace wording indicates source-to-target chain analysis",
		)
		self._add_term_signals(
			states,
			query,
			_RUNTIME_FLOW_TERMS,
			intent="runtime_flow",
			signal_type="runtime_phrase",
			weight=0.45,
			reason="runtime chain wording indicates concurrent flow analysis",
		)
		self._add_term_signals(
			states,
			query,
			_PROFILE_TERMS,
			intent="profile_diff",
			signal_type="profile_keyword",
			weight=0.35,
			reason="profile, board, or config terms indicate profile-aware analysis",
		)
		self._add_term_signals(
			states,
			query,
			_API_TERMS,
			intent="api_lookup",
			signal_type="api_keyword",
			weight=0.30,
			reason="API wording indicates docs-based API lookup",
		)
		self._add_term_signals(
			states,
			query,
			_WIDGET_TERMS,
			intent="widget_lookup",
			signal_type="widget_keyword",
			weight=0.40,
			reason="widget wording indicates UI component lookup",
		)
		self._add_term_signals(
			states,
			query,
			_NAVIGATION_TERMS,
			intent="workspace_nav",
			signal_type="navigation_phrase",
			weight=0.45,
			reason="navigation wording indicates workspace guidance",
		)
		self._add_term_signals(
			states,
			query,
			_PRODUCT_TERMS,
			intent="product_context",
			signal_type="product_keyword",
			weight=0.40,
			reason="product wording indicates PRD or feature-scope lookup",
		)
		self._add_term_signals(
			states,
			query,
			_PROJECT_TERMS,
			intent="project_context",
			signal_type="project_keyword",
			weight=0.40,
			reason="project wording indicates milestone or release lookup",
		)
		self._add_term_signals(
			states,
			query,
			_RUNTIME_KEYWORDS,
			intent="runtime_flow",
			signal_type="runtime_keyword",
			weight=0.25,
			reason="runtime entities indicate concurrent execution flow",
		)
		for pattern in _EVIDENCE_PRIMARY_PATTERNS:
			for value, span in regex_matches(pattern, query):
				add_signal(
					states,
					"evidence_lookup",
					signal_type="evidence_keyword",
					value=value,
					weight=0.50,
					source="query",
					reason="evidence wording indicates source or excerpt retrieval",
					span=span,
				)
		for term in _EVIDENCE_MODIFIER_TERMS:
			for value, span in find_term_matches(query, term):
				add_signal(
					states,
					"evidence_lookup",
					signal_type="evidence_keyword",
					value=value,
					weight=0.12,
					source="query",
					reason="evidence phrasing is present as a modifier, not the primary intent",
					span=span,
				)
		if is_vague_query(query, lower_query):
			add_signal(
				states,
				"unknown",
				signal_type="vague_query",
				value=query,
				weight=0.45,
				source="query",
				reason="query is too short or pronoun-heavy to classify confidently",
			)

	def _score_client_context(
		self,
		states: dict[QueryIntent, _IntentState],
		client_context: Mapping[str, object],
	) -> None:
		selected_symbol = client_context.get("selected_symbol")
		if isinstance(selected_symbol, str) and selected_symbol.strip():
			symbol = selected_symbol.strip()
			signal_type = "macro_name" if _MACRO_RE.fullmatch(symbol) else "symbol_like"
			add_signal(
				states,
				"profile_diff" if signal_type == "macro_name" else "code_exact",
				signal_type=signal_type,
				value=symbol,
				weight=0.20,
				source="client_context",
				reason="selected_symbol from client context provides an anchor",
			)
			add_signal(
				states,
				"call_trace",
				signal_type="client_context_hint",
				value=symbol,
				weight=0.10,
				source="client_context",
				reason="selected_symbol may anchor a trace follow-up",
			)
		active_file = client_context.get("active_file")
		if isinstance(active_file, str) and active_file.strip():
			add_signal(
				states,
				"workspace_nav",
				signal_type="client_context_hint",
				value=active_file,
				weight=0.15,
				source="client_context",
				reason="active_file scopes the workspace context",
			)
			add_signal(
				states,
				"code_concept",
				signal_type="client_context_hint",
				value=active_file,
				weight=0.10,
				source="client_context",
				reason="active_file can scope module explanation",
			)
		previous_intent = client_context.get("previous_intent")
		if isinstance(previous_intent, str) and previous_intent in _DEFAULT_VIEW_BY_INTENT:
			add_signal(
				states,
				previous_intent,
				signal_type="client_context_hint",
				value=previous_intent,
				weight=0.10,
				source="history",
				reason="previous_intent is used as a low-priority tie-breaker",
			)

	def _apply_conflicts(
		self,
		states: dict[QueryIntent, _IntentState],
		query: str,
		lower_query: str,
	) -> None:
		evidence_modifier_only = any(term in lower_query for term in _EVIDENCE_MODIFIER_TERMS)
		if evidence_modifier_only:
			strongest_non_evidence = max(
				state.score for intent, state in states.items() if intent != "evidence_lookup"
			)
			if strongest_non_evidence >= 0.45:
				states["evidence_lookup"].score = min(states["evidence_lookup"].score, 0.20)

		if any(term in query for term in _EXACT_LOOKUP_TERMS) and not any(
			term in lower_query for term in _PROFILE_TERMS
		):
			states["code_exact"].score += 0.10

		if any(term in query for term in _NAVIGATION_TERMS):
			states["workspace_nav"].score += 0.10

		has_trace_phrase = any(term in query for term in _CALL_TRACE_TERMS)
		symbol_signal_count = len(
			[signal for signal in states["call_trace"].signals if signal.type == "symbol_like"]
		)
		if has_trace_phrase and symbol_signal_count >= 2:
			states["call_trace"].score += 0.15
		elif has_trace_phrase and symbol_signal_count >= 1:
			states["call_trace"].score += 0.10

		has_runtime_phrase = any(term in query for term in _RUNTIME_FLOW_TERMS)
		if has_runtime_phrase and any(term in lower_query for term in _RUNTIME_KEYWORDS):
			states["runtime_flow"].score += 0.15

		if "职责边界" in query and "和" in query:
			states["workspace_nav"].score += 0.20

		if any(term in query for term in _CONCEPT_TERMS) and any(
			signal.type == "path_like" for signal in states["code_concept"].signals
		) and not any(term in query for term in _NAVIGATION_TERMS):
			states["code_concept"].score += 0.15

		if any(term in lower_query for term in _PROJECT_TERMS):
			states["project_context"].score += 0.15
			states["product_context"].score = max(0.0, states["product_context"].score - 0.05)

		if any(term in lower_query for term in _PRODUCT_TERMS) and not any(
			term in lower_query for term in _PROJECT_TERMS
		):
			states["product_context"].score += 0.10

		api_signal_types = {signal.type for signal in states["api_lookup"].signals}
		if {"api_keyword", "symbol_like"}.issubset(api_signal_types):
			states["api_lookup"].score += 0.05

	def _select_view(
		self,
		intent: QueryIntent,
		request: QueryRequest,
		matched_signals: Iterable[MatchedSignal],
	) -> QueryView:
		if request.view != "auto":
			return request.view
		signal_types = {signal.type for signal in matched_signals}
		if intent == "code_exact":
			return "code"
		if intent == "code_concept" and "path_like" in signal_types:
			return "code"
		if intent == "workspace_nav" and any(
			signal.value == "职责边界" for signal in matched_signals
		):
			return "layer"
		return _DEFAULT_VIEW_BY_INTENT[intent]

	def _select_granularity(
		self,
		intent: QueryIntent,
		request: QueryRequest,
		matched_signals: Iterable[MatchedSignal],
	) -> QueryGranularity:
		if request.granularity != "auto":
			return request.granularity
		signal_types = {signal.type for signal in matched_signals}
		signal_values = {signal.value for signal in matched_signals}
		if intent == "code_exact" and "path_like" in signal_types:
			return "file"
		if intent == "workspace_nav":
			if any("目录" in value or "/" in value for value in signal_values):
				return "directory"
			return "module"
		if intent == "evidence_lookup" and signal_types & _CODE_ENTITY_SIGNAL_TYPES:
			return "symbol"
		return _DEFAULT_GRANULARITY_BY_INTENT[intent]

	def _resolve_profile(
		self,
		*,
		request: QueryRequest,
		intent: QueryIntent,
		selected_view: QueryView,
		matched_signals: Iterable[MatchedSignal],
	) -> dict[str, object]:
		if request.profile_id is None:
			return {
				"requested": None,
				"status": "not_required",
				"resolved_profile_id": None,
				"profile_record_id": None,
				"source": "explicit_null",
				"confidence": None,
				"candidates": [],
				"warnings": [],
			}
		requires_profile = (
			request.profile_id == "current"
			or request.profile_id not in (None, "auto")
			or selected_view == "profile"
			or intent in _PROFILE_SENSITIVE_INTENTS
			or any(signal.type == "profile_keyword" for signal in matched_signals)
		)
		if not requires_profile:
			return {
				"requested": request.profile_id,
				"status": "not_required",
				"resolved_profile_id": None,
				"profile_record_id": None,
				"source": "not_required",
				"confidence": None,
				"candidates": [],
				"warnings": [],
			}
		requested_profile_id = request.profile_id
		collector_request: str | None
		if requested_profile_id in {"auto", "current"}:
			collector_request = None
		else:
			collector_request = requested_profile_id
		collected = self._profile_collector.collect(
			snapshot_id=request.snapshot_id,
			requested_profile_id=collector_request,
			client_context=request.client_context,
		)
		resolution = collected.resolution.to_dict()
		if requested_profile_id == "current":
			resolution["requested"] = "current"
		return resolution

	def _profile_resolution_warnings(
		self,
		profile_resolution: Mapping[str, object],
	) -> list[Warning]:
		warnings_payload = profile_resolution.get("warnings")
		if not isinstance(warnings_payload, list):
			return []
		result: list[Warning] = []
		for warning_payload in warnings_payload:
			if not isinstance(warning_payload, dict):
				continue
			code = str(warning_payload.get("code", "profile.unresolved"))
			result.append(
				Warning(
					level=str(warning_payload.get("level", "caution")),
					code=code,
					message=str(warning_payload.get("message", "Profile resolution warning.")),
					details=dict(warning_payload.get("details", {})),
					actionable=True,
					suggested_action=_WARNING_ACTIONS.get(
						code,
						"Specify an explicit profile_id or reduce the profile-dependent scope.",
					),
				)
			)
		return result

	def _retriever_weights_for_intent(self, intent: QueryIntent) -> dict[str, float]:
		raw = dict(_RAW_RETRIEVER_WEIGHTS[intent])
		enabled = {
			"symbol": self._config.query.hybrid.enable_symbol,
			"fts": self._config.query.hybrid.enable_fts,
			"vector": self._config.query.hybrid.enable_vector,
			"graph": self._config.query.hybrid.enable_graph_expand,
		}
		filtered = {name: weight if enabled[name] else 0.0 for name, weight in raw.items()}
		total = sum(filtered.values())
		if total <= 0.0:
			return {"fts": 1.0}
		return {name: round(weight / total, 6) for name, weight in filtered.items() if weight > 0.0}

	def _build_tool_plan(
		self,
		*,
		request: QueryRequest,
		intent: QueryIntent,
		selected_view: QueryView,
		selected_granularity: QueryGranularity,
		matched_signals: Iterable[MatchedSignal],
		profile_resolution: Mapping[str, object],
		confidence: float,
	) -> ToolPlan:
		signals_by_type = group_signals_by_type(matched_signals)
		resolved_profile_id = profile_resolution.get("resolved_profile_id")
		route_mode = "direct"
		primary_tool = "kb_search"
		primary_args: dict[str, object] = {
			"query": request.normalized_query,
			"snapshot_id": request.snapshot_id or self._config.project.default_snapshot,
		}
		fallback_tools: list[str] = []
		chain: list[ToolChainStep] = []

		if intent == "unknown":
			route_mode = "explore"
			primary_tool = "kb_search"
			primary_args.update({
				"domain": request.domain,
				"view": selected_view,
				"top_k": self._config.query.default_top_k,
			})
			fallback_tools = ["code_resolve", "docs_search", "workspace_view", "config_impact"]
		elif intent == "code_exact":
			primary_tool = "code_resolve"
			primary_args.update(
				build_code_resolve_args(
					matched_signals=matched_signals,
					granularity=selected_granularity,
					profile_id=resolved_profile_id,
				)
			)
			fallback_tools = ["code_context", "evidence_bundle", "kb_search"]
		elif intent == "code_concept":
			anchored = bool(set(signals_by_type) & _CODE_ENTITY_SIGNAL_TYPES)
			primary_tool = "code_context" if anchored else "kb_search"
			route_mode = "direct" if anchored else "explore"
			primary_args.update(
				{
					"view": selected_view,
					"granularity": selected_granularity,
					"domain": "code",
					"profile_id": resolved_profile_id,
				}
			)
			fallback_tools = ["workspace_view", "docs_search", "evidence_bundle"]
		elif intent == "call_trace":
			route_mode = "chain"
			primary_tool = "code_resolve"
			primary_args.update(
				build_code_resolve_args(
					matched_signals=matched_signals,
					granularity="symbol",
					profile_id=resolved_profile_id,
				)
			)
			fallback_tools = ["code_context", "kb_search", "evidence_bundle"]
			chain = [
				ToolChainStep(
					tool="code_trace",
					on_status=("ok", "multi_result", "partial_ready"),
					when="source_or_target_resolved",
					stop_if_evidence_sufficient=True,
				),
				ToolChainStep(
					tool="evidence_bundle",
					when="summary_has_entities_without_source_refs",
				),
			]
		elif intent == "runtime_flow":
			route_mode = "chain"
			anchored = bool(set(signals_by_type) & _CODE_ENTITY_SIGNAL_TYPES)
			primary_tool = "code_trace" if anchored else "workspace_view"
			primary_args.update(
				build_runtime_args(
					matched_signals=matched_signals,
					selected_view=selected_view,
					profile_id=resolved_profile_id,
				)
			)
			fallback_tools = ["docs_search", "kb_search", "evidence_bundle"]
			chain = [
				ToolChainStep(
					tool="docs_search",
					on_status=("partial_ready", "low_confidence"),
					when="runtime_evidence_needs_engineering_docs",
				),
				ToolChainStep(
					tool="evidence_bundle",
					when="runtime_chain_requires_source_refs",
				),
			]
		elif intent == "profile_diff":
			route_mode = "chain"
			primary_tool = "config_impact"
			primary_args.update(
				build_profile_args(
					matched_signals=matched_signals,
					profile_resolution=profile_resolution,
					client_context=request.client_context,
				)
			)
			fallback_tools = ["code_resolve", "workspace_view", "evidence_bundle"]
			chain = [
				ToolChainStep(
					tool="evidence_bundle",
					when="impact_summary_has_entities_without_source_refs",
				)
			]
		elif intent == "api_lookup":
			primary_tool = "docs_search"
			primary_args.update({"doc_type": "api", "domain": "api"})
			fallback_tools = ["code_resolve", "kb_search", "evidence_bundle"]
		elif intent == "widget_lookup":
			primary_tool = "docs_search"
			primary_args.update({"doc_type": "widget", "domain": "widget"})
			fallback_tools = ["workspace_view", "code_resolve", "evidence_bundle"]
		elif intent == "workspace_nav":
			primary_tool = "workspace_view"
			primary_args.update({"view": selected_view, "granularity": selected_granularity})
			fallback_tools = ["code_context", "kb_search", "evidence_bundle"]
		elif intent == "product_context":
			primary_tool = "docs_search" if confidence >= _LOW_CONFIDENCE_THRESHOLD else "kb_search"
			primary_args.update({
				"doc_type": "product",
				"domain": "product",
				"view": selected_view,
			})
			fallback_tools = ["workspace_view", "evidence_bundle"]
		elif intent == "project_context":
			primary_tool = "docs_search"
			primary_args.update({"doc_type": "project", "domain": "project"})
			fallback_tools = ["evidence_bundle", "workspace_view"]
		elif intent == "evidence_lookup":
			primary_tool = "evidence_bundle"
			primary_args.update({
				"entity_hint": first_signal_value(matched_signals, "symbol_like")
				or first_signal_value(matched_signals, "macro_name")
				or first_signal_value(matched_signals, "path_like"),
				"view": selected_view,
			})
			fallback_tools = ["code_resolve", "docs_search", "kb_search"]

		return ToolPlan(
			route_mode=route_mode,
			primary_tool=primary_tool,
			primary_args={key: value for key, value in primary_args.items() if value is not None},
			fallback_tools=tuple(dict.fromkeys(fallback_tools)),
			chain=tuple(chain),
			prohibited_dependencies=_PROHIBITED_DEPENDENCIES,
		)

	def _add_term_signals(
		self,
		states: dict[QueryIntent, _IntentState],
		query: str,
		terms: Iterable[str],
		*,
		intent: QueryIntent,
		signal_type: str,
		weight: float,
		reason: str,
	) -> None:
		for term in terms:
			for value, span in find_term_matches(query, term):
				add_signal(
					states,
					intent,
					signal_type=signal_type,
					value=value,
					weight=weight,
					source="query",
					reason=reason,
					span=span,
				)

	@staticmethod
	def _sorted_signals(signals: Iterable[MatchedSignal]) -> list[MatchedSignal]:
		return sorted(
			signals,
			key=lambda signal: (-signal.weight, signal.type, signal.value),
		)

	@staticmethod
	def _dedupe_warnings(warnings: Iterable[Warning]) -> list[Warning]:
		unique: dict[str, Warning] = {}
		for warning in warnings:
			unique.setdefault(warning.code, warning)
		return list(unique.values())


def normalize_query(query: str) -> str:
	"""Normalize whitespace while preserving the original query semantics."""

	normalized = query.replace("\u3000", " ").strip()
	normalized = re.sub(r"\s+", " ", normalized)
	return normalized


def regex_matches(pattern: re.Pattern[str], query: str) -> list[tuple[str, tuple[int, int]]]:
	"""Return regex matches with spans."""

	return [(match.group(0), match.span()) for match in pattern.finditer(query)]


def find_term_matches(query: str, term: str) -> list[tuple[str, tuple[int, int]]]:
	"""Return case-insensitive substring matches with spans."""

	lower_query = query.lower()
	lower_term = term.lower()
	start = 0
	matches: list[tuple[str, tuple[int, int]]] = []
	while True:
		index = lower_query.find(lower_term, start)
		if index < 0:
			return matches
		end = index + len(term)
		matches.append((query[index:end], (index, end)))
		start = end


def is_vague_query(query: str, lower_query: str) -> bool:
	"""Return whether the query lacks stable routing anchors."""

	has_anchor = bool(
		_FUNCTION_RE.search(query)
		or _PATH_RE.search(query)
		or _MACRO_RE.search(query)
		or _SYMBOL_RE.search(query)
	)
	if has_anchor:
		return False
	if len(normalize_query(query)) <= 10:
		return True
	return any(term in lower_query for term in _VAGUE_QUERY_TERMS)


def add_signal(
	states: dict[QueryIntent, _IntentState],
	intent: QueryIntent,
	*,
	signal_type: str,
	value: str,
	weight: float,
	source: str,
	reason: str,
	span: tuple[int, int] | None = None,
) -> None:
	"""Add one explainable signal to an intent accumulator."""

	state = states[intent]
	key = (signal_type, value, source)
	if key in state.signal_keys:
		return
	state.signal_keys.add(key)
	state.score += weight
	state.signals.append(
		MatchedSignal(
			type=signal_type,
			value=value,
			span=span,
			weight=weight,
			source=source,
			reason=reason,
		)
	)


def group_signals_by_type(signals: Iterable[MatchedSignal]) -> dict[str, list[MatchedSignal]]:
	"""Group matched signals by signal type."""

	grouped: dict[str, list[MatchedSignal]] = defaultdict(list)
	for signal in signals:
		grouped[signal.type].append(signal)
	return grouped


def first_signal_value(signals: Iterable[MatchedSignal], signal_type: str) -> str | None:
	"""Return the first value for the requested signal type."""

	for signal in signals:
		if signal.type == signal_type:
			return signal.value
	return None


def build_router_warning(
	*,
	code: str,
	message: str,
	details: Mapping[str, object],
) -> Warning:
	"""Build a shared router warning with a stable suggested action."""

	return Warning(
		level="caution",
		code=code,
		message=message,
		details=dict(details),
		actionable=True,
		suggested_action=_WARNING_ACTIONS.get(code, "Clarify the query and retry."),
	)


def build_code_resolve_args(
	*,
	matched_signals: Iterable[MatchedSignal],
	granularity: QueryGranularity,
	profile_id: object,
) -> dict[str, object]:
	"""Build code_resolve-style primary args from matched signals."""

	macro = first_signal_value(matched_signals, "macro_name")
	path = first_signal_value(matched_signals, "path_like")
	symbol = first_signal_value(matched_signals, "symbol_like")
	return {
		"entity_type": "macro" if macro else "path" if path or granularity == "file" else "symbol",
		"symbol_or_path": macro or path or symbol,
		"profile_id": profile_id,
	}


def build_runtime_args(
	*,
	matched_signals: Iterable[MatchedSignal],
	selected_view: QueryView,
	profile_id: object,
) -> dict[str, object]:
	"""Build runtime-flow-specific primary args."""

	trace_type = "runtime"
	runtime_terms = {signal.value.lower() for signal in matched_signals if signal.type == "runtime_keyword"}
	if any("isr" in value or "中断" in value for value in runtime_terms):
		trace_type = "isr"
	elif any("queue" in value for value in runtime_terms):
		trace_type = "queue"
	elif any("timer" in value for value in runtime_terms):
		trace_type = "timer"
	elif any("fault" in value for value in runtime_terms):
		trace_type = "fault"
	elif any("startup" in value or "boot" in value for value in runtime_terms):
		trace_type = "startup"
	return {
		"trace_type": trace_type,
		"view": selected_view,
		"profile_id": profile_id,
	}


def build_profile_args(
	*,
	matched_signals: Iterable[MatchedSignal],
	profile_resolution: Mapping[str, object],
	client_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
	"""Build config_impact-style primary args from matched signals."""

	resolved_profile_id = profile_resolution.get("resolved_profile_id")
	compare_to = _compare_to_profile_arg(
		client_context,
		primary_profile_id=resolved_profile_id if isinstance(resolved_profile_id, str) else None,
	)
	return {
		"macro_or_config": first_signal_value(matched_signals, "macro_name")
		or first_signal_value(matched_signals, "profile_keyword"),
		"profile_id": resolved_profile_id,
		"compare_to": compare_to,
		"resolution_status": profile_resolution.get("status"),
	}


def _compare_to_profile_arg(
	client_context: Mapping[str, object] | None,
	*,
	primary_profile_id: str | None,
) -> str | None:
	"""Return an explicit compare_to profile from client context when present."""

	if client_context is None:
		return None
	compare_to = client_context.get("compare_to")
	if isinstance(compare_to, str):
		value = compare_to.strip()
		if value and value != primary_profile_id:
			return value
	profile_ids = client_context.get("profile_ids")
	if not isinstance(profile_ids, (list, tuple)):
		return None
	for item in profile_ids:
		if not isinstance(item, str):
			continue
		value = item.strip()
		if value and value != primary_profile_id:
			return value
	return None
