"""Shared query request contract models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

QueryIntent = Literal[
	"code_exact",
	"code_concept",
	"call_trace",
	"runtime_flow",
	"profile_diff",
	"api_lookup",
	"widget_lookup",
	"workspace_nav",
	"product_context",
	"project_context",
	"evidence_lookup",
	"unknown",
]

QueryDomain = Literal[
	"auto",
	"code",
	"docs",
	"api",
	"widget",
	"product",
	"project",
	"design",
	"engineering",
]

QueryView = Literal[
	"auto",
	"workspace",
	"layer",
	"domain",
	"feature",
	"runtime",
	"profile",
	"evidence",
	"code",
]

QueryGranularity = Literal[
	"auto",
	"workspace",
	"directory",
	"module",
	"file",
	"symbol",
	"function",
	"flow",
	"feature",
	"profile",
	"doc_section",
]

CallerTool = Literal[
	"kb_search",
	"code_resolve",
	"code_context",
	"code_trace",
	"config_impact",
	"docs_search",
	"workspace_view",
	"evidence_bundle",
	"skill",
	"client",
]


class QueryRequest(BaseModel):
	"""Stable query router input contract."""

	model_config = ConfigDict(extra="forbid")

	query: str = Field(min_length=1)
	domain: QueryDomain = "auto"
	view: QueryView = "auto"
	granularity: QueryGranularity = "auto"
	profile_id: str | None = "auto"
	snapshot_id: str | None = "current"
	caller_tool: CallerTool = "client"
	client_context: dict[str, Any] = Field(default_factory=dict)

	@field_validator("query")
	@classmethod
	def validate_query(cls, value: str) -> str:
		"""Reject blank queries while preserving the original text."""

		if not value.strip():
			raise ValueError("query must not be blank")
		return value

	@property
	def normalized_query(self) -> str:
		"""Return the normalized text used for routing and retrieval."""

		return " ".join(self.query.split())

	def to_dict(self) -> dict[str, Any]:
		"""Return a JSON-serializable payload."""

		return self.model_dump(mode="json")
