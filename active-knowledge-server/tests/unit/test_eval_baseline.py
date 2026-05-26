from __future__ import annotations

from pathlib import Path

from active_knowledge_server.eval.baseline import (
    compare_against_baseline,
    create_baseline_snapshot,
    load_baseline_snapshot,
    save_baseline_snapshot,
)
from active_knowledge_server.eval.metrics import PERFORMANCE_GATE_THRESHOLDS
from active_knowledge_server.eval.runner import EvalRunReport


def test_eval_baseline_snapshot_round_trips_to_disk(tmp_path: Path) -> None:
    snapshot = create_baseline_snapshot(
        baseline_id="release-20260522",
        quality_report=_quality_report(),
        performance_report=_performance_report(),
    )
    output_path = tmp_path / "eval-baseline" / "release-20260522.json"
    latest_path = tmp_path / "eval-baseline" / "latest.json"

    save_baseline_snapshot(snapshot, output_path=output_path, latest_path=latest_path)
    loaded = load_baseline_snapshot(output_path)

    assert loaded.baseline_id == "release-20260522"
    assert latest_path.exists()
    assert loaded.quality_report.metrics["quality_gate"]["metrics"]["evidence_hit_rate"] == 0.9


def test_compare_against_baseline_passes_when_metrics_are_stable(tmp_path: Path) -> None:
    baseline = create_baseline_snapshot(
        baseline_id="release-20260522",
        quality_report=_quality_report(),
        performance_report=_performance_report(),
    )

    report = compare_against_baseline(
        baseline=baseline,
        baseline_path=tmp_path / "latest.json",
        current_quality_report=_quality_report(),
        current_performance_report=_performance_report(),
    )

    assert report.status == "pass"
    assert report.failures == ()
    assert report.warnings == ()


def test_compare_against_baseline_fails_when_quality_regresses_beyond_gate(tmp_path: Path) -> None:
    baseline = create_baseline_snapshot(
        baseline_id="release-20260522",
        quality_report=_quality_report(),
        performance_report=_performance_report(),
    )
    current_quality = _quality_report(
        evidence_hit_rate=0.87,
        schema_compliance=0.99,
        category_rates={"widget_usage": 0.0},
    )

    report = compare_against_baseline(
        baseline=baseline,
        baseline_path=tmp_path / "latest.json",
        current_quality_report=current_quality,
        current_performance_report=_performance_report(),
    )

    assert report.status == "fail"
    assert any(item["check"] == "quality_metric_regression" for item in report.failures)
    assert any(item["check"] == "security_contract" for item in report.failures)
    assert any(item["check"] == "category_evidence_regression" for item in report.failures)


def test_compare_against_baseline_fails_for_unexempted_perf_p95_regression(
    tmp_path: Path,
) -> None:
    baseline = create_baseline_snapshot(
        baseline_id="release-20260522",
        quality_report=_quality_report(),
        performance_report=_performance_report(),
    )
    current_performance = _performance_report(
        p95_overrides={"kb_search": 2.0},
    )

    report = compare_against_baseline(
        baseline=baseline,
        baseline_path=tmp_path / "latest.json",
        current_quality_report=_quality_report(),
        current_performance_report=current_performance,
    )

    assert report.status == "fail"
    assert report.warnings == ()
    assert any(item["check"] == "performance_regression" for item in report.failures)


def test_compare_against_baseline_returns_partial_ready_for_exempted_perf_p95_regression(
    tmp_path: Path,
) -> None:
    baseline = create_baseline_snapshot(
        baseline_id="release-20260522",
        quality_report=_quality_report(),
        performance_report=_performance_report(),
    )
    current_performance = _performance_report(
        p95_overrides={"kb_search": 2.0},
    )

    report = compare_against_baseline(
        baseline=baseline,
        baseline_path=tmp_path / "latest.json",
        current_quality_report=_quality_report(),
        current_performance_report=current_performance,
        performance_exemptions={"kb_search": "accepted for large workspace release"},
    )

    assert report.status == "partial_ready"
    assert report.failures == ()
    assert report.warnings
    assert report.warnings[0]["check"] == "performance_regression_exempted"
    assert report.warnings[0]["exemption_reason"] == "accepted for large workspace release"


def _quality_report(
    *,
    evidence_hit_rate: float = 0.90,
    top_5_recall: float = 0.93,
    symbol_top_3_recall: float = 0.97,
    mrr: float = 0.80,
    profile_correctness: float = 0.94,
    warning_quality: float = 0.90,
    schema_compliance: float = 1.0,
    blocked_security_contract: float = 1.0,
    category_rates: dict[str, float] | None = None,
) -> EvalRunReport:
    return EvalRunReport(
        gate_id="quality",
        suite_id="quality-benchmark-v1",
        status="pass",
        started_at="2026-05-22T00:00:00Z",
        finished_at="2026-05-22T00:00:01Z",
        cases_file="eval/quality_cases.yaml",
        metrics={
            "quality_gate": {
                "metrics": {
                    "schema_compliance": schema_compliance,
                    "evidence_hit_rate": evidence_hit_rate,
                    "top_5_recall": top_5_recall,
                    "symbol_top_3_recall": symbol_top_3_recall,
                    "mrr": mrr,
                    "profile_correctness": profile_correctness,
                    "warning_quality": warning_quality,
                    "blocked_security_contract": blocked_security_contract,
                },
                "case_observations": _category_case_observations(category_rates or {}),
            }
        },
    )


def _performance_report(
    *,
    p95_overrides: dict[str, float] | None = None,
) -> EvalRunReport:
    metrics = {}
    for probe_id, (unit, threshold) in PERFORMANCE_GATE_THRESHOLDS.items():
        p95 = (p95_overrides or {}).get(probe_id, threshold / 2.0)
        metrics[probe_id] = {
            "unit": unit,
            "p50": p95 / 2.0,
            "p95": p95,
            "mean": p95 / 2.0,
            "max": p95,
        }
    return EvalRunReport(
        gate_id="performance",
        suite_id="performance-benchmark-v1",
        status="pass",
        started_at="2026-05-22T00:00:00Z",
        finished_at="2026-05-22T00:00:01Z",
        cases_file="eval/performance_cases.yaml",
        metrics={"performance_gate": {"metrics": metrics}},
    )


def _category_case_observations(category_rates: dict[str, float]) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for category in (
        "symbol_lookup",
        "api_documentation",
        "widget_usage",
        "workspace_navigation",
        "profile_impact",
        "feature_domain_cross_layer",
    ):
        rate = category_rates.get(category, 1.0)
        observations.append(
            {
                "case_id": f"{category}:1",
                "category": category,
                "result_status": "ok",
                "schema_compliant": True,
                "warning_quality_ok": True,
                "profile_correct": True,
                "evidence_hit": rate >= 0.5,
                "top_5_hit": True,
                "symbol_top_3_hit": True,
                "reciprocal_rank": 1.0,
            }
        )
    return observations
