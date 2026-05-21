"""Shared query response contract models."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from active_knowledge_server.models.evidence import EvidenceRef
from active_knowledge_server.models.query import QueryIntent

QUERY_RESULT_SCHEMA_VERSION = "query_result.v1"

WarningLevel = Literal["info", "caution", "degraded", "blocked"]
ConfidenceBand = Literal["high", "medium", "low"]
QueryResultStatus = Literal[
	"ok",
	"zero_result",
	"multi_result",
	"ambiguous",
	"low_confidence",
	"partial_ready",
	"blocked",
	"error",
]


def confidence_to_band(confidence: float) -> ConfidenceBand:
	"""Convert a numeric confidence score to the shared confidence band."""

	if confidence >= 0.80:
		return "high"
	if confidence >= 0.50:
		return "medium"
	return "low"


class Warning(BaseModel):
	"""Shared warning contract for query results."""

	model_config = ConfigDict(extra="forbid")

	level: WarningLevel
	code: str = Field(min_length=1)
	message: str = Field(min_length=1)
	details: dict[str, Any] = Field(default_factory=dict)
	actionable: bool
	suggested_action: str | None = None
	affected_sources: tuple[str, ...] = ()
	evidence_refs: tuple[str, ...] = ()

	@model_validator(mode="after")
	def validate_actionable_contract(self) -> Warning:
		"""Require a suggested action when a warning is actionable."""

		if self.actionable and not (self.suggested_action and self.suggested_action.strip()):
			raise ValueError("actionable warnings must include suggested_action")
		return self

	def to_dict(self) -> dict[str, Any]:
		"""Return a JSON-serializable payload."""

		return self.model_dump(mode="json", exclude_none=True)


class Candidate(BaseModel):
	"""Stable disambiguation candidate contract."""

	model_config = ConfigDict(extra="forbid")

	disambiguation_key: str = Field(min_length=1)
	entity_type: str = Field(min_length=1)
	path: str | None = None
	module: str | None = None
	profile_id: str | None = None
	match_reason: str = Field(min_length=1)
	score: float = Field(ge=0.0, le=1.0)

	@model_validator(mode="after")
	def validate_locator(self) -> Candidate:
		"""Require at least one stable locator for candidate disambiguation."""

		if not any((self.path, self.module, self.profile_id)):
			raise ValueError(
				"candidates must include at least one of path, module, or profile_id"
			)
		return self

	def to_dict(self) -> dict[str, Any]:
		"""Return a JSON-serializable payload."""

		return self.model_dump(mode="json", exclude_none=True)


class SuggestedFilter(BaseModel):
	"""Recommended filter refinement for follow-up queries."""

	model_config = ConfigDict(extra="forbid")

	field: str = Field(min_length=1)
	value: str = Field(min_length=1)


class QueryResult(BaseModel):
	"""Stable outer contract shared by all V1 query tools."""

	model_config = ConfigDict(extra="forbid")

	schema_version: Literal["query_result.v1"] = QUERY_RESULT_SCHEMA_VERSION
	tool_name: str = Field(min_length=1)
	result_status: QueryResultStatus
	confidence: float = Field(ge=0.0, le=1.0)
	confidence_band: ConfidenceBand | None = None
	query_intent: QueryIntent
	snapshot_id: str = Field(min_length=1)
	profile_id: str = Field(min_length=1)
	summary: str = Field(min_length=1)
	items: tuple[dict[str, Any], ...] = ()
	candidates: tuple[Candidate, ...] = ()
	entities: tuple[dict[str, Any], ...] = ()
	relations: tuple[dict[str, Any], ...] = ()
	evidence_refs: tuple[EvidenceRef, ...] = ()
	warnings: tuple[Warning, ...] = ()
	next_queries: tuple[str, ...] = ()
	suggested_filters: tuple[SuggestedFilter, ...] = ()
	diagnostics: dict[str, Any] = Field(default_factory=dict)

	@model_validator(mode="before")
	@classmethod
	def populate_confidence_band(cls, data: Any) -> Any:
		"""Fill the confidence band from the numeric score when omitted."""

		if not isinstance(data, dict):
			return data
		if data.get("confidence_band") is None and "confidence" in data:
			updated = dict(data)
			updated["confidence_band"] = confidence_to_band(float(updated["confidence"]))
			return updated
		return data

	@model_validator(mode="after")
	def validate_contract(self) -> QueryResult:
		"""Enforce the result-status-specific contract constraints."""

		if self.confidence_band != confidence_to_band(self.confidence):
			raise ValueError("confidence_band must match the configured confidence thresholds")

		warning_codes = [warning.code for warning in self.warnings]
		if len(warning_codes) != len(set(warning_codes)):
			raise ValueError("warning codes must be unique within a single QueryResult")

		if self.result_status == "zero_result":
			if self.items or self.candidates or self.evidence_refs:
				raise ValueError(
					"zero_result may not include items, candidates, or evidence_refs"
				)
			if not (self.next_queries or self.suggested_filters):
				raise ValueError(
					"zero_result must include next_queries or suggested_filters"
				)
			if "retrieval.zero_result" not in warning_codes:
				raise ValueError("zero_result must include retrieval.zero_result warning")

		if self.result_status == "multi_result" and not self.candidates:
			raise ValueError("multi_result must include at least one candidate")

		if self.result_status == "ambiguous":
			required = self.diagnostics.get("required_context")
			if not isinstance(required, list) or not required:
				raise ValueError("ambiguous results must include diagnostics.required_context")
			if not self.next_queries:
				raise ValueError("ambiguous results must include next_queries")

		if self.result_status == "low_confidence":
			if self.confidence_band != "low":
				raise ValueError("low_confidence results must use confidence_band=low")
			if not self.evidence_refs:
				raise ValueError("low_confidence results must include evidence_refs")
			if not ({"router.low_confidence", "retrieval.low_confidence"} & set(warning_codes)):
				raise ValueError(
					"low_confidence must include router.low_confidence or retrieval.low_confidence"
				)

		if self.result_status == "partial_ready":
			index_status = self.diagnostics.get("index_status")
			required_keys = {
				"ready_sources",
				"missing_sources",
				"failed_jobs",
				"degradation_chain",
			}
			if not isinstance(index_status, dict) or not required_keys.issubset(index_status):
				raise ValueError(
					"partial_ready results must include diagnostics.index_status with the shared keys"
				)
			if "index.partial_ready" not in warning_codes:
				raise ValueError("partial_ready must include index.partial_ready warning")

		if self.result_status == "blocked":
			if self.items or self.candidates or self.evidence_refs:
				raise ValueError("blocked results may not include items, candidates, or evidence")
			if not self.warnings:
				raise ValueError("blocked results must include at least one blocked warning")
			if any(warning.level != "blocked" for warning in self.warnings):
				raise ValueError("blocked results may only include blocked-level warnings")
			if "blocked_reason" not in self.diagnostics:
				raise ValueError("blocked results must include diagnostics.blocked_reason")

		if self.result_status == "error":
			if self.items:
				raise ValueError("error results may not include items")
			if "request_id" not in self.diagnostics or "error_kind" not in self.diagnostics:
				raise ValueError(
					"error results must include diagnostics.request_id and diagnostics.error_kind"
				)

		return self

	@classmethod
	def blocked(
		cls,
		*,
		tool_name: str,
		summary: str,
		warnings: Sequence[Warning],
		next_queries: Sequence[str] = (),
		diagnostics: dict[str, Any] | None = None,
		query_intent: QueryIntent = "unknown",
		snapshot_id: str = "current",
		profile_id: str = "not_required",
		suggested_filters: Sequence[SuggestedFilter] = (),
	) -> QueryResult:
		"""Build a blocked response with the shared schema defaults."""

		return cls(
			tool_name=tool_name,
			result_status="blocked",
			confidence=0.0,
			query_intent=query_intent,
			snapshot_id=snapshot_id,
			profile_id=profile_id,
			summary=summary,
			warnings=tuple(warnings),
			next_queries=tuple(next_queries),
			suggested_filters=tuple(suggested_filters),
			diagnostics=dict(diagnostics or {}),
		)

	def to_dict(self) -> dict[str, Any]:
		"""Return a JSON-serializable payload."""

		return self.model_dump(mode="json", exclude_none=True)
