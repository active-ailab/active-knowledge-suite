from __future__ import annotations

import pytest
from pydantic import ValidationError

from active_knowledge_server.models import Candidate, QueryRequest, QueryResult, Warning


def caution_warning(code: str) -> Warning:
    return Warning(
        level="caution",
        code=code,
        message=f"warning for {code}",
        actionable=True,
        suggested_action="Refine the query.",
    )


def blocked_warning(code: str) -> Warning:
    return Warning(
        level="blocked",
        code=code,
        message=f"blocked for {code}",
        actionable=True,
        suggested_action="Fix the blocking configuration.",
    )


def test_query_request_keeps_raw_query_and_exposes_normalized_query() -> None:
    request = QueryRequest(query="  find   CONFIG_BT\n in modules  ")

    assert request.query == "  find   CONFIG_BT\n in modules  "
    assert request.normalized_query == "find CONFIG_BT in modules"


def test_candidate_requires_locator_field() -> None:
    with pytest.raises(ValidationError):
        Candidate(
            disambiguation_key="service:init_service",
            entity_type="function",
            match_reason="exact symbol",
            score=0.88,
        )


def test_zero_result_requires_empty_payload_and_follow_up_hint() -> None:
    with pytest.raises(ValidationError):
        QueryResult(
            tool_name="code_resolve",
            result_status="zero_result",
            confidence=0.0,
            query_intent="code_exact",
            snapshot_id="current",
            profile_id="not_required",
            summary="No candidate was found in the current index.",
            items=({"entity_id": "sym:init_service"},),
            warnings=(caution_warning("retrieval.zero_result"),),
        )


def test_blocked_builder_populates_shared_contract_fields() -> None:
    payload = QueryResult.blocked(
        tool_name="serve",
        summary="Startup was blocked by fail-safe security configuration.",
        warnings=(blocked_warning("security.auth_required"),),
        next_queries=("Fix the blocked security configuration and retry.",),
        diagnostics={"blocked_reason": "security_config"},
    ).to_dict()

    assert payload["schema_version"] == "query_result.v1"
    assert payload["result_status"] == "blocked"
    assert payload["confidence"] == 0.0
    assert payload["confidence_band"] == "low"
    assert payload["items"] == []
    assert payload["warnings"][0]["level"] == "blocked"