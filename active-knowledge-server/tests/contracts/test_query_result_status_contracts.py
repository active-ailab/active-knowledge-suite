from __future__ import annotations

import pytest

from active_knowledge_server.models import (
	Candidate,
	EvidenceRef,
	QueryResult,
	SuggestedFilter,
	Warning,
)


def caution_warning(
	code: str,
	*,
	message: str,
	details: dict[str, object] | None = None,
	suggested_action: str = "Refine the query and retry.",
) -> Warning:
	return Warning(
		level="caution",
		code=code,
		message=message,
		details={} if details is None else dict(details),
		actionable=True,
		suggested_action=suggested_action,
	)


def degraded_warning(
	code: str,
	*,
	message: str,
	details: dict[str, object],
	suggested_action: str,
) -> Warning:
	return Warning(
		level="degraded",
		code=code,
		message=message,
		details=dict(details),
		actionable=True,
		suggested_action=suggested_action,
	)


def blocked_warning(code: str, *, message: str, details: dict[str, object]) -> Warning:
	return Warning(
		level="blocked",
		code=code,
		message=message,
		details=dict(details),
		actionable=True,
		suggested_action="Fix the blocking condition and retry.",
	)


def sample_evidence() -> EvidenceRef:
	return EvidenceRef(
		evidence_id="evidence:doc:sensor-open",
		type="doc",
		path="knowledge-sources/api/sensor.md",
		start_line=12,
		end_line=24,
		authority_level="source_doc",
		excerpt="sensor_open API quick reference",
		content_hash="hash-sensor-open",
		source_index="baseline",
	)


def zero_result_fixture() -> QueryResult:
	return QueryResult(
		tool_name="kb_search",
		result_status="zero_result",
		confidence=0.0,
		query_intent="unknown",
		snapshot_id="current",
		profile_id="not_required",
		summary="No evidence-bearing candidates were found for the current hybrid query.",
		warnings=(
			caution_warning(
				"retrieval.zero_result",
				message="No indexed evidence matched the current retrieval scope.",
				details={"intent": "unknown"},
				suggested_action="Add a module, path, symbol, doc_type, or profile_id and retry.",
			),
		),
		next_queries=(
			"sensor_open 所在模块是什么？",
			"sensor_open 请限定 doc_type=api 后重试。",
		),
		suggested_filters=(SuggestedFilter(field="view", value="evidence"),),
		diagnostics={"route": {"primary_tool": "kb_search"}},
	)


def multi_result_fixture() -> QueryResult:
	return QueryResult(
		tool_name="code_resolve",
		result_status="multi_result",
		confidence=0.46,
		query_intent="code_exact",
		snapshot_id="current",
		profile_id="not_required",
		summary="Multiple symbol candidates matched the requested name.",
		candidates=(
			Candidate(
				disambiguation_key="Function|src/sensor/open.c",
				entity_type="function",
				path="src/sensor/open.c",
				match_reason="exact symbol in workspace code",
				score=0.93,
			),
			Candidate(
				disambiguation_key="Function|src/sensorhub/open.c",
				entity_type="function",
				path="src/sensorhub/open.c",
				module="sensorhub",
				match_reason="exact symbol in sibling module",
				score=0.89,
			),
		),
		warnings=(
			caution_warning(
				"retrieval.multi_result",
				message="Multiple symbol candidates need disambiguation.",
				details={"candidate_count": 2},
				suggested_action="Specify a module, path, or profile_id to disambiguate.",
			),
		),
	)


def ambiguous_fixture() -> QueryResult:
	return QueryResult(
		tool_name="config_impact",
		result_status="ambiguous",
		confidence=0.44,
		query_intent="profile_diff",
		snapshot_id="current",
		profile_id="unresolved",
		summary="Profile-aware analysis requires an explicit compare_to context.",
		warnings=(
			caution_warning(
				"profile.invalid",
				message="The compare_to profile is not indexed for the current snapshot.",
				details={"compare_to": "missing_profile", "snapshot_id": "current"},
				suggested_action="Specify a valid compare_to profile_id and retry.",
			),
		),
		next_queries=("CONFIG_HEALTH_BT compare_to=mhs003_sensorhub",),
		suggested_filters=(SuggestedFilter(field="compare_to", value="mhs003_sensorhub"),),
		diagnostics={
			"required_context": ["compare_to"],
			"profile_resolution": {"status": "resolved", "resolved_profile_id": "mhs003_watch"},
		},
	)


def low_confidence_fixture() -> QueryResult:
	return QueryResult(
		tool_name="kb_search",
		result_status="low_confidence",
		confidence=0.41,
		query_intent="unknown",
		snapshot_id="current",
		profile_id="not_required",
		summary="Low-confidence candidate leads were found; verify the attached evidence before relying on them.",
		items=(
			{
				"candidate_id": "doc:sensor-open",
				"object_type": "chunk",
				"title": "sensor_open",
			},
		),
		evidence_refs=(sample_evidence(),),
		warnings=(
			caution_warning(
				"retrieval.low_confidence",
				message="Ranking confidence remained below the shared contract threshold.",
				details={"router_confidence": 0.41},
				suggested_action="Add a module, symbol, doc_type, or profile_id and retry.",
			),
		),
	)


def partial_ready_fixture() -> QueryResult:
	index_status = {
		"ready_sources": ["knowledge-sources/api"],
		"missing_sources": ["knowledge-sources/widgets"],
		"failed_jobs": ["job-17"],
		"degradation_chain": ["skip_widgets", "fts_only"],
	}
	return QueryResult(
		tool_name="docs_search",
		result_status="partial_ready",
		confidence=0.68,
		query_intent="api_lookup",
		snapshot_id="current",
		profile_id="watch",
		summary="Partial index availability returned limited results; missing sources may hide additional evidence.",
		items=(
			{
				"candidate_id": "doc:sensor-open",
				"object_type": "chunk",
				"title": "sensor_open",
			},
		),
		warnings=(
			degraded_warning(
				"index.partial_ready",
				message="Widget and profile indexes are not ready yet.",
				details=index_status,
				suggested_action="Repair the failed jobs and rebuild the missing sources.",
			),
		),
		diagnostics={"index_status": index_status},
	)


def blocked_fixture() -> QueryResult:
	return QueryResult.blocked(
		tool_name="code_context",
		summary="Path guard blocked access to the requested file.",
		warnings=(
			blocked_warning(
				"security.path_blocked",
				message="The requested path escapes the configured allowlist.",
				details={"path": "../../secrets.txt", "reason": "allowlist_escape"},
			),
		),
		next_queries=("Inspect an allowlisted path and retry.",),
		diagnostics={"blocked_reason": "path_guard"},
		query_intent="workspace_nav",
	)


@pytest.mark.parametrize(
	("status_name", "builder"),
	[
		("zero_result", zero_result_fixture),
		("multi_result", multi_result_fixture),
		("ambiguous", ambiguous_fixture),
		("low_confidence", low_confidence_fixture),
		("partial_ready", partial_ready_fixture),
		("blocked", blocked_fixture),
	],
)
def test_query_result_status_fixtures_round_trip(
	status_name: str,
	builder: callable,
) -> None:
	result = builder()
	payload = result.to_dict()
	reparsed = QueryResult.model_validate(payload)

	assert reparsed.result_status == status_name
	assert reparsed.schema_version == "query_result.v1"
	assert reparsed.warnings

	if status_name == "zero_result":
		assert reparsed.items == ()
		assert reparsed.next_queries
		assert reparsed.suggested_filters
	elif status_name == "multi_result":
		assert len(reparsed.candidates) == 2
	elif status_name == "ambiguous":
		assert reparsed.diagnostics["required_context"] == ["compare_to"]
		assert reparsed.next_queries
	elif status_name == "low_confidence":
		assert reparsed.confidence_band == "low"
		assert reparsed.evidence_refs
	elif status_name == "partial_ready":
		assert reparsed.diagnostics["index_status"]["missing_sources"] == [
			"knowledge-sources/widgets"
		]
	elif status_name == "blocked":
		assert reparsed.diagnostics["blocked_reason"] == "path_guard"
		assert all(warning.level == "blocked" for warning in reparsed.warnings)