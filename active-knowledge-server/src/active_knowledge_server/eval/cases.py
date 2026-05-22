"""Evaluation case model boundary."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from active_knowledge_server.models.query import QueryRequest
from active_knowledge_server.models.routing import RouteMode, ToolName

EvalCaseCategory = Literal[
	"symbol_lookup",
	"api_documentation",
	"widget_usage",
	"workspace_navigation",
	"profile_impact",
	"feature_domain_cross_layer",
	"warning_degradation",
	"storage_incremental",
]
EvalCasePriority = Literal["P0", "P1", "P2"]
EvalBlockingLevel = Literal["blocker", "warning", "advisory"]
EvalExecutionMode = Literal["router_contract"]
EvalExpectedEvidenceKind = Literal[
	"symbol",
	"module",
	"path",
	"doc_section",
	"feature",
	"profile",
	"query_refinement",
]
EvalProfileStatus = Literal["resolved", "unresolved", "not_required"]


class EvalExpectedEvidence(BaseModel):
	"""Human-curated target that the eval case is supposed to recover or explain."""

	model_config = ConfigDict(extra="forbid")

	kind: EvalExpectedEvidenceKind
	locator: str = Field(min_length=1)
	rationale: str | None = None


class EvalProfileRequirement(BaseModel):
	"""Profile and snapshot constraints attached to one eval case."""

	model_config = ConfigDict(extra="forbid")

	requested_profile_id: str | None
	compare_to: str | None = None
	expected_status: EvalProfileStatus


class EvalRouteExpectation(BaseModel):
	"""Expected router-level behavior for one eval case."""

	model_config = ConfigDict(extra="forbid")

	intent: str = Field(min_length=1)
	primary_tool: ToolName
	route_mode: RouteMode
	selected_view: str = Field(min_length=1)
	selected_granularity: str = Field(min_length=1)
	required_warning_codes: tuple[str, ...]
	allowed_warning_codes: tuple[str, ...]


class EvalCase(BaseModel):
	"""One runnable eval case derived from user-facing Skill examples."""

	model_config = ConfigDict(extra="forbid")

	case_id: str = Field(min_length=1)
	title: str = Field(min_length=1)
	category: EvalCaseCategory
	priority: EvalCasePriority
	execution_mode: EvalExecutionMode = "router_contract"
	blocking_level: EvalBlockingLevel
	include_in_release_gate: bool = True
	source_refs: tuple[str, ...] = ()
	input_tool: ToolName
	request: QueryRequest
	snapshot_requirement: str = "current"
	profile_requirement: EvalProfileRequirement
	expected_route: EvalRouteExpectation
	expected_evidence: tuple[EvalExpectedEvidence, ...] = ()
	tags: tuple[str, ...] = ()

	@model_validator(mode="after")
	def validate_case(self) -> EvalCase:
		"""Enforce the E7-01 shared case contract."""

		requested_snapshot = self.request.snapshot_id or "current"
		if requested_snapshot != self.snapshot_requirement:
			raise ValueError("snapshot_requirement must match request.snapshot_id")
		if self.input_tool != self.expected_route.primary_tool:
			raise ValueError("input_tool must match expected_route.primary_tool")
		if not self.source_refs:
			raise ValueError("eval cases must reference at least one source fixture or document")
		if not self.expected_evidence:
			raise ValueError("eval cases must declare at least one expected_evidence target")
		if self.profile_requirement.requested_profile_id != self.request.profile_id:
			raise ValueError(
				"profile_requirement.requested_profile_id must match request.profile_id"
			)
		compare_to = self.profile_requirement.compare_to
		if compare_to is not None and self.request.client_context.get("compare_to") != compare_to:
			raise ValueError(
				"profile_requirement.compare_to must match request.client_context['compare_to']"
			)
		return self


class EvalCaseSuite(BaseModel):
	"""YAML-backed eval case suite loaded by the E7 runner."""

	model_config = ConfigDict(extra="forbid")

	schema_version: Literal["eval_cases.v1"] = "eval_cases.v1"
	suite_id: str = Field(min_length=1)
	description: str = Field(min_length=1)
	generated_from: tuple[str, ...] = ()
	cases: tuple[EvalCase, ...]

	@model_validator(mode="after")
	def validate_unique_case_ids(self) -> EvalCaseSuite:
		"""Reject duplicated case identifiers."""

		case_ids = [case.case_id for case in self.cases]
		duplicates = [case_id for case_id, count in Counter(case_ids).items() if count > 1]
		if duplicates:
			raise ValueError(f"duplicate eval case ids: {', '.join(sorted(duplicates))}")
		return self

	def category_counts(self, *, release_gate_only: bool = False) -> dict[str, int]:
		"""Return category counts for coverage summaries."""

		cases = self.cases
		if release_gate_only:
			cases = tuple(case for case in cases if case.include_in_release_gate)
		counts = Counter(case.category for case in cases)
		return dict(sorted(counts.items(), key=lambda item: item[0]))

	def to_dict(self) -> dict[str, Any]:
		"""Return a JSON-serializable payload."""

		return self.model_dump(mode="json", exclude_none=True)


def load_eval_suite(path: Path) -> EvalCaseSuite:
	"""Load one eval case suite from YAML."""

	payload = yaml.safe_load(path.read_text(encoding="utf-8"))
	if not isinstance(payload, dict):
		raise ValueError("eval case file must contain a top-level mapping")
	return EvalCaseSuite.model_validate(payload)
