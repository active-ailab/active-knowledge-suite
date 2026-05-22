"""Evaluation runner boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.eval.cases import EvalCase, EvalCaseSuite, load_eval_suite
from active_knowledge_server.eval.metrics import build_category_coverage, build_suite_metrics
from active_knowledge_server.models import QueryRequest
from active_knowledge_server.query import QueryRouter

EvalCaseRunStatus = Literal["passed", "failed"]
EvalRunStatus = Literal["pass", "partial_ready", "fail"]


@dataclass(frozen=True)
class _StubProfileResolution:
	requested: str | None
	status: str
	resolved_profile_id: str | None = None

	def to_dict(self) -> dict[str, object]:
		return {
			"requested": self.requested,
			"status": self.status,
			"resolved_profile_id": self.resolved_profile_id,
			"profile_record_id": None,
			"source": "eval_stub",
			"confidence": None,
			"candidates": [],
			"warnings": [],
		}


@dataclass(frozen=True)
class _StubCollectedProfiles:
	resolution: _StubProfileResolution


class _EvalProfileCollector:
	"""Deterministic profile collector for router-contract eval cases."""

	def collect(
		self,
		snapshot_id: str | None = None,
		*,
		requested_profile_id: str | None = None,
		build_outputs_manifest: object | None = None,
		client_context: dict[str, object] | None = None,
	) -> _StubCollectedProfiles:
		del snapshot_id, build_outputs_manifest, client_context
		if requested_profile_id not in (None, "auto"):
			return _StubCollectedProfiles(
				resolution=_StubProfileResolution(
					requested=requested_profile_id,
					status="resolved",
					resolved_profile_id=requested_profile_id,
				)
			)
		return _StubCollectedProfiles(
			resolution=_StubProfileResolution(
				requested="auto",
				status="unresolved",
			)
		)


class EvalCaseResult(BaseModel):
	"""Observed result for one executed eval case."""

	model_config = ConfigDict(extra="forbid")

	case_id: str = Field(min_length=1)
	status: EvalCaseRunStatus
	observed_intent: str = Field(min_length=1)
	observed_primary_tool: str = Field(min_length=1)
	observed_route_mode: str = Field(min_length=1)
	warning_codes: tuple[str, ...] = ()
	failures: tuple[str, ...] = ()

	def to_dict(self) -> dict[str, object]:
		return self.model_dump(mode="json", exclude_none=True)


class EvalRunReport(BaseModel):
	"""Stable eval-run report emitted by CLI and tests."""

	model_config = ConfigDict(extra="forbid")

	schema_version: Literal["eval_run.v1"] = "eval_run.v1"
	gate_id: str = Field(min_length=1)
	suite_id: str = Field(min_length=1)
	status: EvalRunStatus
	started_at: str
	finished_at: str
	cases_file: str = Field(min_length=1)
	metrics: dict[str, Any] = Field(default_factory=dict)
	failures: tuple[dict[str, Any], ...] = ()
	warnings: tuple[dict[str, Any], ...] = ()
	artifacts: tuple[str, ...] = ()

	def to_dict(self) -> dict[str, Any]:
		return self.model_dump(mode="json", exclude_none=True)


class EvalRunner:
	"""Minimal E7-01 runner that executes router-contract eval cases."""

	def __init__(
		self,
		config: ActiveKnowledgeConfig,
		*,
		cwd: Path,
		profile_collector: object | None = None,
	) -> None:
		self._cwd = cwd
		self._router = QueryRouter.from_config(
			config,
			cwd=cwd,
			profile_collector=profile_collector or _EvalProfileCollector(),
		)

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		cwd: Path,
		profile_collector: object | None = None,
	) -> EvalRunner:
		return cls(config, cwd=cwd, profile_collector=profile_collector)

	def run(self, cases_file: Path, *, gate_id: str) -> EvalRunReport:
		"""Load, execute, and summarize one eval suite."""

		started_at = _utc_now()
		suite = load_eval_suite(cases_file)
		case_results = tuple(self._execute_case(case) for case in suite.cases)
		failed_results = tuple(result for result in case_results if result.status == "failed")
		coverage = build_category_coverage(suite)
		coverage_gaps = tuple(item.to_dict() for item in coverage if not item.ready)
		status: EvalRunStatus
		if failed_results:
			status = "fail"
		elif coverage_gaps:
			status = "partial_ready"
		else:
			status = "pass"
		warnings: list[dict[str, Any]] = []
		if coverage_gaps:
			warnings.append(
				{
					"code": "eval.coverage_incomplete",
					"message": "Eval suite executes successfully, but category minimums are not met yet.",
					"details": {"missing_categories": coverage_gaps},
				}
			)
		metrics = build_suite_metrics(
			suite,
			executed_cases=len(case_results),
			passed_cases=len(case_results) - len(failed_results),
			failed_cases=len(failed_results),
		)
		return EvalRunReport(
			gate_id=gate_id,
			suite_id=suite.suite_id,
			status=status,
			started_at=started_at,
			finished_at=_utc_now(),
			cases_file=str(cases_file),
			metrics=metrics,
			failures=tuple(
				{
					"case_id": result.case_id,
					"failures": list(result.failures),
					"observed_intent": result.observed_intent,
					"observed_primary_tool": result.observed_primary_tool,
					"observed_route_mode": result.observed_route_mode,
					"warning_codes": list(result.warning_codes),
				}
				for result in failed_results
			),
			warnings=tuple(warnings),
		)

	def _execute_case(self, case: EvalCase) -> EvalCaseResult:
		if case.execution_mode != "router_contract":
			return EvalCaseResult(
				case_id=case.case_id,
				status="failed",
				observed_intent="unsupported_execution_mode",
				observed_primary_tool="unsupported_execution_mode",
				observed_route_mode=case.execution_mode,
				failures=(f"unsupported execution_mode: {case.execution_mode}",),
			)

		decision = self._router.route(QueryRequest.model_validate(case.request.to_dict()))
		warning_codes = tuple(warning.code for warning in decision.warnings)
		failures: list[str] = []
		if decision.intent != case.expected_route.intent:
			failures.append(
				f"intent mismatch: expected {case.expected_route.intent}, got {decision.intent}"
			)
		if decision.tool_plan.primary_tool != case.expected_route.primary_tool:
			failures.append(
				"primary_tool mismatch: "
				f"expected {case.expected_route.primary_tool}, got {decision.tool_plan.primary_tool}"
			)
		if decision.tool_plan.route_mode != case.expected_route.route_mode:
			failures.append(
				f"route_mode mismatch: expected {case.expected_route.route_mode}, got {decision.tool_plan.route_mode}"
			)
		if decision.selected_view != case.expected_route.selected_view:
			failures.append(
				f"selected_view mismatch: expected {case.expected_route.selected_view}, got {decision.selected_view}"
			)
		if decision.selected_granularity != case.expected_route.selected_granularity:
			failures.append(
				"selected_granularity mismatch: "
				f"expected {case.expected_route.selected_granularity}, got {decision.selected_granularity}"
			)
		required_warning_codes = set(case.expected_route.required_warning_codes)
		observed_warning_codes = set(warning_codes)
		if not required_warning_codes.issubset(observed_warning_codes):
			failures.append(
				"missing required warnings: "
				f"expected {sorted(required_warning_codes)}, got {sorted(observed_warning_codes)}"
			)
		allowed_warning_codes = required_warning_codes | set(case.expected_route.allowed_warning_codes)
		unexpected_warning_codes = observed_warning_codes - allowed_warning_codes
		if unexpected_warning_codes:
			failures.append(
				f"unexpected warnings: {sorted(unexpected_warning_codes)}"
			)
		expected_profile_status = case.profile_requirement.expected_status
		observed_profile_status = str(decision.profile_resolution.get("status"))
		if observed_profile_status != expected_profile_status:
			failures.append(
				"profile status mismatch: "
				f"expected {expected_profile_status}, got {observed_profile_status}"
			)
		return EvalCaseResult(
			case_id=case.case_id,
			status="failed" if failures else "passed",
			observed_intent=decision.intent,
			observed_primary_tool=decision.tool_plan.primary_tool,
			observed_route_mode=decision.tool_plan.route_mode,
			warning_codes=warning_codes,
			failures=tuple(failures),
		)


def _utc_now() -> str:
	return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
