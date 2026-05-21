"""Profile-aware query helpers for unresolved and compare-to flows."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.models.evidence import EvidenceRef
from active_knowledge_server.models.query import QueryIntent, QueryRequest
from active_knowledge_server.models.responses import Candidate, QueryResult, SuggestedFilter, Warning
from active_knowledge_server.models.routing import RouterDecision
from active_knowledge_server.query.retrievers import dedupe_query_warnings
from active_knowledge_server.storage import (
	ALL_SCOPE,
	LogicalEntity,
	ProfileRecord,
	QueryScope,
	RelationRecord,
	StorageAdapter,
)

_MACRO_NAME_RE: Final = re.compile(r"^CONFIG_[A-Z0-9_]+$")
_PROFILE_SENSITIVE_INTENTS: Final[frozenset[QueryIntent]] = frozenset(
	{"call_trace", "runtime_flow", "profile_diff"}
)
_PROFILE_RESOLUTION_BLOCKING_STATUSES: Final[frozenset[str]] = frozenset(
	{"multiple_candidates", "unresolved", "invalid"}
)
_PROFILE_RELATION_TYPES: Final[frozenset[str]] = frozenset(
	{"enabled_by", "disabled_by", "unknown_by"}
)


def execution_scope_profile_id(decision: RouterDecision, request: QueryRequest) -> str:
	"""Return the storage-scope profile ID used for query execution."""

	resolved = decision.profile_resolution.get("resolved_profile_id")
	if isinstance(resolved, str) and resolved.strip():
		return resolved.strip()
	if isinstance(request.profile_id, str) and request.profile_id not in {"", "auto"}:
		return request.profile_id
	return ALL_SCOPE


def reported_profile_id(decision: RouterDecision, request: QueryRequest) -> str:
	"""Return the profile label exposed on QueryResult."""

	status = _profile_resolution_status(decision)
	resolved = _primary_profile_id(decision, request)
	if status == "not_required":
		return "not_required"
	if resolved is not None:
		return resolved
	if status in _PROFILE_RESOLUTION_BLOCKING_STATUSES:
		return "unresolved"
	return ALL_SCOPE


def build_profile_resolution_result(
	*,
	request: QueryRequest,
	decision: RouterDecision,
	warnings: Sequence[Warning],
) -> QueryResult | None:
	"""Return a contract result when a profile-sensitive query cannot resolve one profile."""

	merged_warnings = _merged_profile_warnings(decision, warnings)
	status = _profile_resolution_status(decision)
	if status not in _PROFILE_RESOLUTION_BLOCKING_STATUSES:
		return None
	if not _requires_resolved_profile(decision):
		return None
	resolution = dict(decision.profile_resolution)
	snapshot_id = request.snapshot_id or "current"
	if status == "multiple_candidates":
		result_candidates = tuple(
			_profile_resolution_candidate_to_result_candidate(candidate)
			for candidate in _profile_resolution_candidates(decision)
		)
		return QueryResult(
			tool_name=decision.tool_plan.primary_tool,
			result_status="multi_result",
			confidence=min(decision.confidence, 0.49),
			query_intent=decision.intent,
			snapshot_id=snapshot_id,
			profile_id="unresolved",
			summary="Profile-sensitive query requires an explicit profile selection before analysis can continue.",
			candidates=result_candidates,
			warnings=dedupe_query_warnings(merged_warnings),
			next_queries=_profile_resolution_next_queries(request, decision),
			suggested_filters=tuple(
				SuggestedFilter(field="profile_id", value=candidate.profile_id)
				for candidate in result_candidates
				if candidate.profile_id is not None
			),
			diagnostics={
				"route": decision.to_dict(),
				"profile_resolution": resolution,
			},
		)
	return _build_profile_ambiguous_result(
		request=request,
		decision=decision,
		warnings=merged_warnings,
		required_context=("profile_id",),
		summary="Profile-sensitive query could not resolve a valid profile context.",
	)


def build_profile_matrix_result(
	*,
	config: ActiveKnowledgeConfig,
	metadata_adapter: StorageAdapter | None,
	request: QueryRequest,
	decision: RouterDecision,
	warnings: Sequence[Warning],
) -> QueryResult | None:
	"""Return a profile matrix/config impact result when compare_to or macro scope is available."""

	if metadata_adapter is None or decision.intent != "profile_diff":
		return None
	macro_or_config = _primary_arg_text(decision, "macro_or_config")
	compare_to = _compare_to_profile_id(decision, request)
	if compare_to is None and not _looks_like_macro_name(macro_or_config):
		return None
	primary_profile_id = _primary_profile_id(decision, request)
	if primary_profile_id is None:
		return None
	reader = metadata_adapter.reader()
	snapshot_id = request.snapshot_id or "current"
	profiles = _latest_profiles_by_id(tuple(reader.iter_profiles(snapshot_id=snapshot_id)))
	primary_profile = profiles.get(primary_profile_id)
	if primary_profile is None:
		return _build_profile_ambiguous_result(
			request=request,
			decision=decision,
			warnings=[
				*warnings,
				_build_query_warning(
					code="profile.invalid",
					level="caution",
					message="The requested profile is not indexed for the current snapshot.",
					details={"profile_id": primary_profile_id, "snapshot_id": snapshot_id},
					actionable=True,
					suggested_action="Specify a valid profile_id or refresh the current snapshot.",
				),
			],
			required_context=("profile_id",),
			summary="Profile-aware analysis requires a valid primary profile context.",
		)
	compare_profile = None if compare_to is None else profiles.get(compare_to)
	if compare_to is not None and compare_profile is None:
		return _build_profile_ambiguous_result(
			request=request,
			decision=decision,
			warnings=[
				*warnings,
				_build_query_warning(
					code="profile.invalid",
					level="caution",
					message="The compare_to profile is not indexed for the current snapshot.",
					details={"compare_to": compare_to, "snapshot_id": snapshot_id},
					actionable=True,
					suggested_action="Specify a valid compare_to profile_id or refresh the current snapshot.",
				),
			],
			required_context=("compare_to",),
			summary="Profile-aware analysis requires a valid compare_to profile context.",
		)
	target_macros = _resolve_target_macros(
		macro_or_config=macro_or_config,
		primary_profile=primary_profile,
		compare_profile=compare_profile,
	)
	if not target_macros:
		return _build_profile_zero_result(
			request=request,
			decision=decision,
			warnings=[
				*warnings,
				_build_query_warning(
					code="retrieval.zero_result",
					level="caution",
					message="No differing macros were found for the requested profile comparison.",
					details={
						"primary_profile_id": primary_profile.profile_id,
						"compare_to": None if compare_profile is None else compare_profile.profile_id,
					},
					actionable=True,
					suggested_action="Specify a different compare_to profile or narrow the macro_or_config target.",
				),
			],
			next_queries=(
				f"{request.query} profile_id={primary_profile.profile_id}",
				f"{request.query} compare_to={compare_to or primary_profile.profile_id}",
			),
			suggested_filters=tuple(
				filter_item
				for filter_item in (
					SuggestedFilter(field="profile_id", value=primary_profile.profile_id),
					None
					if compare_profile is None
					else SuggestedFilter(field="compare_to", value=compare_profile.profile_id),
				)
				if filter_item is not None
			),
		)
	relations = tuple(reader.iter_relations(QueryScope(snapshot_id=snapshot_id)))
	entities = _logical_entities_by_id(
		tuple(reader.logical_entities(QueryScope(snapshot_id=snapshot_id, profile_id=ALL_SCOPE)))
	)
	target_profiles = (primary_profile,) if compare_profile is None else (primary_profile, compare_profile)
	evidence_catalog: dict[tuple[str, str, int | None, int | None], EvidenceRef] = {}
	evidence_trace: list[dict[str, object]] = []
	items: list[dict[str, object]] = []
	for profile in target_profiles:
		peer_profile = None
		if compare_profile is not None:
			peer_profile = compare_profile if profile.profile_id != compare_profile.profile_id else primary_profile
		item, item_evidence = _build_profile_matrix_item(
			profile=profile,
			compare_profile=peer_profile,
			target_macros=target_macros,
			macro_or_config=macro_or_config,
			relations=relations,
			entities=entities,
		)
		registered_ids: list[str] = []
		for evidence_ref in item_evidence:
			key = (evidence_ref.type, evidence_ref.path, evidence_ref.start_line, evidence_ref.end_line)
			stored = evidence_catalog.setdefault(key, evidence_ref)
			registered_ids.append(stored.evidence_id)
			if len(evidence_trace) < config.query.max_evidence_items:
				evidence_trace.append(
					{
						"evidence_id": stored.evidence_id,
						"path": stored.path,
						"profile_id": profile.profile_id,
						"macro_count": len(target_macros),
					}
				)
		item["evidence_refs"] = registered_ids
		items.append(item)
	confidence = round(min(1.0, (decision.confidence * 0.85) + 0.10), 6)
	compare_label = None if compare_profile is None else compare_profile.profile_id
	return QueryResult(
		tool_name=decision.tool_plan.primary_tool,
		result_status="ok",
		confidence=confidence,
		query_intent=decision.intent,
		snapshot_id=snapshot_id,
		profile_id=primary_profile.profile_id if compare_profile is None else "multi",
		summary=_build_profile_matrix_summary(
			primary_profile_id=primary_profile.profile_id,
			compare_to=compare_label,
			macro_count=len(target_macros),
		),
		items=tuple(items),
		evidence_refs=tuple(evidence_catalog.values())[: config.query.max_evidence_items],
		warnings=dedupe_query_warnings(warnings),
		diagnostics={
			"route": decision.to_dict(),
			"profile_matrix": {
				"primary_profile_id": primary_profile.profile_id,
				"compare_to": compare_label,
				"target_macros": list(target_macros),
				"profile_count": len(target_profiles),
			},
			"retrieval_trace": {
				"route_trace": [item.to_dict() for item in decision.route_trace],
				"retriever_runs": [
					{
						"retriever": "profile_matrix",
						"returned_profiles": len(target_profiles),
						"target_macros": list(target_macros),
					}
				],
				"fusion_strategy": {
					"name": "profile_matrix",
					"rerank_mode": "profile_aware",
					"weights": {},
				},
				"ranked_candidates": [],
				"evidence_trace": evidence_trace,
			},
		},
	)


def _profile_resolution_status(decision: RouterDecision) -> str:
	status = decision.profile_resolution.get("status")
	return status.strip() if isinstance(status, str) and status.strip() else "unresolved"


def _requires_resolved_profile(decision: RouterDecision) -> bool:
	return decision.intent in _PROFILE_SENSITIVE_INTENTS or decision.selected_view == "profile"


def _primary_profile_id(decision: RouterDecision, request: QueryRequest) -> str | None:
	resolved = decision.profile_resolution.get("resolved_profile_id")
	if isinstance(resolved, str) and resolved.strip():
		return resolved.strip()
	if isinstance(request.profile_id, str) and request.profile_id not in {"", "auto", "current"}:
		return request.profile_id.strip()
	return None


def _compare_to_profile_id(decision: RouterDecision, request: QueryRequest) -> str | None:
	primary = decision.tool_plan.primary_args.get("compare_to")
	if isinstance(primary, str) and primary.strip():
		return primary.strip()
	context_compare = request.client_context.get("compare_to")
	if isinstance(context_compare, str) and context_compare.strip():
		return context_compare.strip()
	profile_ids = request.client_context.get("profile_ids")
	if not isinstance(profile_ids, (list, tuple)):
		return None
	primary_profile_id = _primary_profile_id(decision, request)
	for item in profile_ids:
		if not isinstance(item, str):
			continue
		value = item.strip()
		if value and value != primary_profile_id:
			return value
	return None


def _primary_arg_text(decision: RouterDecision, key: str) -> str | None:
	value = decision.tool_plan.primary_args.get(key)
	if isinstance(value, str) and value.strip():
		return value.strip()
	return None


def _profile_resolution_candidates(decision: RouterDecision) -> tuple[Mapping[str, object], ...]:
	payload = decision.profile_resolution.get("candidates")
	if not isinstance(payload, list):
		return ()
	return tuple(item for item in payload if isinstance(item, dict))


def _merged_profile_warnings(
	decision: RouterDecision,
	warnings: Sequence[Warning],
) -> list[Warning]:
	merged = list(warnings)
	resolution_warnings = decision.profile_resolution.get("warnings")
	if not isinstance(resolution_warnings, list):
		return merged
	for payload in resolution_warnings:
		if not isinstance(payload, dict):
			continue
		code = _mapping_text(payload, "code") or "profile.unresolved"
		merged.append(
			Warning(
				level=_mapping_text(payload, "level") or "caution",
				code=code,
				message=_mapping_text(payload, "message") or "Profile resolution warning.",
				details=dict(payload.get("details", {})) if isinstance(payload.get("details", {}), dict) else {},
				actionable=True,
				suggested_action=(
					"Specify an explicit profile_id or compare_to profile and retry."
					if code == "profile.multiple_candidates"
					else "Specify an explicit profile_id and retry."
				),
			)
		)
	return merged


def _profile_resolution_candidate_to_result_candidate(payload: Mapping[str, object]) -> Candidate:
	profile_id = _mapping_text(payload, "profile_id")
	profile_record_id = _mapping_text(payload, "profile_record_id") or profile_id or "profile:unknown"
	path = _mapping_text(payload, "dotconfig_path") or _mapping_text(payload, "defconfig_path")
	module = _mapping_text(payload, "app") or _mapping_text(payload, "board")
	confidence = payload.get("confidence")
	try:
		score = float(confidence)
	except (TypeError, ValueError):
		score = 0.0
	return Candidate(
		disambiguation_key=profile_record_id,
		entity_type="profile",
		path=path,
		module=module,
		profile_id=profile_id,
		match_reason=_mapping_text(payload, "match_reason") or "candidate profile from auto resolution",
		score=max(0.0, min(1.0, score)),
	)


def _profile_resolution_next_queries(
	request: QueryRequest,
	decision: RouterDecision,
) -> tuple[str, ...]:
	queries = [
		f"{request.query} profile_id={profile_id}"
		for profile_id in (
			_mapping_text(candidate, "profile_id")
			for candidate in _profile_resolution_candidates(decision)
		)
		if profile_id is not None
	]
	if queries:
		return tuple(queries[:3])
	return (f"{request.query} 请指定 profile_id 后重试。",)


def _build_profile_zero_result(
	*,
	request: QueryRequest,
	decision: RouterDecision,
	warnings: Sequence[Warning],
	next_queries: Sequence[str],
	suggested_filters: Sequence[SuggestedFilter],
) -> QueryResult:
	return QueryResult(
		tool_name=decision.tool_plan.primary_tool,
		result_status="zero_result",
		confidence=0.0,
		query_intent=decision.intent,
		snapshot_id=request.snapshot_id or "current",
		profile_id="multi" if _compare_to_profile_id(decision, request) is not None else reported_profile_id(decision, request),
		summary="Profile-aware analysis did not find any diff-bearing macros or impacted entities.",
		warnings=dedupe_query_warnings(warnings),
		next_queries=tuple(next_queries),
		suggested_filters=tuple(suggested_filters),
		diagnostics={
			"route": decision.to_dict(),
			"profile_resolution": dict(decision.profile_resolution),
		},
	)


def _build_profile_ambiguous_result(
	*,
	request: QueryRequest,
	decision: RouterDecision,
	warnings: Sequence[Warning],
	required_context: Sequence[str],
	summary: str,
) -> QueryResult:
	return QueryResult(
		tool_name=decision.tool_plan.primary_tool,
		result_status="ambiguous",
		confidence=min(decision.confidence, 0.49),
		query_intent=decision.intent,
		snapshot_id=request.snapshot_id or "current",
		profile_id="unresolved",
		summary=summary,
		warnings=dedupe_query_warnings(warnings),
		next_queries=_profile_resolution_next_queries(request, decision),
		suggested_filters=tuple(
			SuggestedFilter(field="profile_id", value=profile_id)
			for profile_id in (
				_mapping_text(candidate, "profile_id")
				for candidate in _profile_resolution_candidates(decision)
			)
			if profile_id is not None
		),
		diagnostics={
			"route": decision.to_dict(),
			"profile_resolution": dict(decision.profile_resolution),
			"required_context": list(required_context),
		},
	)


def _resolve_target_macros(
	*,
	macro_or_config: str | None,
	primary_profile: ProfileRecord,
	compare_profile: ProfileRecord | None,
) -> tuple[str, ...]:
	if _looks_like_macro_name(macro_or_config):
		assert macro_or_config is not None
		return (macro_or_config.strip(),)
	if compare_profile is None:
		return ()
	primary_assignments = _profile_macro_assignments(primary_profile)
	compare_assignments = _profile_macro_assignments(compare_profile)
	differing = sorted(
		macro_name
		for macro_name in set(primary_assignments) | set(compare_assignments)
		if _macro_assignment_signature(primary_assignments.get(macro_name))
		!= _macro_assignment_signature(compare_assignments.get(macro_name))
		and _looks_like_macro_name(macro_name)
	)
	return tuple(differing[:12])


def _profile_macro_assignments(profile: ProfileRecord) -> Mapping[str, object]:
	payload = profile.metadata.get("macro_assignments")
	return payload if isinstance(payload, dict) else {}


def _macro_assignment_signature(payload: object) -> tuple[object, object, object]:
	if not isinstance(payload, Mapping):
		return (None, None, None)
	return (payload.get("value"), payload.get("enabled"), payload.get("value_type"))


def _macro_assignment_display(profile: ProfileRecord, macro_name: str) -> str:
	payload = _profile_macro_assignments(profile).get(macro_name)
	if not isinstance(payload, Mapping):
		return "missing"
	value = payload.get("value")
	if isinstance(value, str) and value.strip():
		return value.strip()
	enabled = payload.get("enabled")
	if enabled is True:
		return "y"
	if enabled is False:
		return "n"
	return "unknown"


def _build_profile_matrix_item(
	*,
	profile: ProfileRecord,
	compare_profile: ProfileRecord | None,
	target_macros: Sequence[str],
	macro_or_config: str | None,
	relations: Sequence[RelationRecord],
	entities: Mapping[str, LogicalEntity],
) -> tuple[dict[str, object], tuple[EvidenceRef, ...]]:
	filtered_relations = [
		relation
		for relation in relations
		if relation.profile_id == profile.profile_id
		and relation.relation_type in _PROFILE_RELATION_TYPES
		and _relation_macro_name(relation) in target_macros
	]
	affected_modules: set[str] = set()
	affected_files: set[str] = set()
	affected_symbols: set[str] = set()
	condition_exprs: list[str] = []
	item_warnings: list[dict[str, object]] = []
	evidence_refs: list[EvidenceRef] = list(_profile_config_evidence_refs(profile, target_macros))
	for relation in filtered_relations:
		entity = entities.get(relation.src_entity_id)
		if entity is None:
			continue
		entity_type = entity.record.entity_type
		if entity_type == "Module":
			affected_modules.add(entity.record.name)
		elif entity_type == "File":
			affected_files.add(_entity_path(entity))
		else:
			affected_symbols.add(entity.record.name)
		condition_expr = _mapping_text(relation.metadata, "condition_expr")
		if condition_expr is not None:
			condition_exprs.append(condition_expr)
		if relation.relation_type == "unknown_by":
			item_warnings.append(
				{
					"code": "profile.unknown_macro",
					"message": "At least one guarding macro could not be resolved for this profile.",
					"relation_id": relation.relation_id,
				}
			)
		evidence_refs.append(
			EvidenceRef(
				evidence_id=f"entity:{entity.logical_object_id}",
				type="code",
				path=_entity_path(entity),
				start_line=entity.record.start_line,
				end_line=entity.record.end_line,
				authority_level="workspace_code",
				excerpt=entity.record.qualified_name,
				source_index=(
					entity.source_index if entity.source_index in {"baseline", "overlay", "merged"} else None
				),
			)
		)
	item = {
		"profile_id": profile.profile_id,
		"profile_record_id": profile.profile_record_id,
		"status": _profile_target_status(
			profile=profile,
			target_macro=macro_or_config if _looks_like_macro_name(macro_or_config) else None,
			relations=filtered_relations,
		),
		"macro_diff": _build_macro_diff_entries(
			target_macros=target_macros,
			profile=profile,
			compare_profile=compare_profile,
		),
		"affected_modules": sorted(affected_modules),
		"affected_files": sorted(affected_files),
		"affected_symbols": sorted(affected_symbols),
		"condition_expr": _join_unique(condition_exprs),
		"warnings": item_warnings,
		"evidence_refs": [],
	}
	return item, _dedupe_evidence_refs(evidence_refs)


def _profile_target_status(
	*,
	profile: ProfileRecord,
	target_macro: str | None,
	relations: Sequence[RelationRecord],
) -> str:
	if target_macro is not None:
		value = _macro_assignment_display(profile, target_macro)
		if value in {"y", "1", "true", "True"}:
			return "enabled"
		if value in {"n", "0", "false", "False"}:
			return "disabled"
		if value == "unknown":
			return "unknown"
		if any(relation.relation_type == "unknown_by" for relation in relations):
			return "unknown"
		return "not_applicable"
	if any(relation.relation_type == "unknown_by" for relation in relations):
		return "unknown"
	return "not_applicable"


def _build_macro_diff_entries(
	*,
	target_macros: Sequence[str],
	profile: ProfileRecord,
	compare_profile: ProfileRecord | None,
) -> list[dict[str, object]]:
	entries: list[dict[str, object]] = []
	for macro_name in target_macros:
		current_value = _macro_assignment_display(profile, macro_name)
		entry: dict[str, object] = {
			"macro": macro_name,
			"value": current_value,
		}
		if compare_profile is not None:
			compare_value = _macro_assignment_display(compare_profile, macro_name)
			entry["compare_to"] = compare_value
			entry["compare_profile_id"] = compare_profile.profile_id
			if current_value == compare_value and len(target_macros) > 1:
				continue
		entries.append(entry)
	return entries


def _profile_config_evidence_refs(
	profile: ProfileRecord,
	target_macros: Sequence[str],
) -> tuple[EvidenceRef, ...]:
	macro_excerpt = ", ".join(
		f"{macro_name}={_macro_assignment_display(profile, macro_name)}"
		for macro_name in target_macros[:3]
	)
	refs: list[EvidenceRef] = []
	if profile.defconfig_path:
		refs.append(
			EvidenceRef(
				evidence_id=f"profile:{profile.profile_id}:defconfig",
				type="config",
				path=profile.defconfig_path,
				authority_level="profile_config",
				excerpt=macro_excerpt or profile.profile_id,
				source_index="merged",
			)
		)
	if profile.dotconfig_path:
		refs.append(
			EvidenceRef(
				evidence_id=f"profile:{profile.profile_id}:dotconfig",
				type="config",
				path=profile.dotconfig_path,
				authority_level="profile_config",
				excerpt=macro_excerpt or profile.profile_id,
				source_index="merged",
			)
		)
	return tuple(refs)


def _dedupe_evidence_refs(evidence_refs: Sequence[EvidenceRef]) -> tuple[EvidenceRef, ...]:
	unique: dict[tuple[str, str, int | None, int | None], EvidenceRef] = {}
	for evidence_ref in evidence_refs:
		key = (
			evidence_ref.type,
			evidence_ref.path,
			evidence_ref.start_line,
			evidence_ref.end_line,
		)
		unique.setdefault(key, evidence_ref)
	return tuple(unique.values())


def _latest_profiles_by_id(profiles: Sequence[ProfileRecord]) -> dict[str, ProfileRecord]:
	latest: dict[str, ProfileRecord] = {}
	for profile in sorted(profiles, key=lambda item: item.profile_record_id):
		latest[profile.profile_id] = profile
	return latest


def _logical_entities_by_id(entities: Sequence[LogicalEntity]) -> dict[str, LogicalEntity]:
	return {entity.logical_object_id: entity for entity in entities}


def _relation_macro_name(relation: RelationRecord) -> str | None:
	macro_name = _mapping_text(relation.metadata, "macro_name")
	if macro_name is not None:
		return macro_name
	condition_macros = relation.metadata.get("condition_macros")
	if not isinstance(condition_macros, list):
		return None
	for macro in condition_macros:
		if isinstance(macro, str) and macro.strip():
			return macro.strip()
	return None


def _entity_path(entity: LogicalEntity) -> str:
	path = entity.record.path
	for separator in ("::", "#"):
		if separator in path:
			return path.split(separator, 1)[0]
	return path


def _join_unique(values: Sequence[str]) -> str | None:
	seen: set[str] = set()
	ordered: list[str] = []
	for value in values:
		text = value.strip()
		if not text or text in seen:
			continue
		seen.add(text)
		ordered.append(text)
	if not ordered:
		return None
	return " || ".join(ordered)


def _mapping_text(payload: Mapping[str, object], key: str) -> str | None:
	value = payload.get(key)
	if isinstance(value, str) and value.strip():
		return value.strip()
	return None


def _looks_like_macro_name(value: str | None) -> bool:
	return bool(value and _MACRO_NAME_RE.fullmatch(value.strip()))


def _build_profile_matrix_summary(
	*,
	primary_profile_id: str,
	compare_to: str | None,
	macro_count: int,
) -> str:
	if compare_to is None:
		return (
			f"Profile-aware impact analysis returned 1 profile view for {primary_profile_id} "
			f"across {macro_count} relevant macros."
		)
	return (
		f"Profile-aware diff compared {primary_profile_id} against {compare_to} "
		f"across {macro_count} differing macros."
	)


def _build_query_warning(
	*,
	code: str,
	level: str,
	message: str,
	details: dict[str, object],
	actionable: bool,
	suggested_action: str | None = None,
) -> Warning:
	return Warning(
		level=level,
		code=code,
		message=message,
		details=details,
		actionable=actionable,
		suggested_action=suggested_action,
	)