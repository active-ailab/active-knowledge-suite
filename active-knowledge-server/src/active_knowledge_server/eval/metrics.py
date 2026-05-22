"""Evaluation metrics boundary."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final

from active_knowledge_server.eval.cases import EvalCaseCategory, EvalCaseSuite

CATEGORY_MINIMUMS: Final[dict[EvalCaseCategory, int]] = {
	"symbol_lookup": 10,
	"api_documentation": 10,
	"widget_usage": 10,
	"workspace_navigation": 10,
	"profile_impact": 10,
	"feature_domain_cross_layer": 10,
}

QUALITY_GATE_THRESHOLDS: Final[dict[str, float]] = {
	"schema_compliance": 1.0,
	"evidence_hit_rate": 0.85,
	"top_5_recall": 0.90,
	"symbol_top_3_recall": 0.95,
	"mrr": 0.75,
	"profile_correctness": 0.90,
	"warning_quality": 0.85,
	"blocked_security_contract": 1.0,
}


@dataclass(frozen=True)
class EvalCategoryCoverage:
	"""Coverage status for one eval category."""

	category: EvalCaseCategory
	minimum_required: int
	actual_cases: int

	@property
	def remaining_gap(self) -> int:
		return max(self.minimum_required - self.actual_cases, 0)

	@property
	def ready(self) -> bool:
		return self.remaining_gap == 0

	def to_dict(self) -> dict[str, int | bool | str]:
		return {
			"category": self.category,
			"minimum_required": self.minimum_required,
			"actual_cases": self.actual_cases,
			"remaining_gap": self.remaining_gap,
			"ready": self.ready,
		}


@dataclass(frozen=True)
class QualityCaseObservation:
	"""Per-case quality observation used to derive E7-02 metrics."""

	case_id: str
	category: EvalCaseCategory
	result_status: str
	schema_compliant: bool
	warning_quality_ok: bool
	profile_correct: bool | None
	evidence_hit: bool | None
	top_5_hit: bool | None
	symbol_top_3_hit: bool | None
	reciprocal_rank: float | None

	def to_dict(self) -> dict[str, object]:
		return {
			"case_id": self.case_id,
			"category": self.category,
			"result_status": self.result_status,
			"schema_compliant": self.schema_compliant,
			"warning_quality_ok": self.warning_quality_ok,
			"profile_correct": self.profile_correct,
			"evidence_hit": self.evidence_hit,
			"top_5_hit": self.top_5_hit,
			"symbol_top_3_hit": self.symbol_top_3_hit,
			"reciprocal_rank": self.reciprocal_rank,
		}


@dataclass(frozen=True)
class QualityGateCheck:
	"""One blocker threshold evaluation for the quality gate."""

	metric: str
	actual: float
	threshold: float
	passed: bool
	blocking_level: str = "blocker"

	def to_dict(self) -> dict[str, object]:
		return {
			"metric": self.metric,
			"actual": self.actual,
			"threshold": self.threshold,
			"passed": self.passed,
			"blocking_level": self.blocking_level,
		}


def build_category_coverage(suite: EvalCaseSuite) -> tuple[EvalCategoryCoverage, ...]:
	"""Build category coverage against the Phase 7 minimums."""

	counts = Counter(case.category for case in suite.cases if case.include_in_release_gate)
	return tuple(
		EvalCategoryCoverage(
			category=category,
			minimum_required=minimum_required,
			actual_cases=counts.get(category, 0),
		)
		for category, minimum_required in CATEGORY_MINIMUMS.items()
	)


def build_suite_metrics(
	suite: EvalCaseSuite,
	*,
	executed_cases: int,
	passed_cases: int,
	failed_cases: int,
) -> dict[str, object]:
	"""Return the stable summary metrics emitted by the E7 runner."""

	category_coverage = build_category_coverage(suite)
	intent_counts = Counter(case.expected_route.intent for case in suite.cases)
	priority_counts = Counter(case.priority for case in suite.cases)
	return {
		"total_cases": len(suite.cases),
		"release_gate_cases": sum(1 for case in suite.cases if case.include_in_release_gate),
		"executed_cases": executed_cases,
		"passed_cases": passed_cases,
		"failed_cases": failed_cases,
		"category_coverage": [item.to_dict() for item in category_coverage],
		"intent_coverage": dict(sorted(intent_counts.items(), key=lambda item: item[0])),
		"priority_coverage": dict(sorted(priority_counts.items(), key=lambda item: item[0])),
	}


def build_quality_gate_metrics(
	observations: tuple[QualityCaseObservation, ...],
	*,
	blocked_security_contract_ok: bool,
) -> dict[str, object]:
	"""Return E7-02 quality metrics plus blocker-threshold checks."""

	schema_cases = [item.schema_compliant for item in observations]
	warning_cases = [item.warning_quality_ok for item in observations]
	profile_cases = [item.profile_correct for item in observations if item.profile_correct is not None]
	evidence_cases = [item.evidence_hit for item in observations if item.evidence_hit is not None]
	top_5_cases = [item.top_5_hit for item in observations if item.top_5_hit is not None]
	symbol_top_3_cases = [
		item.symbol_top_3_hit for item in observations if item.symbol_top_3_hit is not None
	]
	reciprocal_ranks = [
		item.reciprocal_rank for item in observations if item.reciprocal_rank is not None
	]
	metric_values = {
		"schema_compliance": _boolean_rate(schema_cases),
		"evidence_hit_rate": _boolean_rate(evidence_cases),
		"top_5_recall": _boolean_rate(top_5_cases),
		"symbol_top_3_recall": _boolean_rate(symbol_top_3_cases),
		"mrr": _mean(reciprocal_ranks),
		"profile_correctness": _boolean_rate(profile_cases),
		"warning_quality": _boolean_rate(warning_cases),
		"blocked_security_contract": 1.0 if blocked_security_contract_ok else 0.0,
	}
	checks = tuple(
		QualityGateCheck(
			metric=metric,
			actual=value,
			threshold=QUALITY_GATE_THRESHOLDS[metric],
			passed=value >= QUALITY_GATE_THRESHOLDS[metric],
		)
		for metric, value in metric_values.items()
	)
	return {
		"metrics": {key: round(value, 6) for key, value in metric_values.items()},
		"thresholds": dict(QUALITY_GATE_THRESHOLDS),
		"checks": [item.to_dict() for item in checks],
		"passed": all(item.passed for item in checks),
		"eligible_case_counts": {
			"schema_compliance": len(schema_cases),
			"evidence_hit_rate": len(evidence_cases),
			"top_5_recall": len(top_5_cases),
			"symbol_top_3_recall": len(symbol_top_3_cases),
			"mrr": len(reciprocal_ranks),
			"profile_correctness": len(profile_cases),
			"warning_quality": len(warning_cases),
			"blocked_security_contract": 1,
		},
		"case_observations": [item.to_dict() for item in observations],
	}


def _boolean_rate(values: list[bool | None]) -> float:
	eligible = [value for value in values if value is not None]
	if not eligible:
		return 0.0
	return sum(1 for value in eligible if value) / len(eligible)


def _mean(values: list[float | None]) -> float:
	eligible = [value for value in values if value is not None]
	if not eligible:
		return 0.0
	return sum(eligible) / len(eligible)
