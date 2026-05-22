"""Evaluation metrics boundary."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import ceil
from typing import Final, Literal

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

PERFORMANCE_GATE_THRESHOLDS: Final[dict[str, tuple[Literal["seconds", "bytes"], float]]] = {
	"serve_startup": ("seconds", 10.0),
	"init_reuse_baseline": ("seconds", 60.0),
	"docs_search": ("seconds", 2.0),
	"code_resolve": ("seconds", 1.5),
	"workspace_view": ("seconds", 2.0),
	"kb_search": ("seconds", 3.0),
	"evidence_bundle": ("seconds", 3.0),
	"incremental_index_100_files": ("seconds", 600.0),
	"serve_resident_memory": ("bytes", float(4 * 1024 * 1024 * 1024)),
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


PerformanceMetricUnit = Literal["seconds", "bytes"]


@dataclass(frozen=True)
class PerformanceProbeObservation:
	"""Observed latency or memory samples for one E7-03 probe."""

	probe_id: str
	display_name: str
	unit: PerformanceMetricUnit
	samples: tuple[float, ...]
	p50: float
	p95: float
	mean: float
	max_value: float
	metadata: dict[str, object]

	@classmethod
	def from_samples(
		cls,
		*,
		probe_id: str,
		display_name: str,
		unit: PerformanceMetricUnit,
		samples: tuple[float, ...],
		metadata: dict[str, object] | None = None,
	) -> PerformanceProbeObservation:
		if not samples:
			raise ValueError("performance probes must record at least one sample")
		normalized = tuple(float(sample) for sample in samples)
		sorted_samples = tuple(sorted(normalized))
		return cls(
			probe_id=probe_id,
			display_name=display_name,
			unit=unit,
			samples=normalized,
			p50=_percentile(sorted_samples, 0.50),
			p95=_percentile(sorted_samples, 0.95),
			mean=_mean(list(sorted_samples)),
			max_value=sorted_samples[-1],
			metadata=dict(metadata or {}),
		)

	@property
	def sample_size(self) -> int:
		return len(self.samples)

	def to_dict(self) -> dict[str, object]:
		return {
			"probe_id": self.probe_id,
			"display_name": self.display_name,
			"unit": self.unit,
			"sample_size": self.sample_size,
			"samples": [_normalize_metric_value(sample, self.unit) for sample in self.samples],
			"p50": _normalize_metric_value(self.p50, self.unit),
			"p95": _normalize_metric_value(self.p95, self.unit),
			"mean": _normalize_metric_value(self.mean, self.unit),
			"max": _normalize_metric_value(self.max_value, self.unit),
			"metadata": self.metadata,
		}


@dataclass(frozen=True)
class PerformanceGateCheck:
	"""One blocker threshold evaluation for the performance gate."""

	metric: str
	unit: PerformanceMetricUnit
	actual_p95: float | None
	threshold_p95: float
	passed: bool
	blocking_level: str = "blocker"

	def to_dict(self) -> dict[str, object]:
		return {
			"metric": self.metric,
			"unit": self.unit,
			"actual_p95": None
			if self.actual_p95 is None
			else _normalize_metric_value(self.actual_p95, self.unit),
			"threshold_p95": _normalize_metric_value(self.threshold_p95, self.unit),
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


def build_performance_gate_metrics(
	observations: tuple[PerformanceProbeObservation, ...],
	*,
	environment: dict[str, object],
	dataset_scale: dict[str, object],
) -> dict[str, object]:
	"""Return E7-03 performance metrics plus blocker-threshold checks."""

	observation_map = {item.probe_id: item for item in observations}
	checks = tuple(
		PerformanceGateCheck(
			metric=probe_id,
			unit=unit,
			actual_p95=None if observation_map.get(probe_id) is None else observation_map[probe_id].p95,
			threshold_p95=threshold,
			passed=(
				probe_id in observation_map
				and observation_map[probe_id].unit == unit
				and observation_map[probe_id].p95 <= threshold
			),
		)
		for probe_id, (unit, threshold) in PERFORMANCE_GATE_THRESHOLDS.items()
	)
	return {
		"environment": dict(environment),
		"dataset_scale": dict(dataset_scale),
		"sample_counts": {
			item.probe_id: item.sample_size for item in observations
		},
		"metrics": {
			item.probe_id: {
				"unit": item.unit,
				"p50": _normalize_metric_value(item.p50, item.unit),
				"p95": _normalize_metric_value(item.p95, item.unit),
				"mean": _normalize_metric_value(item.mean, item.unit),
				"max": _normalize_metric_value(item.max_value, item.unit),
			}
			for item in observations
		},
		"thresholds": {
			probe_id: {
				"unit": unit,
				"p95": _normalize_metric_value(threshold, unit),
			}
			for probe_id, (unit, threshold) in PERFORMANCE_GATE_THRESHOLDS.items()
		},
		"checks": [item.to_dict() for item in checks],
		"passed": all(item.passed for item in checks),
		"probe_observations": [item.to_dict() for item in observations],
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


def _percentile(values: tuple[float, ...], quantile: float) -> float:
	if not values:
		return 0.0
	index = max(0, ceil(len(values) * quantile) - 1)
	return values[index]


def _normalize_metric_value(
	value: float,
	unit: PerformanceMetricUnit,
) -> float | int:
	if unit == "bytes":
		return int(round(value))
	return round(value, 6)
