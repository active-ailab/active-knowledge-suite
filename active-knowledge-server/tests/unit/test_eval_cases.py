from __future__ import annotations

from pathlib import Path

from active_knowledge_server.eval import load_eval_suite

CASES_FILE = Path(__file__).resolve().parents[2] / "eval" / "cases.yaml"


def test_load_eval_suite_parses_seed_cases_file() -> None:
    suite = load_eval_suite(CASES_FILE)

    assert suite.schema_version == "eval_cases.v1"
    assert suite.suite_id == "v1-routing-v1"
    assert len(suite.cases) >= 62
    assert "tests/fixtures/skill_routing_examples.yaml" in suite.generated_from
    assert "tests/fixtures/query_intents.yaml" in suite.generated_from


def test_seed_eval_suite_has_warning_and_profile_cases() -> None:
    suite = load_eval_suite(CASES_FILE)
    counts = suite.category_counts(release_gate_only=True)

    assert counts == {
        "api_documentation": 10,
        "feature_domain_cross_layer": 10,
        "profile_impact": 10,
        "symbol_lookup": 10,
        "widget_usage": 10,
        "workspace_navigation": 10,
    }
    assert sum(1 for case in suite.cases if case.include_in_release_gate) == 60
    assert any(case.expected_route.required_warning_codes for case in suite.cases)
    assert all(case.profile_requirement.expected_status for case in suite.cases)
    assert all(case.source_refs for case in suite.cases)