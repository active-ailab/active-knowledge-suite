"""Baseline snapshot and regression comparison for E7-05."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from active_knowledge_server.eval.metrics import (
    CATEGORY_MINIMUMS,
    PERFORMANCE_GATE_THRESHOLDS,
    QUALITY_GATE_THRESHOLDS,
)
from active_knowledge_server.eval.runner import EvalRunReport, EvalRunStatus

RegressionRunStatus = Literal["pass", "partial_ready", "fail"]

_QUALITY_REGRESSION_METRICS = (
    "evidence_hit_rate",
    "top_5_recall",
    "symbol_top_3_recall",
    "profile_correctness",
    "warning_quality",
)
_SECURITY_CONTRACT_METRICS = (
    "schema_compliance",
    "blocked_security_contract",
)
_QUALITY_MAX_DROP = 0.02
_CATEGORY_EVIDENCE_MAX_DROP = 0.05
_MRR_MAX_DROP = 0.03
_PERFORMANCE_WARNING_RATIO = 1.20


class EvalBaselineSnapshot(BaseModel):
    """Saved release baseline snapshot for later regression comparison."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["eval_baseline.v1"] = "eval_baseline.v1"
    baseline_id: str = Field(min_length=1)
    created_at: str
    quality_report: EvalRunReport
    performance_report: EvalRunReport
    stability_report: EvalRunReport | None = None
    source_artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class RegressionGateReport(BaseModel):
    """Machine-readable E7-05 regression comparison report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["eval_regression.v1"] = "eval_regression.v1"
    baseline_id: str = Field(min_length=1)
    status: RegressionRunStatus
    started_at: str
    finished_at: str
    baseline_path: str = Field(min_length=1)
    metrics: dict[str, Any] = Field(default_factory=dict)
    failures: tuple[dict[str, Any], ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def create_baseline_snapshot(
    *,
    baseline_id: str,
    quality_report: EvalRunReport,
    performance_report: EvalRunReport,
    stability_report: EvalRunReport | None = None,
    source_artifacts: tuple[str, ...] = (),
) -> EvalBaselineSnapshot:
    """Create one saved baseline snapshot from gate reports."""

    return EvalBaselineSnapshot(
        baseline_id=baseline_id,
        created_at=_utc_now(),
        quality_report=quality_report,
        performance_report=performance_report,
        stability_report=stability_report,
        source_artifacts=source_artifacts,
    )


def save_baseline_snapshot(
    snapshot: EvalBaselineSnapshot,
    *,
    output_path: Path,
    latest_path: Path | None = None,
) -> EvalBaselineSnapshot:
    """Persist one baseline snapshot and optionally refresh latest.json."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot.to_dict(), indent=2, sort_keys=True)
    output_path.write_text(payload, encoding="utf-8")
    if latest_path is not None:
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        latest_path.write_text(payload, encoding="utf-8")
    return snapshot


def load_baseline_snapshot(path: Path) -> EvalBaselineSnapshot:
    """Load one saved baseline snapshot from disk."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("baseline snapshot must be a top-level mapping")
    return EvalBaselineSnapshot.model_validate(payload)


def load_eval_report_payload(path: Path) -> EvalRunReport:
    """Load a saved eval/perf/stability JSON payload into EvalRunReport."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("eval report must be a top-level mapping")
    report_payload = {
        "schema_version": payload.get("schema_version"),
        "gate_id": payload.get("gate_id"),
        "suite_id": payload.get("suite_id"),
        "status": payload.get("status"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "cases_file": payload.get("cases_file"),
        "metrics": payload.get("metrics", {}),
        "failures": payload.get("failures", ()),
        "warnings": payload.get("warnings", ()),
        "artifacts": payload.get("artifacts", ()),
    }
    return EvalRunReport.model_validate(report_payload)


def compare_against_baseline(
    *,
    baseline: EvalBaselineSnapshot,
    baseline_path: Path,
    current_quality_report: EvalRunReport,
    current_performance_report: EvalRunReport,
    current_stability_report: EvalRunReport | None = None,
    performance_exemptions: Mapping[str, str] | None = None,
) -> RegressionGateReport:
    """Compare current gate results against one saved release baseline."""

    started_at = _utc_now()
    metrics = build_regression_gate_metrics(
        baseline=baseline,
        current_quality_report=current_quality_report,
        current_performance_report=current_performance_report,
        current_stability_report=current_stability_report,
        performance_exemptions=performance_exemptions,
    )
    failures: tuple[dict[str, Any], ...] = tuple(metrics["failures"])
    warnings: tuple[dict[str, Any], ...] = tuple(metrics["warnings"])
    status: RegressionRunStatus = "pass"
    if failures:
        status = "fail"
    elif warnings:
        status = "partial_ready"
    return RegressionGateReport(
        baseline_id=baseline.baseline_id,
        status=status,
        started_at=started_at,
        finished_at=_utc_now(),
        baseline_path=str(baseline_path),
        metrics=metrics["summary"],
        failures=failures,
        warnings=warnings,
    )


def build_regression_gate_metrics(
    *,
    baseline: EvalBaselineSnapshot,
    current_quality_report: EvalRunReport,
    current_performance_report: EvalRunReport,
    current_stability_report: EvalRunReport | None,
    performance_exemptions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the E7-05 regression summary, failures, and warnings."""

    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    resolved_performance_exemptions = {
        probe_id: reason.strip()
        for probe_id, reason in dict(performance_exemptions or {}).items()
        if reason.strip()
    }
    summary: dict[str, Any] = {
        "baseline_quality_status": baseline.quality_report.status,
        "current_quality_status": current_quality_report.status,
        "baseline_performance_status": baseline.performance_report.status,
        "current_performance_status": current_performance_report.status,
        "performance_exemptions": dict(sorted(resolved_performance_exemptions.items())),
    }
    _require_pass_report(
        failures,
        label="baseline_quality",
        report=baseline.quality_report,
        allowed_statuses={"pass"},
    )
    _require_pass_report(
        failures,
        label="current_quality",
        report=current_quality_report,
        allowed_statuses={"pass"},
    )
    _require_pass_report(
        failures,
        label="baseline_performance",
        report=baseline.performance_report,
        allowed_statuses={"pass"},
    )
    _require_pass_report(
        failures,
        label="current_performance",
        report=current_performance_report,
        allowed_statuses={"pass"},
    )
    if current_stability_report is not None:
        summary["current_stability_status"] = current_stability_report.status

    current_quality_gate = _mapping(summary_get(current_quality_report.metrics, "quality_gate"))
    baseline_quality_gate = _mapping(summary_get(baseline.quality_report.metrics, "quality_gate"))
    current_quality_metrics = _mapping(summary_get(current_quality_gate, "metrics"))
    baseline_quality_metrics = _mapping(summary_get(baseline_quality_gate, "metrics"))
    quality_deltas: dict[str, float] = {}
    for metric in _QUALITY_REGRESSION_METRICS:
        current_value = _float_value(current_quality_metrics.get(metric))
        baseline_value = _float_value(baseline_quality_metrics.get(metric))
        if current_value is None or baseline_value is None:
            failures.append(
                {
                    "check": "quality_metric_available",
                    "metric": metric,
                    "message": (
                        "quality regression comparison requires both current and baseline values"
                    ),
                }
            )
            continue
        delta = round(current_value - baseline_value, 6)
        quality_deltas[metric] = delta
        if baseline_value - current_value > _QUALITY_MAX_DROP:
            failures.append(
                {
                    "check": "quality_metric_regression",
                    "metric": metric,
                    "baseline": baseline_value,
                    "current": current_value,
                    "delta": delta,
                    "max_drop": _QUALITY_MAX_DROP,
                }
            )
    for metric in _SECURITY_CONTRACT_METRICS:
        current_value = _float_value(current_quality_metrics.get(metric))
        if current_value != 1.0:
            failures.append(
                {
                    "check": "security_contract",
                    "metric": metric,
                    "current": current_value,
                    "required": 1.0,
                }
            )
    mrr_current = _float_value(current_quality_metrics.get("mrr"))
    mrr_baseline = _float_value(baseline_quality_metrics.get("mrr"))
    if mrr_current is None or mrr_baseline is None:
        failures.append(
            {
                "check": "quality_metric_available",
                "metric": "mrr",
                "message": "MRR regression comparison requires both current and baseline values",
            }
        )
    else:
        mrr_delta = round(mrr_current - mrr_baseline, 6)
        summary["mrr_delta"] = mrr_delta
        if (
            mrr_current < QUALITY_GATE_THRESHOLDS["mrr"]
            or (mrr_baseline - mrr_current) > _MRR_MAX_DROP
        ):
            failures.append(
                {
                    "check": "mrr_regression",
                    "baseline": mrr_baseline,
                    "current": mrr_current,
                    "delta": mrr_delta,
                    "max_drop": _MRR_MAX_DROP,
                    "threshold": QUALITY_GATE_THRESHOLDS["mrr"],
                }
            )

    category_evidence = build_category_evidence_regression(
        current_quality_gate=current_quality_gate,
        baseline_quality_gate=baseline_quality_gate,
    )
    summary["category_evidence_hit_rate"] = category_evidence["rates"]
    failures.extend(category_evidence["failures"])

    performance_comparison = build_performance_regression_summary(
        current_performance_gate=_mapping(
            summary_get(current_performance_report.metrics, "performance_gate")
        ),
        baseline_performance_gate=_mapping(
            summary_get(baseline.performance_report.metrics, "performance_gate")
        ),
        performance_exemptions=resolved_performance_exemptions,
    )
    summary["performance_regression"] = performance_comparison["summary"]
    failures.extend(performance_comparison["failures"])
    warnings.extend(performance_comparison["warnings"])
    summary["quality_metric_deltas"] = quality_deltas
    if baseline.stability_report is not None:
        summary["baseline_stability_status"] = baseline.stability_report.status
    return {
        "summary": summary,
        "failures": failures,
        "warnings": warnings,
    }


def build_category_evidence_regression(
    *,
    current_quality_gate: dict[str, object],
    baseline_quality_gate: dict[str, object],
) -> dict[str, Any]:
    """Compare category-level evidence hit rates against the previous baseline."""

    current_rates = _category_evidence_rates(current_quality_gate)
    baseline_rates = _category_evidence_rates(baseline_quality_gate)
    failures: list[dict[str, Any]] = []
    rates: dict[str, dict[str, float | None]] = {}
    for category in CATEGORY_MINIMUMS:
        current_value = current_rates.get(category)
        baseline_value = baseline_rates.get(category)
        rates[category] = {
            "baseline": baseline_value,
            "current": current_value,
            "delta": None
            if current_value is None or baseline_value is None
            else round(current_value - baseline_value, 6),
        }
        if current_value is None or baseline_value is None:
            failures.append(
                {
                    "check": "category_evidence_available",
                    "category": category,
                    "baseline": baseline_value,
                    "current": current_value,
                }
            )
            continue
        if baseline_value - current_value > _CATEGORY_EVIDENCE_MAX_DROP:
            failures.append(
                {
                    "check": "category_evidence_regression",
                    "category": category,
                    "baseline": baseline_value,
                    "current": current_value,
                    "delta": round(current_value - baseline_value, 6),
                    "max_drop": _CATEGORY_EVIDENCE_MAX_DROP,
                }
            )
    return {"rates": rates, "failures": failures}


def build_performance_regression_summary(
    *,
    current_performance_gate: dict[str, object],
    baseline_performance_gate: dict[str, object],
    performance_exemptions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compare current P95 values with the previous baseline."""

    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    summary: dict[str, dict[str, object]] = {}
    resolved_exemptions = dict(performance_exemptions or {})
    for probe_id, reason in resolved_exemptions.items():
        if probe_id not in PERFORMANCE_GATE_THRESHOLDS:
            failures.append(
                {
                    "check": "performance_exemption_unknown",
                    "probe_id": probe_id,
                    "reason": reason,
                }
            )
    current_metrics = _mapping(summary_get(current_performance_gate, "metrics"))
    baseline_metrics = _mapping(summary_get(baseline_performance_gate, "metrics"))
    for probe_id, (unit, _threshold) in PERFORMANCE_GATE_THRESHOLDS.items():
        current_probe = _mapping(summary_get(current_metrics, probe_id))
        baseline_probe = _mapping(summary_get(baseline_metrics, probe_id))
        current_p95 = _float_value(current_probe.get("p95"))
        baseline_p95 = _float_value(baseline_probe.get("p95"))
        summary[probe_id] = {
            "unit": unit,
            "baseline_p95": baseline_p95,
            "current_p95": current_p95,
            "regression_ratio": None,
        }
        if current_p95 is None or baseline_p95 is None:
            failures.append(
                {
                    "check": "performance_probe_available",
                    "probe_id": probe_id,
                    "unit": unit,
                    "baseline_p95": baseline_p95,
                    "current_p95": current_p95,
                }
            )
            continue
        regression_ratio = 1.0 if baseline_p95 <= 0 else current_p95 / baseline_p95
        summary[probe_id]["regression_ratio"] = round(regression_ratio, 6)
        if regression_ratio > _PERFORMANCE_WARNING_RATIO:
            payload = {
                "probe_id": probe_id,
                "unit": unit,
                "baseline_p95": baseline_p95,
                "current_p95": current_p95,
                "regression_ratio": round(regression_ratio, 6),
                "max_ratio": _PERFORMANCE_WARNING_RATIO,
            }
            if probe_id in resolved_exemptions:
                warnings.append(
                    {
                        "check": "performance_regression_exempted",
                        **payload,
                        "exemption_reason": resolved_exemptions[probe_id],
                    }
                )
            else:
                failures.append(
                    {
                        "check": "performance_regression",
                        **payload,
                    }
                )
    return {"summary": summary, "failures": failures, "warnings": warnings}


def _require_pass_report(
    failures: list[dict[str, Any]],
    *,
    label: str,
    report: EvalRunReport,
    allowed_statuses: set[EvalRunStatus],
) -> None:
    if report.status not in allowed_statuses:
        failures.append(
            {
                "check": "gate_status",
                "gate": label,
                "status": report.status,
                "allowed_statuses": sorted(allowed_statuses),
            }
        )


def _category_evidence_rates(quality_gate: dict[str, object]) -> dict[str, float]:
    observations = summary_get(quality_gate, "case_observations")
    if not isinstance(observations, list):
        return {}
    grouped: dict[str, list[bool]] = {}
    for payload in observations:
        if not isinstance(payload, dict):
            continue
        category = payload.get("category")
        evidence_hit = payload.get("evidence_hit")
        if not isinstance(category, str) or not isinstance(evidence_hit, bool):
            continue
        grouped.setdefault(category, []).append(evidence_hit)
    return {
        category: sum(1 for item in values if item) / len(values)
        for category, values in grouped.items()
        if values
    }


def summary_get(mapping: object, key: str) -> object:
    if not isinstance(mapping, dict):
        return {}
    return mapping.get(key, {})


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _float_value(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
