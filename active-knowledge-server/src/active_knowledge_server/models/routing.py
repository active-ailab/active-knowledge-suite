"""Shared routing decision models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from active_knowledge_server.models.query import QueryGranularity, QueryIntent, QueryView
from active_knowledge_server.models.responses import QueryResultStatus, Warning

MatchedSignalType = Literal[
    "symbol_like",
    "path_like",
    "macro_name",
    "error_code",
    "runtime_keyword",
    "profile_keyword",
    "api_keyword",
    "widget_keyword",
    "product_keyword",
    "project_keyword",
    "evidence_keyword",
    "caller_tool_hint",
    "client_context_hint",
    "lookup_phrase",
    "concept_phrase",
    "trace_phrase",
    "runtime_phrase",
    "navigation_phrase",
    "usage_phrase",
    "comparison_phrase",
    "vague_query",
]

SignalSource = Literal["query", "explicit_param", "caller_tool", "client_context", "history"]
ToolName = Literal[
    "kb_search",
    "code_resolve",
    "code_context",
    "code_trace",
    "config_impact",
    "docs_search",
    "workspace_view",
    "evidence_bundle",
]
RouteMode = Literal["direct", "chain", "explore"]


class MatchedSignal(BaseModel):
    """Explainable signal that contributed to the router decision."""

    model_config = ConfigDict(extra="forbid")

    type: MatchedSignalType
    value: str = Field(min_length=1)
    span: tuple[int, int] | None = None
    weight: float = Field(ge=0.0, le=1.0)
    source: SignalSource
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_span(self) -> MatchedSignal:
        """Ensure matched spans are monotonic when present."""

        if self.span is not None and self.span[1] < self.span[0]:
            raise ValueError("signal span end must be greater than or equal to start")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""

        return self.model_dump(mode="json", exclude_none=True)


class ToolChainStep(BaseModel):
    """One step in the router's planned tool chain."""

    model_config = ConfigDict(extra="forbid")

    tool: ToolName
    on_status: tuple[QueryResultStatus, ...] = ()
    when: str | None = None
    stop_if_evidence_sufficient: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""

        return self.model_dump(mode="json", exclude_none=True)


class ToolPlan(BaseModel):
    """Stable router output describing the next tool call and fallbacks."""

    model_config = ConfigDict(extra="forbid")

    route_mode: RouteMode
    primary_tool: ToolName
    primary_args: dict[str, Any] = Field(default_factory=dict)
    fallback_tools: tuple[ToolName, ...] = ()
    chain: tuple[ToolChainStep, ...] = ()
    prohibited_dependencies: tuple[str, ...] = (
        "ops_tools",
        "sqlite_tables",
        "lancedb_collections",
        "artifact_paths",
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""

        return self.model_dump(mode="json", exclude_none=True)


class RouteTraceEntry(BaseModel):
    """Structured route trace entry kept for debugging and auditability."""

    model_config = ConfigDict(extra="forbid")

    stage: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""

        return self.model_dump(mode="json", exclude_none=True)


class RouterDecision(BaseModel):
    """Stable output of the Query Router classifier and planner."""

    model_config = ConfigDict(extra="forbid")

    normalized_query: str = Field(min_length=1)
    intent: QueryIntent
    confidence: float = Field(ge=0.0, le=1.0)
    matched_signals: tuple[MatchedSignal, ...] = ()
    selected_view: QueryView
    selected_granularity: QueryGranularity
    profile_resolution: dict[str, Any] = Field(default_factory=dict)
    warnings: tuple[Warning, ...] = ()
    retriever_weights: dict[str, float] = Field(default_factory=dict)
    tool_plan: ToolPlan
    route_trace: tuple[RouteTraceEntry, ...] = ()

    @model_validator(mode="after")
    def validate_router_decision(self) -> RouterDecision:
        """Validate selected defaults and retriever weights."""

        if self.selected_view == "auto":
            raise ValueError("selected_view must be resolved before returning RouterDecision")
        if self.selected_granularity == "auto":
            raise ValueError(
                "selected_granularity must be resolved before returning RouterDecision"
            )
        if self.intent == "unknown" and self.tool_plan.primary_tool != "kb_search":
            raise ValueError("unknown intent must route through kb_search")
        if self.retriever_weights:
            total = sum(self.retriever_weights.values())
            if any(weight < 0.0 or weight > 1.0 for weight in self.retriever_weights.values()):
                raise ValueError("retriever weights must stay within [0.0, 1.0]")
            if total > 0.0 and abs(total - 1.0) > 1e-6:
                raise ValueError("retriever weights must be normalized")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""

        return self.model_dump(mode="json", exclude_none=True)