"""User feedback artifacts for eval and learned-seed drafting."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from active_knowledge_server.eval.cases import (
    EvalBlockingLevel,
    EvalCase,
    EvalCasePriority,
    EvalCaseSuite,
    EvalExpectedEvidence,
    EvalProfileRequirement,
    EvalRouteExpectation,
)
from active_knowledge_server.models.evidence import EvidenceRef
from active_knowledge_server.models.query import QueryIntent, QueryRequest
from active_knowledge_server.models.responses import QueryResult, QueryResultStatus
from active_knowledge_server.models.routing import RouteMode, ToolName

FEEDBACK_RECORD_SCHEMA_VERSION: Final = "feedback_record.v1"
_DEFAULT_FEEDBACK_SOURCE_REF_PREFIX: Final = "feedback-record"
_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")

FeedbackEvidenceVerdict = Literal["useful", "not_useful", "accepted_final"]
FeedbackMissedTargetKind = Literal["path", "symbol", "doc_section"]


class FeedbackEvidenceAnnotation(BaseModel):
    """One evidence-level human judgement."""

    model_config = ConfigDict(extra="forbid")

    verdict: FeedbackEvidenceVerdict
    evidence_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    path: str = Field(min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    authority_level: str = Field(min_length=1)
    excerpt: str | None = None
    source_index: str | None = None
    note: str | None = None

    @classmethod
    def from_evidence_ref(
        cls,
        evidence_ref: EvidenceRef,
        *,
        verdict: FeedbackEvidenceVerdict,
        note: str | None = None,
    ) -> FeedbackEvidenceAnnotation:
        """Build one annotation from a returned evidence ref."""

        return cls(
            verdict=verdict,
            evidence_id=evidence_ref.evidence_id,
            type=evidence_ref.type,
            path=evidence_ref.path,
            start_line=evidence_ref.start_line,
            end_line=evidence_ref.end_line,
            authority_level=evidence_ref.authority_level,
            excerpt=evidence_ref.excerpt,
            source_index=evidence_ref.source_index,
            note=note,
        )


class FeedbackMissedTarget(BaseModel):
    """A target that the user expected but retrieval missed."""

    model_config = ConfigDict(extra="forbid")

    kind: FeedbackMissedTargetKind
    locator: str = Field(min_length=1)
    note: str | None = None


class FeedbackRecord(BaseModel):
    """Persisted human feedback artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["feedback_record.v1"] = FEEDBACK_RECORD_SCHEMA_VERSION
    feedback_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    query: str = Field(min_length=1)
    tool_name: ToolName
    query_intent: QueryIntent
    profile_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    result_status: QueryResultStatus
    result_summary: str = Field(min_length=1)
    request_id: str | None = None
    warning_codes: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    route: dict[str, Any] = Field(default_factory=dict)
    evidence_feedback: tuple[FeedbackEvidenceAnnotation, ...] = ()
    missed_targets: tuple[FeedbackMissedTarget, ...] = ()
    returned_evidence_refs: tuple[EvidenceRef, ...] = ()
    note: str | None = None
    query_result_path: str | None = None

    @model_validator(mode="after")
    def validate_feedback_content(self) -> FeedbackRecord:
        """Require at least one actionable signal in the feedback."""

        if not self.evidence_feedback and not self.missed_targets:
            raise ValueError("feedback must include evidence feedback or missed targets")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""

        return self.model_dump(mode="json", exclude_none=True)


def load_query_result_payload(path: Path) -> tuple[QueryResult, dict[str, Any]]:
    """Load one saved query result payload plus raw route metadata."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("query result file must contain a JSON object")
    result = QueryResult.model_validate(payload)
    diagnostics = payload.get("diagnostics")
    route = {}
    if isinstance(diagnostics, Mapping):
        route_value = diagnostics.get("route")
        if isinstance(route_value, Mapping):
            route = {str(key): value for key, value in route_value.items()}
    return result, route


def build_feedback_record(
    *,
    query: str,
    tool_name: ToolName,
    query_intent: QueryIntent,
    profile_id: str,
    snapshot_id: str,
    result_status: QueryResultStatus,
    result_summary: str,
    warning_codes: Sequence[str] = (),
    source_refs: Sequence[str] = (),
    request_id: str | None = None,
    route: Mapping[str, Any] | None = None,
    evidence_feedback: Sequence[FeedbackEvidenceAnnotation] = (),
    missed_targets: Sequence[FeedbackMissedTarget] = (),
    returned_evidence_refs: Sequence[EvidenceRef] = (),
    note: str | None = None,
    query_result_path: str | None = None,
    now: datetime | None = None,
) -> FeedbackRecord:
    """Build one persisted feedback record."""

    created_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    feedback_id = build_feedback_id(query=query, created_at=created_at)
    normalized_source_refs = tuple(
        ref.strip()
        for ref in source_refs
        if isinstance(ref, str) and ref.strip()
    )
    if not normalized_source_refs:
        normalized_source_refs = (f"{_DEFAULT_FEEDBACK_SOURCE_REF_PREFIX}:{feedback_id}",)
    return FeedbackRecord(
        feedback_id=feedback_id,
        created_at=created_at.isoformat().replace("+00:00", "Z"),
        query=query,
        tool_name=tool_name,
        query_intent=query_intent,
        profile_id=profile_id,
        snapshot_id=snapshot_id,
        result_status=result_status,
        result_summary=result_summary,
        request_id=request_id,
        warning_codes=tuple(str(code) for code in warning_codes if str(code).strip()),
        source_refs=normalized_source_refs,
        route={str(key): value for key, value in (route or {}).items()},
        evidence_feedback=tuple(evidence_feedback),
        missed_targets=tuple(missed_targets),
        returned_evidence_refs=tuple(returned_evidence_refs),
        note=note,
        query_result_path=query_result_path,
    )


def build_feedback_evidence_annotations(
    result: QueryResult,
    *,
    useful_ids: Sequence[str] = (),
    not_useful_ids: Sequence[str] = (),
    accepted_ids: Sequence[str] = (),
    note: str | None = None,
) -> tuple[FeedbackEvidenceAnnotation, ...]:
    """Map verdict id lists onto returned evidence refs."""

    evidence_by_id = {item.evidence_id: item for item in result.evidence_refs}
    verdict_to_ids = {
        "useful": tuple(_normalize_non_empty(values=useful_ids)),
        "not_useful": tuple(_normalize_non_empty(values=not_useful_ids)),
        "accepted_final": tuple(_normalize_non_empty(values=accepted_ids)),
    }
    _validate_evidence_verdict_assignments(
        verdict_to_ids=verdict_to_ids,
        evidence_by_id=evidence_by_id,
    )

    annotations: list[FeedbackEvidenceAnnotation] = []
    for verdict, evidence_ids in verdict_to_ids.items():
        for evidence_id in evidence_ids:
            annotations.append(
                FeedbackEvidenceAnnotation.from_evidence_ref(
                    evidence_by_id[evidence_id],
                    verdict=verdict,
                    note=note,
                )
            )
    return tuple(annotations)


def write_feedback_record(base_dir: Path, record: FeedbackRecord) -> Path:
    """Persist one feedback record as JSON."""

    records_dir = ensure_feedback_directories(base_dir)["records"]
    output_path = records_dir / f"{record.feedback_id}.json"
    output_path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def load_feedback_record(base_dir: Path, feedback_id: str) -> FeedbackRecord:
    """Load one persisted feedback record."""

    record_path = ensure_feedback_directories(base_dir)["records"] / f"{feedback_id}.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("feedback record file must contain a JSON object")
    return FeedbackRecord.model_validate(payload)


def write_eval_draft(base_dir: Path, record: FeedbackRecord) -> Path:
    """Generate one eval-case suite draft from feedback."""

    suite = build_eval_case_suite(record)
    eval_dir = ensure_feedback_directories(base_dir)["eval_drafts"]
    output_path = eval_dir / f"{record.feedback_id}.yaml"
    output_path.write_text(
        yaml.safe_dump(suite.to_dict(), sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return output_path


def write_learned_seed_draft(base_dir: Path, record: FeedbackRecord) -> Path:
    """Generate one learned-seed Markdown draft that still requires review."""

    seed_dir = ensure_feedback_directories(base_dir)["learned_seed_drafts"]
    output_path = seed_dir / f"{record.feedback_id}.md"
    output_path.write_text(render_learned_seed_draft(record), encoding="utf-8")
    return output_path


def ensure_feedback_directories(base_dir: Path) -> dict[str, Path]:
    """Create and return the managed feedback artifact directories."""

    directories = {
        "root": base_dir,
        "records": base_dir / "records",
        "eval_drafts": base_dir / "eval-drafts",
        "learned_seed_drafts": base_dir / "learned-seed-drafts",
    }
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
    return directories


def build_eval_case_suite(record: FeedbackRecord) -> EvalCaseSuite:
    """Project feedback into one reviewable eval-case suite."""

    requested_profile = "auto" if record.profile_id == "not_required" else record.profile_id
    expected_profile_status = "not_required" if requested_profile == "auto" else "resolved"
    route_mode = _route_mode_from_record(record)
    selected_view = _route_text(
        record.route,
        "selected_view",
        fallback=_default_view(record.tool_name),
    )
    selected_granularity = _route_text(
        record.route,
        "selected_granularity",
        fallback=_default_granularity(record.tool_name),
    )
    warning_codes = tuple(record.warning_codes)
    case = EvalCase(
        case_id=f"feedback_{record.feedback_id}",
        title=_short_title(record.query),
        category=_infer_eval_category(record),
        priority=_infer_eval_priority(record),
        blocking_level=_infer_blocking_level(record),
        include_in_release_gate=False,
        source_refs=record.source_refs,
        input_tool=record.tool_name,
        request=QueryRequest(
            query=record.query,
            profile_id=requested_profile,
            snapshot_id=record.snapshot_id,
            caller_tool="client",
            client_context={"feedback_id": record.feedback_id},
        ),
        snapshot_requirement=record.snapshot_id,
        profile_requirement=EvalProfileRequirement(
            requested_profile_id=requested_profile,
            expected_status=expected_profile_status,
        ),
        expected_route=EvalRouteExpectation(
            intent=record.query_intent,
            primary_tool=record.tool_name,
            route_mode=route_mode,
            selected_view=selected_view,
            selected_granularity=selected_granularity,
            required_warning_codes=warning_codes,
            allowed_warning_codes=warning_codes,
        ),
        execution_mode="query_quality",
        expected_result_status=record.result_status,
        expected_evidence=_expected_evidence_from_record(record),
        tags=("feedback-draft", record.tool_name, record.result_status),
    )
    return EvalCaseSuite(
        suite_id=f"feedback-draft-{record.feedback_id}",
        description=f"Draft eval suite generated from feedback record {record.feedback_id}.",
        generated_from=record.source_refs,
        cases=(case,),
    )


def render_learned_seed_draft(record: FeedbackRecord) -> str:
    """Render one review-pending learned-seed Markdown draft."""

    title = _short_title(record.query)
    derived_from = _derived_from_locators(record)
    front_matter = {
        "title": title,
        "doc_type": "learned-seeds",
        "domain": "engineering",
        "authority_level": "derived",
        "review_status": "pending",
        "feedback_id": record.feedback_id,
        "snapshot_id": record.snapshot_id,
        "profile_id": record.profile_id,
        "source_refs": list(record.source_refs),
        "derived_from": derived_from,
        "tags": ["feedback-draft", record.tool_name, record.result_status],
    }
    sections = [
        "---",
        yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip(),
        "---",
        "",
        f"# {title}",
        "",
        "## Query",
        "",
        record.query,
        "",
        "## Observed Result",
        "",
        f"- tool: `{record.tool_name}`",
        f"- intent: `{record.query_intent}`",
        f"- result_status: `{record.result_status}`",
        f"- snapshot_id: `{record.snapshot_id}`",
        f"- profile_id: `{record.profile_id}`",
        "",
        record.result_summary,
        "",
    ]
    if record.note:
        sections.extend(["## Reviewer Notes", "", record.note, ""])
    helpful = [
        item
        for item in record.evidence_feedback
        if item.verdict in {"useful", "accepted_final"}
    ]
    if helpful:
        sections.extend(["## Helpful Evidence", ""])
        for item in helpful:
            locator = _line_locator(item.path, item.start_line, item.end_line)
            sections.append(f"- `{locator}` ({item.verdict})")
            if item.excerpt:
                sections.append(f"  excerpt: {item.excerpt}")
        sections.append("")
    not_helpful = [item for item in record.evidence_feedback if item.verdict == "not_useful"]
    if not_helpful:
        sections.extend(["## Not Helpful Evidence", ""])
        for item in not_helpful:
            locator = _line_locator(item.path, item.start_line, item.end_line)
            sections.append(f"- `{locator}`")
        sections.append("")
    if record.missed_targets:
        sections.extend(["## Missed Targets", ""])
        for item in record.missed_targets:
            sections.append(f"- `{item.kind}`: `{item.locator}`")
            if item.note:
                sections.append(f"  note: {item.note}")
        sections.append("")
    sections.extend(
        [
            "## Review Checklist",
            "",
            "- Confirm the evidence really supports the conclusion.",
            "- Decide whether this belongs in `eval/cases.yaml` as a regression case.",
            (
                "- If this should become a curated knowledge card, rewrite it in "
                "reviewer-owned words before moving it into "
                "`knowledge-sources/learned-seeds/`."
            ),
            "",
        ]
    )
    return "\n".join(sections)


def build_feedback_id(*, query: str, created_at: datetime) -> str:
    """Return a readable, timestamped feedback identifier."""

    timestamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = _slugify(query)[:24] or "feedback"
    return f"feedback-{timestamp}-{slug}"


def _validate_evidence_verdict_assignments(
    *,
    verdict_to_ids: Mapping[str, Sequence[str]],
    evidence_by_id: Mapping[str, EvidenceRef],
) -> None:
    seen: dict[str, str] = {}
    for verdict, evidence_ids in verdict_to_ids.items():
        for evidence_id in evidence_ids:
            if evidence_id not in evidence_by_id:
                available = ", ".join(sorted(evidence_by_id))
                raise ValueError(
                    f"unknown evidence id {evidence_id!r}; available ids: {available or '<none>'}"
                )
            previous = seen.get(evidence_id)
            if previous is not None and previous != verdict:
                raise ValueError(
                    "evidence id "
                    f"{evidence_id!r} cannot be assigned to both "
                    f"{previous!r} and {verdict!r}"
                )
            seen[evidence_id] = verdict


def _expected_evidence_from_record(record: FeedbackRecord) -> tuple[EvalExpectedEvidence, ...]:
    expected: list[EvalExpectedEvidence] = []
    for item in record.evidence_feedback:
        if item.verdict not in {"useful", "accepted_final"}:
            continue
        expected.append(
            EvalExpectedEvidence(
                kind=_expected_evidence_kind(item.type),
                locator=_line_locator(item.path, item.start_line, item.end_line),
                rationale=item.note or f"Feedback marked this evidence as {item.verdict}.",
            )
        )
    for item in record.missed_targets:
        expected.append(
            EvalExpectedEvidence(
                kind=item.kind,
                locator=item.locator,
                rationale=item.note or "User reported this target as missing from the result.",
            )
        )
    if expected:
        return tuple(expected)
    for item in record.returned_evidence_refs[:1]:
        expected.append(
            EvalExpectedEvidence(
                kind=_expected_evidence_kind(item.type),
                locator=_line_locator(item.path, item.start_line, item.end_line),
                rationale="Fallback evidence captured from the returned query result.",
            )
        )
    return tuple(expected)


def _expected_evidence_kind(evidence_type: str) -> str:
    mapping = {
        "doc": "doc_section",
        "profile": "profile",
    }
    return mapping.get(evidence_type, "path")


def _infer_eval_category(record: FeedbackRecord) -> str:
    if record.result_status != "ok":
        return "warning_degradation"
    mapping = {
        "code_exact": "symbol_lookup",
        "api_lookup": "api_documentation",
        "widget_lookup": "widget_usage",
        "workspace_nav": "workspace_navigation",
        "profile_diff": "profile_impact",
    }
    return mapping.get(record.query_intent, "feature_domain_cross_layer")


def _infer_eval_priority(record: FeedbackRecord) -> EvalCasePriority:
    if record.result_status in {"blocked", "error", "zero_result"} or record.missed_targets:
        return "P1"
    return "P2"


def _infer_blocking_level(record: FeedbackRecord) -> EvalBlockingLevel:
    if record.result_status in {"blocked", "error"} or record.missed_targets:
        return "blocker"
    if record.result_status in {"partial_ready", "low_confidence", "zero_result"}:
        return "warning"
    return "advisory"


def _route_mode_from_record(record: FeedbackRecord) -> RouteMode:
    tool_plan = record.route.get("tool_plan")
    route_mode = tool_plan.get("route_mode") if isinstance(tool_plan, Mapping) else None
    if isinstance(route_mode, str) and route_mode in {"direct", "chain", "explore"}:
        return route_mode
    return "explore" if record.tool_name == "kb_search" else "direct"


def _route_text(route: Mapping[str, Any], key: str, *, fallback: str) -> str:
    value = route.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return fallback


def _default_view(tool_name: ToolName) -> str:
    mapping = {
        "docs_search": "evidence",
        "workspace_view": "workspace",
        "config_impact": "profile",
    }
    return mapping.get(tool_name, "code")


def _default_granularity(tool_name: ToolName) -> str:
    mapping = {
        "docs_search": "doc_section",
        "workspace_view": "workspace",
        "config_impact": "profile",
        "evidence_bundle": "doc_section",
    }
    return mapping.get(tool_name, "symbol")


def _derived_from_locators(record: FeedbackRecord) -> list[str]:
    locators: list[str] = []
    for item in record.evidence_feedback:
        if item.verdict not in {"useful", "accepted_final"}:
            continue
        locators.append(_line_locator(item.path, item.start_line, item.end_line))
    if locators:
        return locators
    return [
        _line_locator(item.path, item.start_line, item.end_line)
        for item in record.returned_evidence_refs
    ]


def _line_locator(path: str, start_line: int | None, end_line: int | None) -> str:
    if start_line is None:
        return path
    if end_line is None or end_line == start_line:
        return f"{path}:L{start_line}"
    return f"{path}:L{start_line}-L{end_line}"


def _normalize_non_empty(*, values: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = str(value).strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(stripped)
    return normalized


def _short_title(query: str) -> str:
    normalized = " ".join(query.split())
    if len(normalized) <= 64:
        return normalized
    return f"{normalized[:61].rstrip()}..."


def _slugify(value: str) -> str:
    normalized = value.lower().strip()
    normalized = _SLUG_RE.sub("-", normalized).strip("-")
    return normalized
