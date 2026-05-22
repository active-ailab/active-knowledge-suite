"""Evaluation runner boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.eval.benchmark import QualityBenchmark
from active_knowledge_server.eval.cases import EvalCase, EvalCaseSuite, load_eval_suite
from active_knowledge_server.eval.metrics import (
	PerformanceProbeObservation,
	QualityCaseObservation,
	build_category_coverage,
	build_performance_gate_metrics,
	build_quality_gate_metrics,
	build_suite_metrics,
)
from active_knowledge_server.eval.performance import PerformanceBenchmark
from active_knowledge_server.models import QueryRequest
from active_knowledge_server.models.responses import QueryResult
from active_knowledge_server.query import QueryRouter

EvalCaseRunStatus = Literal["passed", "failed"]
EvalRunStatus = Literal["pass", "partial_ready", "fail"]
EvalSuiteKind = Literal["router", "quality", "performance"]


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
	observed_result_status: str | None = None
	warning_codes: tuple[str, ...] = ()
	schema_compliant: bool | None = None
	evidence_hit: bool | None = None
	first_relevant_rank: int | None = None
	profile_correct: bool | None = None
	warning_quality_ok: bool | None = None
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
		quality_benchmark_factory: Callable[[], object] | None = None,
		performance_benchmark_factory: Callable[[], object] | None = None,
	) -> None:
		self._cwd = cwd
		self._router = QueryRouter.from_config(
			config,
			cwd=cwd,
			profile_collector=profile_collector or _EvalProfileCollector(),
		)
		self._quality_benchmark_factory = quality_benchmark_factory or QualityBenchmark
		self._performance_benchmark_factory = (
			performance_benchmark_factory or PerformanceBenchmark
		)

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		cwd: Path,
		profile_collector: object | None = None,
		quality_benchmark_factory: Callable[[], object] | None = None,
		performance_benchmark_factory: Callable[[], object] | None = None,
	) -> EvalRunner:
		return cls(
			config,
			cwd=cwd,
			profile_collector=profile_collector,
			quality_benchmark_factory=quality_benchmark_factory,
			performance_benchmark_factory=performance_benchmark_factory,
		)

	def run(
		self,
		cases_file: Path,
		*,
		gate_id: str,
		suite_kind: EvalSuiteKind | None = None,
	) -> EvalRunReport:
		"""Load, execute, and summarize one eval suite."""

		started_at = _utc_now()
		suite = load_eval_suite(cases_file)
		resolved_suite_kind = suite_kind or _infer_suite_kind(gate_id)
		if resolved_suite_kind == "quality":
			return self._run_quality_suite(
				suite,
				cases_file=cases_file,
				gate_id=gate_id,
				started_at=started_at,
			)
		if resolved_suite_kind == "performance":
			return self._run_performance_suite(
				suite,
				cases_file=cases_file,
				gate_id=gate_id,
				started_at=started_at,
			)
		return self._run_router_suite(
			suite,
			cases_file=cases_file,
			gate_id=gate_id,
			started_at=started_at,
		)

	def _run_router_suite(
		self,
		suite: EvalCaseSuite,
		*,
		cases_file: Path,
		gate_id: str,
		started_at: str,
	) -> EvalRunReport:
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

	def _run_quality_suite(
		self,
		suite: EvalCaseSuite,
		*,
		cases_file: Path,
		gate_id: str,
		started_at: str,
	) -> EvalRunReport:
		benchmark = self._quality_benchmark_factory()
		try:
			case_results = tuple(
				self._execute_quality_case(case, benchmark=benchmark)
				for case in suite.cases
			)
			failed_results = tuple(result for result in case_results if result.status == "failed")
			observations = tuple(
				QualityCaseObservation(
					case_id=result.case_id,
					category=self._case_category(suite, result.case_id),
					result_status=result.observed_result_status or "unknown",
					schema_compliant=bool(result.schema_compliant),
					warning_quality_ok=bool(result.warning_quality_ok),
					profile_correct=result.profile_correct,
					evidence_hit=result.evidence_hit,
					top_5_hit=None
					if result.first_relevant_rank is None
					else result.first_relevant_rank <= 5,
					symbol_top_3_hit=None
					if self._case_category(suite, result.case_id) != "symbol_lookup"
					or result.first_relevant_rank is None
					else result.first_relevant_rank <= 3,
					reciprocal_rank=None
					if result.first_relevant_rank is None
					else round(1.0 / float(result.first_relevant_rank), 6),
				)
				for result in case_results
			)
			blocked_probe = benchmark.blocked_security_probe()
			blocked_contract_ok = self._blocked_contract_ok(blocked_probe)
			metrics = build_suite_metrics(
				suite,
				executed_cases=len(case_results),
				passed_cases=len(case_results) - len(failed_results),
				failed_cases=len(failed_results),
			)
			quality_metrics = build_quality_gate_metrics(
				observations,
				blocked_security_contract_ok=blocked_contract_ok,
			)
			metrics["quality_gate"] = quality_metrics
			metrics["blocked_security_probe"] = {
				"result_status": blocked_probe.result_status,
				"warning_codes": [warning.code for warning in blocked_probe.warnings],
				"ok": blocked_contract_ok,
			}
			warnings: list[dict[str, Any]] = []
			if not quality_metrics["passed"]:
				failed_checks = [item for item in quality_metrics["checks"] if not item["passed"]]
				warnings.append(
					{
						"code": "eval.quality_gate_failed",
						"message": "One or more E7-02 quality blocker thresholds were not met.",
						"details": {"failed_checks": failed_checks},
					}
				)
			status: EvalRunStatus = "pass"
			if failed_results or not quality_metrics["passed"]:
				status = "fail"
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
						"observed_result_status": result.observed_result_status,
						"warning_codes": list(result.warning_codes),
					}
					for result in failed_results
				),
				warnings=tuple(warnings),
			)
		finally:
			close = getattr(benchmark, "close", None)
			if callable(close):
				close()

	def _run_performance_suite(
		self,
		suite: EvalCaseSuite,
		*,
		cases_file: Path,
		gate_id: str,
		started_at: str,
	) -> EvalRunReport:
		benchmark = self._performance_benchmark_factory()
		try:
			observations = tuple(
				item
				for item in benchmark.measure_suite(suite)
				if isinstance(item, PerformanceProbeObservation)
			)
			metrics = build_suite_metrics(
				suite,
				executed_cases=len(suite.cases),
				passed_cases=len(suite.cases),
				failed_cases=0,
			)
			performance_metrics = build_performance_gate_metrics(
				observations,
				environment=dict(benchmark.environment()),
				dataset_scale=dict(benchmark.dataset_scale()),
			)
			metrics["performance_gate"] = performance_metrics
			warnings: list[dict[str, Any]] = []
			if not performance_metrics["passed"]:
				failed_checks = [
					item for item in performance_metrics["checks"] if not item["passed"]
				]
				warnings.append(
					{
						"code": "eval.performance_gate_failed",
						"message": "One or more E7-03 performance blocker thresholds were not met.",
						"details": {"failed_checks": failed_checks},
					}
				)
			return EvalRunReport(
				gate_id=gate_id,
				suite_id=suite.suite_id,
				status="pass" if performance_metrics["passed"] else "fail",
				started_at=started_at,
				finished_at=_utc_now(),
				cases_file=str(cases_file),
				metrics=metrics,
				warnings=tuple(warnings),
			)
		finally:
			close = getattr(benchmark, "close", None)
			if callable(close):
				close()

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

	def _execute_quality_case(self, case: EvalCase, *, benchmark: object) -> EvalCaseResult:
		if case.execution_mode != "query_quality":
			return EvalCaseResult(
				case_id=case.case_id,
				status="failed",
				observed_intent="unsupported_execution_mode",
				observed_primary_tool="unsupported_execution_mode",
				observed_route_mode=case.execution_mode,
				failures=(f"unsupported execution_mode: {case.execution_mode}",),
			)

		request = QueryRequest.model_validate(case.request.to_dict())
		decision = benchmark.route(request)
		result = benchmark.search(case)
		warning_codes = tuple(warning.code for warning in result.warnings)
		failures: list[str] = []
		failures.extend(self._route_failures(case, decision, warning_codes))
		if case.expected_result_status is not None and result.result_status != case.expected_result_status:
			failures.append(
				f"result_status mismatch: expected {case.expected_result_status}, got {result.result_status}"
			)
		if result.tool_name != case.expected_route.primary_tool:
			failures.append(
				f"tool_name mismatch: expected {case.expected_route.primary_tool}, got {result.tool_name}"
			)
		if result.query_intent != case.expected_route.intent:
			failures.append(
				f"query_intent mismatch: expected {case.expected_route.intent}, got {result.query_intent}"
			)
		schema_compliant = self._schema_compliant(result)
		if not schema_compliant:
			failures.append("query result failed schema round-trip validation")
		first_rank = self._first_relevant_rank(case, result)
		evidence_hit = self._has_expected_evidence(case, result, first_rank=first_rank)
		if case.expected_result_status not in {"zero_result", "blocked"} and not evidence_hit:
			failures.append("expected evidence was not recovered from ranked items or evidence refs")
		observed_profile_status = str(decision.profile_resolution.get("status"))
		profile_correct = observed_profile_status == case.profile_requirement.expected_status
		if not profile_correct:
			failures.append(
				"profile status mismatch: "
				f"expected {case.profile_requirement.expected_status}, got {observed_profile_status}"
			)
		warning_quality_ok = self._warning_quality_ok(case, warning_codes)
		if not warning_quality_ok:
			failures.append(
				"warning quality mismatch: "
				f"expected required={list(case.expected_route.required_warning_codes)}, "
				f"allowed={list(case.expected_route.allowed_warning_codes)}, got {list(warning_codes)}"
			)
		return EvalCaseResult(
			case_id=case.case_id,
			status="failed" if failures else "passed",
			observed_intent=decision.intent,
			observed_primary_tool=result.tool_name,
			observed_route_mode=decision.tool_plan.route_mode,
			observed_result_status=result.result_status,
			warning_codes=warning_codes,
			schema_compliant=schema_compliant,
			evidence_hit=evidence_hit,
			first_relevant_rank=first_rank,
			profile_correct=profile_correct,
			warning_quality_ok=warning_quality_ok,
			failures=tuple(failures),
		)

	def _route_failures(
		self,
		case: EvalCase,
		decision: Any,
		warning_codes: tuple[str, ...],
	) -> list[str]:
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
			failures.append(f"unexpected warnings: {sorted(unexpected_warning_codes)}")
		return failures

	def _schema_compliant(self, result: QueryResult) -> bool:
		try:
			QueryResult.model_validate(result.to_dict())
		except Exception:
			return False
		return True

	def _first_relevant_rank(self, case: EvalCase, result: QueryResult) -> int | None:
		for index, item in enumerate(result.items, start=1):
			if any(self._item_matches_expected(item, expected) for expected in case.expected_evidence):
				return index
		return None

	def _has_expected_evidence(
		self,
		case: EvalCase,
		result: QueryResult,
		*,
		first_rank: int | None,
	) -> bool:
		if first_rank is not None:
			return True
		return any(
			self._evidence_ref_matches_expected(evidence_ref.to_dict(), expected)
			for expected in case.expected_evidence
			for evidence_ref in result.evidence_refs
		)

	def _item_matches_expected(self, item: dict[str, object], expected: Any) -> bool:
		needle = expected.locator.strip().lower()
		if not needle:
			return False
		return any(needle in candidate for candidate in self._collect_strings(item))

	def _evidence_ref_matches_expected(self, payload: dict[str, object], expected: Any) -> bool:
		needle = expected.locator.strip().lower()
		if not needle:
			return False
		return any(needle in candidate for candidate in self._collect_strings(payload))

	def _collect_strings(self, payload: object) -> tuple[str, ...]:
		strings: list[str] = []
		self._collect_strings_into(payload, strings)
		return tuple(strings)

	def _collect_strings_into(self, payload: object, sink: list[str]) -> None:
		if isinstance(payload, str):
			normalized = payload.strip().lower().replace("\\", "/")
			if normalized:
				sink.append(normalized)
			return
		if isinstance(payload, dict):
			for value in payload.values():
				self._collect_strings_into(value, sink)
			return
		if isinstance(payload, (list, tuple, set)):
			for value in payload:
				self._collect_strings_into(value, sink)

	def _warning_quality_ok(self, case: EvalCase, warning_codes: tuple[str, ...]) -> bool:
		required = set(case.expected_route.required_warning_codes)
		observed = set(warning_codes)
		allowed = required | set(case.expected_route.allowed_warning_codes)
		return required.issubset(observed) and observed.issubset(allowed)

	def _blocked_contract_ok(self, result: QueryResult) -> bool:
		return (
			result.result_status == "blocked"
			and bool(result.warnings)
			and all(warning.level == "blocked" for warning in result.warnings)
			and any(warning.code.startswith("security.") for warning in result.warnings)
			and self._schema_compliant(result)
		)

	def _case_category(self, suite: EvalCaseSuite, case_id: str) -> str:
		for case in suite.cases:
			if case.case_id == case_id:
				return case.category
		raise KeyError(case_id)


def _utc_now() -> str:
	return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _infer_suite_kind(gate_id: str) -> EvalSuiteKind:
	if gate_id == "quality":
		return "quality"
	if gate_id == "performance":
		return "performance"
	return "router"
