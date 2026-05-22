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
