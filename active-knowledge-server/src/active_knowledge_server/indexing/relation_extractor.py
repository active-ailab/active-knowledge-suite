"""Profile-conditioned relation projection over static code relations."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from active_knowledge_server.storage import (
	ALL_SCOPE,
	EntityRecord,
	ProfileRecord,
	QueryScope,
	RelationRecord,
	StorageReader,
	StorageWriter,
)

PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION: Final = "profile_conditioned_relations.v1"
ConditionStatus = Literal["enabled", "disabled", "unknown", "not_applicable"]

_PROFILE_RELATION_TYPE_BY_STATUS: Final[dict[str, str]] = {
	"enabled": "enabled_by",
	"disabled": "disabled_by",
	"unknown": "unknown_by",
}
_STATUS_BY_PROFILE_RELATION_TYPE: Final[dict[str, ConditionStatus]] = {
	"enabled_by": "enabled",
	"disabled_by": "disabled",
	"unknown_by": "unknown",
}
_MACRO_REF_RE: Final = re.compile(r"^\$\((?P<macro>[A-Za-z_][A-Za-z0-9_]*)\)$")
_IDENT_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ProfileMacroAssignment:
	"""One resolved macro assignment loaded from persisted profile metadata."""

	macro_name: str
	value: str | None
	value_type: str | None
	enabled: bool | None
	source_kind: str | None = None


@dataclass(frozen=True)
class IndexedProfileConditionedRelations:
	"""One extracted batch of profile-conditioned relation records."""

	schema_version: str
	snapshot_id: str
	relation_records: tuple[RelationRecord, ...]

	def to_dict(self) -> dict[str, object]:
		"""Return a compact JSON-serializable summary."""

		profile_ids = sorted({record.profile_id for record in self.relation_records})
		relation_types = sorted({record.relation_type for record in self.relation_records})
		return {
			"schema_version": self.schema_version,
			"snapshot_id": self.snapshot_id,
			"profile_ids": profile_ids,
			"relation_types": relation_types,
			"relation_count": len(self.relation_records),
		}


@dataclass(frozen=True)
class ProfileConditionedEntityState:
	"""One multi-profile availability view for one entity."""

	entity_id: str
	profile_id: str
	profile_record_id: str | None
	status: ConditionStatus
	condition_expr: str | None = None
	condition_macros: tuple[str, ...] = ()
	macro_entity_ids: tuple[str, ...] = ()
	relation_ids: tuple[str, ...] = ()
	confidence: float | None = None
	unknown_macros: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileRelationRebuildPlan:
	"""Profile IDs whose conditioned relations must be recomputed."""

	changed_profile_ids: tuple[str, ...]
	added_profile_ids: tuple[str, ...]
	removed_profile_ids: tuple[str, ...]
	unchanged_profile_ids: tuple[str, ...]

	@property
	def recompute_required(self) -> bool:
		"""Return whether profile-conditioned relations should be recomputed."""

		return bool(self.changed_profile_ids or self.added_profile_ids or self.removed_profile_ids)


@dataclass(frozen=True)
class _MacroTarget:
	macro_name: str
	dst_entity_id: str


@dataclass(frozen=True)
class _ConditionFragment:
	expr: str | None
	macro_targets: tuple[_MacroTarget, ...]
	source_relation_ids: tuple[str, ...]
	confidence: float
	inherited_from: tuple[str, ...] = ()

	@property
	def has_condition(self) -> bool:
		return bool(self.expr or self.macro_targets)


@dataclass(frozen=True)
class _ConditionEvaluation:
	status: ConditionStatus
	unknown_macros: tuple[str, ...]


class ProfileConditionedRelationExtractor:
	"""Project guarded code entities into profile-aware relation records."""

	def collect(
		self,
		*,
		snapshot_id: str,
		profiles: Sequence[ProfileRecord],
		entities: Sequence[EntityRecord],
		relations: Sequence[RelationRecord],
	) -> IndexedProfileConditionedRelations:
		"""Derive `enabled_by`/`disabled_by`/`unknown_by` relations per profile."""

		entity_by_id = {entity.entity_id: entity for entity in entities}
		base_relations = tuple(
			record
			for record in relations
			if record.snapshot_id == snapshot_id and record.profile_id == ALL_SCOPE
		)
		direct_conditions = _build_direct_conditions(base_relations, entity_by_id)
		file_parents = _build_file_parent_map(base_relations, entity_by_id)
		latest_profiles = latest_profiles_by_id(profiles)
		effective_conditions = _resolve_effective_conditions(
			entity_by_id=entity_by_id,
			direct_conditions=direct_conditions,
			file_parents=file_parents,
		)

		conditioned_records: list[RelationRecord] = []
		for entity_id, entity in entity_by_id.items():
			if entity.snapshot_id != snapshot_id or not _is_conditionable_entity(entity):
				continue
			effective = effective_conditions.get(entity_id)
			if effective is None or not effective.has_condition:
				continue
			condition_macros = tuple(
				_unique_in_order(target.macro_name for target in effective.macro_targets)
			)
			if not condition_macros:
				continue
			for profile_id, profile in latest_profiles.items():
				assignments = profile_macro_assignments(profile)
				evaluation = _evaluate_condition(
					effective.expr,
					condition_macros=condition_macros,
					assignments=assignments,
				)
				relation_type = _PROFILE_RELATION_TYPE_BY_STATUS.get(evaluation.status)
				if relation_type is None:
					continue
				confidence = _conditioned_confidence(entity, profile, effective, evaluation)
				for target in effective.macro_targets:
					conditioned_records.append(
						RelationRecord(
							relation_id=_profile_conditioned_relation_id(
								src_entity_id=entity.entity_id,
								dst_entity_id=target.dst_entity_id,
								profile_id=profile_id,
							),
							snapshot_id=snapshot_id,
							relation_type=relation_type,
							src_entity_id=entity.entity_id,
							dst_entity_id=target.dst_entity_id,
							source_scope=entity.source_scope,
							profile_id=profile_id,
							metadata={
								"extractor": "profile_conditioned_relation_extractor",
								"schema_version": PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION,
								"condition_expr": effective.expr,
								"condition_macros": list(condition_macros),
								"macro_name": target.macro_name,
								"confidence": confidence,
								"profile_record_id": profile.profile_record_id,
								"profile_config_hash": profile.metadata.get("config_hash"),
								"profile_macro_summary_hash": profile.metadata.get(
									"macro_summary_hash"
								),
								"evaluation_status": evaluation.status,
								"unknown_macros": list(evaluation.unknown_macros),
								"inherited_from": list(effective.inherited_from),
								"source_relation_ids": list(effective.source_relation_ids),
							},
						)
					)

		return IndexedProfileConditionedRelations(
			schema_version=PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION,
			snapshot_id=snapshot_id,
			relation_records=tuple(
				sorted(
					conditioned_records,
					key=lambda record: (
						record.profile_id,
						record.src_entity_id,
						record.dst_entity_id,
						record.relation_id,
					),
				)
			),
		)

	def collect_and_store(
		self,
		writer: StorageWriter,
		*,
		snapshot_id: str,
		profiles: Sequence[ProfileRecord],
		entities: Sequence[EntityRecord],
		relations: Sequence[RelationRecord],
	) -> IndexedProfileConditionedRelations:
		"""Extract and persist profile-conditioned relations."""

		indexed = self.collect(
			snapshot_id=snapshot_id,
			profiles=profiles,
			entities=entities,
			relations=relations,
		)
		for record in indexed.relation_records:
			writer.upsert_relation(record)
		writer.flush()
		return indexed


def summarize_entity_profile_states(
	relations: Iterable[RelationRecord],
	*,
	entity_id: str,
	profiles: Sequence[ProfileRecord],
) -> tuple[ProfileConditionedEntityState, ...]:
	"""Summarize one entity as enabled/disabled/unknown across profiles."""

	grouped: dict[str, list[RelationRecord]] = defaultdict(list)
	for relation in relations:
		if relation.src_entity_id != entity_id:
			continue
		if relation.relation_type not in _STATUS_BY_PROFILE_RELATION_TYPE:
			continue
		grouped[relation.profile_id].append(relation)

	items: list[ProfileConditionedEntityState] = []
	for profile in profiles:
		profile_relations = grouped.get(profile.profile_id, [])
		if not profile_relations:
			items.append(
				ProfileConditionedEntityState(
					entity_id=entity_id,
					profile_id=profile.profile_id,
					profile_record_id=profile.profile_record_id,
					status="not_applicable",
				)
			)
			continue
		statuses = {
			_STATUS_BY_PROFILE_RELATION_TYPE[relation.relation_type]
			for relation in profile_relations
			if relation.relation_type in _STATUS_BY_PROFILE_RELATION_TYPE
		}
		if "unknown" in statuses:
			status = "unknown"
		elif "disabled" in statuses:
			status = "disabled"
		else:
			status = "enabled"
		condition_exprs = _unique_in_order(
			value
			for relation in profile_relations
			if (value := _metadata_text(relation.metadata, "condition_expr")) is not None
		)
		condition_macros = _unique_in_order(
			macro
			for relation in profile_relations
			for macro in _metadata_strings(relation.metadata, "condition_macros")
		)
		macro_entity_ids = _unique_in_order(
			relation.dst_entity_id for relation in profile_relations
		)
		unknown_macros = _unique_in_order(
			macro
			for relation in profile_relations
			for macro in _metadata_strings(relation.metadata, "unknown_macros")
		)
		confidences = [
			value
			for relation in profile_relations
			if (value := _metadata_float(relation.metadata, "confidence")) is not None
		]
		items.append(
			ProfileConditionedEntityState(
				entity_id=entity_id,
				profile_id=profile.profile_id,
				profile_record_id=profile.profile_record_id,
				status=status,
				condition_expr=" && ".join(condition_exprs) if condition_exprs else None,
				condition_macros=tuple(condition_macros),
				macro_entity_ids=tuple(macro_entity_ids),
				relation_ids=tuple(sorted(relation.relation_id for relation in profile_relations)),
				confidence=min(confidences) if confidences else None,
				unknown_macros=tuple(unknown_macros),
			)
		)
	return tuple(items)


def summarize_entity_profile_states_from_reader(
	reader: StorageReader,
	*,
	entity_id: str,
	profiles: Sequence[ProfileRecord],
	snapshot_id: str,
) -> tuple[ProfileConditionedEntityState, ...]:
	"""Read and summarize one entity across multiple profiles from storage."""

	return summarize_entity_profile_states(
		reader.iter_relations(QueryScope(snapshot_id=snapshot_id)),
		entity_id=entity_id,
		profiles=profiles,
	)


def plan_profile_conditioned_relation_rebuild(
	previous_profiles: Sequence[ProfileRecord],
	current_profiles: Sequence[ProfileRecord],
) -> ProfileRelationRebuildPlan:
	"""Compare config hashes and return the minimal set of affected profile IDs."""

	previous = latest_profiles_by_id(previous_profiles)
	current = latest_profiles_by_id(current_profiles)
	all_ids = sorted(set(previous) | set(current))
	added: list[str] = []
	removed: list[str] = []
	changed: list[str] = []
	unchanged: list[str] = []
	for profile_id in all_ids:
		old = previous.get(profile_id)
		new = current.get(profile_id)
		if old is None and new is not None:
			added.append(profile_id)
			continue
		if old is not None and new is None:
			removed.append(profile_id)
			continue
		assert old is not None and new is not None
		if profile_config_hash(old) != profile_config_hash(new):
			changed.append(profile_id)
		else:
			unchanged.append(profile_id)
	return ProfileRelationRebuildPlan(
		changed_profile_ids=tuple(changed),
		added_profile_ids=tuple(added),
		removed_profile_ids=tuple(removed),
		unchanged_profile_ids=tuple(unchanged),
	)


def latest_profiles_by_id(profiles: Sequence[ProfileRecord]) -> dict[str, ProfileRecord]:
	"""Collapse profile records to the latest record per readable profile ID."""

	latest: dict[str, ProfileRecord] = {}
	for record in profiles:
		latest[record.profile_id] = record
	return latest


def profile_config_hash(record: ProfileRecord) -> str | None:
	"""Return the persisted config hash when present."""

	value = record.metadata.get("config_hash")
	return None if value is None else str(value)


def profile_macro_assignments(record: ProfileRecord) -> dict[str, ProfileMacroAssignment]:
	"""Load macro assignments from persisted profile metadata."""

	assignments: dict[str, ProfileMacroAssignment] = {}
	raw_assignments = record.metadata.get("macro_assignments")
	if isinstance(raw_assignments, Mapping):
		for macro_name, payload in raw_assignments.items():
			if not isinstance(macro_name, str) or not isinstance(payload, Mapping):
				continue
			assignments[macro_name] = ProfileMacroAssignment(
				macro_name=macro_name,
				value=None if payload.get("value") is None else str(payload.get("value")),
				value_type=None
				if payload.get("value_type") is None
				else str(payload.get("value_type")),
				enabled=_metadata_bool(payload, "enabled"),
				source_kind=None
				if payload.get("source_kind") is None
				else str(payload.get("source_kind")),
			)
		if assignments:
			return assignments

	summary = record.metadata.get("macro_summary")
	if not isinstance(summary, Mapping):
		return {}
	for macro_name in _metadata_strings(summary, "enabled_macros"):
		assignments[macro_name] = ProfileMacroAssignment(
			macro_name=macro_name,
			value="y",
			value_type="bool",
			enabled=True,
		)
	for macro_name in _metadata_strings(summary, "disabled_macros"):
		assignments[macro_name] = ProfileMacroAssignment(
			macro_name=macro_name,
			value="n",
			value_type="bool",
			enabled=False,
		)
	return assignments


def _build_direct_conditions(
	relations: Sequence[RelationRecord],
	entity_by_id: Mapping[str, EntityRecord],
) -> dict[str, tuple[_ConditionFragment, ...]]:
	grouped: dict[str, dict[str | None, dict[str, object]]] = defaultdict(dict)
	for relation in relations:
		if relation.relation_type != "guarded_by_macro":
			continue
		entity = entity_by_id.get(relation.src_entity_id)
		if entity is None or not _is_conditionable_entity(entity):
			continue
		expr = _metadata_text(relation.metadata, "condition_expr")
		key = expr
		payload = grouped[relation.src_entity_id].setdefault(
			key,
			{
				"expr": expr,
				"targets": [],
				"target_keys": set(),
				"relation_ids": [],
				"confidences": [],
			},
		)
		target_keys = cast(set[tuple[str, str]], payload["target_keys"])
		relation_ids = cast(list[str], payload["relation_ids"])
		confidences = cast(list[float], payload["confidences"])
		targets = cast(list[_MacroTarget], payload["targets"])
		macro_names = _metadata_strings(relation.metadata, "condition_macros")
		if not macro_names:
			macro_entity = entity_by_id.get(relation.dst_entity_id)
			if macro_entity is not None:
				macro_names = (macro_entity.name,)
		for macro_name in macro_names:
			macro_key = (macro_name, relation.dst_entity_id)
			if macro_key in target_keys:
				continue
			target_keys.add(macro_key)
			targets.append(
				_MacroTarget(
					macro_name=macro_name,
					dst_entity_id=relation.dst_entity_id,
				)
			)
		relation_ids.append(relation.relation_id)
		if (confidence := _metadata_float(relation.metadata, "confidence")) is not None:
			confidences.append(confidence)

	results: dict[str, tuple[_ConditionFragment, ...]] = {}
	for entity_id, groups in grouped.items():
		fragments: list[_ConditionFragment] = []
		for payload in groups.values():
			targets = tuple(cast(list[_MacroTarget], payload["targets"]))
			expr = cast(str | None, payload["expr"])
			if expr is None and targets:
				expr = " && ".join(_unique_in_order(target.macro_name for target in targets))
			fragments.append(
				_ConditionFragment(
					expr=expr,
					macro_targets=targets,
					source_relation_ids=tuple(sorted(cast(list[str], payload["relation_ids"]))),
					confidence=min(cast(list[float], payload["confidences"]) or [0.88]),
				)
			)
		results[entity_id] = tuple(
			sorted(
				fragments,
				key=lambda item: (
					"" if item.expr is None else item.expr,
					tuple(target.dst_entity_id for target in item.macro_targets),
				),
			)
		)
	return results


def _build_module_parent_map(
	relations: Sequence[RelationRecord],
	entity_by_id: Mapping[str, EntityRecord],
) -> dict[str, tuple[str, ...]]:
	grouped: dict[str, list[str]] = defaultdict(list)
	for relation in relations:
		if relation.relation_type != "belongs_to_module":
			continue
		src = entity_by_id.get(relation.src_entity_id)
		dst = entity_by_id.get(relation.dst_entity_id)
		if src is None or dst is None:
			continue
		if src.entity_type != "File" or dst.entity_type != "Module":
			continue
		grouped[src.entity_id].append(dst.entity_id)
	return {
		entity_id: tuple(sorted(_unique_in_order(parent_ids)))
		for entity_id, parent_ids in grouped.items()
	}


def _build_file_parent_map(
	relations: Sequence[RelationRecord],
	entity_by_id: Mapping[str, EntityRecord],
) -> dict[str, tuple[str, ...]]:
	grouped: dict[str, list[str]] = defaultdict(list)
	for relation in relations:
		if relation.relation_type != "defines":
			continue
		src = entity_by_id.get(relation.src_entity_id)
		dst = entity_by_id.get(relation.dst_entity_id)
		if src is None or dst is None:
			continue
		if src.entity_type != "File" or dst.entity_type in {"File", "Module", "Directory"}:
			continue
		grouped[dst.entity_id].append(src.entity_id)
	return {
		entity_id: tuple(sorted(_unique_in_order(parent_ids)))
		for entity_id, parent_ids in grouped.items()
	}


def _resolve_effective_conditions(
	*,
	entity_by_id: Mapping[str, EntityRecord],
	direct_conditions: Mapping[str, tuple[_ConditionFragment, ...]],
	file_parents: Mapping[str, tuple[str, ...]],
) -> dict[str, _ConditionFragment]:
	resolved: dict[str, _ConditionFragment] = {}
	in_progress: set[str] = set()

	def resolve(entity_id: str) -> _ConditionFragment:
		if entity_id in resolved:
			return resolved[entity_id]
		if entity_id in in_progress:
			return _ConditionFragment(
				expr=None,
				macro_targets=(),
				source_relation_ids=(),
				confidence=1.0,
			)
		in_progress.add(entity_id)
		parts: list[_ConditionFragment] = []
		for parent_id in file_parents.get(entity_id, ()):
			parent_condition = resolve(parent_id)
			if parent_condition.has_condition:
				parts.append(
					_ConditionFragment(
						expr=parent_condition.expr,
						macro_targets=parent_condition.macro_targets,
						source_relation_ids=parent_condition.source_relation_ids,
						confidence=parent_condition.confidence,
						inherited_from=tuple(
							_unique_in_order((parent_id, *parent_condition.inherited_from))
						),
					)
				)
		parts.extend(direct_conditions.get(entity_id, ()))
		combined = _combine_condition_fragments(parts)
		resolved[entity_id] = combined
		in_progress.remove(entity_id)
		return combined

	for entity_id in entity_by_id:
		resolve(entity_id)
	return resolved


def _combine_condition_fragments(fragments: Sequence[_ConditionFragment]) -> _ConditionFragment:
	exprs = _unique_in_order(fragment.expr for fragment in fragments if fragment.expr)
	macro_targets = _unique_targets(
		target
		for fragment in fragments
		for target in fragment.macro_targets
	)
	relation_ids = _unique_in_order(
		relation_id
		for fragment in fragments
		for relation_id in fragment.source_relation_ids
	)
	inherited_from = _unique_in_order(
		entity_id
		for fragment in fragments
		for entity_id in fragment.inherited_from
	)
	confidences = [fragment.confidence for fragment in fragments if fragment.has_condition]
	return _ConditionFragment(
		expr=" && ".join(exps) if (exps := tuple(exprs)) else None,
		macro_targets=tuple(macro_targets),
		source_relation_ids=tuple(relation_ids),
		confidence=min(confidences) if confidences else 1.0,
		inherited_from=tuple(inherited_from),
	)


def _evaluate_condition(
	condition_expr: str | None,
	*,
	condition_macros: Sequence[str],
	assignments: Mapping[str, ProfileMacroAssignment],
) -> _ConditionEvaluation:
	expr = condition_expr or " && ".join(condition_macros)
	if not expr:
		return _ConditionEvaluation(status="enabled", unknown_macros=())
	result = _eval_expr(expr, assignments)
	unknown_macros = tuple(
		sorted(
			{
				macro_name
				for macro_name in condition_macros
				if macro_name not in assignments or assignments[macro_name].enabled is None
			}
		)
	)
	if result is True:
		return _ConditionEvaluation(status="enabled", unknown_macros=unknown_macros)
	if result is False:
		return _ConditionEvaluation(status="disabled", unknown_macros=unknown_macros)
	return _ConditionEvaluation(status="unknown", unknown_macros=unknown_macros)


def _eval_expr(expr: str, assignments: Mapping[str, ProfileMacroAssignment]) -> bool | None:
	normalized = _strip_balanced_parens(expr.strip())
	if not normalized:
		return None
	if (parts := _split_top_level(normalized, "||")) is not None:
		values = [_eval_expr(part, assignments) for part in parts]
		if any(value is True for value in values):
			return True
		if all(value is False for value in values):
			return False
		return None
	if (parts := _split_top_level(normalized, "&&")) is not None:
		values = [_eval_expr(part, assignments) for part in parts]
		if any(value is False for value in values):
			return False
		if all(value is True for value in values):
			return True
		return None
	if normalized.startswith("not "):
		value = _eval_expr(normalized[4:].strip(), assignments)
		return None if value is None else not value
	if normalized.startswith("ifeq(") and normalized.endswith(")"):
		return _eval_equality_call(normalized[5:-1], assignments, negate=False)
	if normalized.startswith("ifneq(") and normalized.endswith(")"):
		return _eval_equality_call(normalized[6:-1], assignments, negate=True)
	return _eval_macro_token(normalized, assignments)


def _eval_equality_call(
	inner: str,
	assignments: Mapping[str, ProfileMacroAssignment],
	*,
	negate: bool,
) -> bool | None:
	args = _split_args(inner)
	if len(args) != 2:
		return None
	left = _resolve_operand(args[0], assignments)
	right = _resolve_operand(args[1], assignments)
	if left is None or right is None:
		return None
	result = left == right
	return not result if negate else result


def _resolve_operand(token: str, assignments: Mapping[str, ProfileMacroAssignment]) -> str | None:
	value = token.strip()
	if not value:
		return None
	if match := _MACRO_REF_RE.match(value):
		macro_name = match.group("macro")
		assignment = assignments.get(macro_name)
		return None if assignment is None or assignment.value is None else assignment.value
	if (value.startswith('"') and value.endswith('"')) or (
		value.startswith("'") and value.endswith("'")
	):
		return value[1:-1]
	if value in assignments:
		assignment = assignments[value]
		return assignment.value
	return value


def _eval_macro_token(token: str, assignments: Mapping[str, ProfileMacroAssignment]) -> bool | None:
	value = token.strip()
	if match := _MACRO_REF_RE.match(value):
		return _macro_truth(assignments.get(match.group("macro")))
	if not _IDENT_RE.fullmatch(value):
		return None
	return _macro_truth(assignments.get(value))


def _macro_truth(assignment: ProfileMacroAssignment | None) -> bool | None:
	if assignment is None or assignment.enabled is None:
		return None
	if not assignment.enabled:
		return False
	value = "" if assignment.value is None else assignment.value.strip().strip('"').strip("'")
	if not value:
		return True
	return value.lower() not in {"0", "false", "n", "off", "no"}


def _split_args(value: str) -> tuple[str, ...]:
	depth = 0
	current: list[str] = []
	parts: list[str] = []
	for char in value:
		if char == "(":
			depth += 1
		elif char == ")":
			depth -= 1
		if char == "," and depth == 0:
			parts.append("".join(current).strip())
			current = []
			continue
		current.append(char)
	if current:
		parts.append("".join(current).strip())
	return tuple(parts)


def _split_top_level(value: str, operator: str) -> tuple[str, ...] | None:
	depth = 0
	parts: list[str] = []
	start = 0
	index = 0
	while index < len(value):
		char = value[index]
		if char == "(":
			depth += 1
		elif char == ")":
			depth -= 1
		elif depth == 0 and value.startswith(operator, index):
			parts.append(value[start:index].strip())
			index += len(operator)
			start = index
			continue
		index += 1
	if not parts:
		return None
	parts.append(value[start:].strip())
	return tuple(part for part in parts if part)


def _strip_balanced_parens(value: str) -> str:
	current = value.strip()
	while current.startswith("(") and current.endswith(")"):
		depth = 0
		wrapped = True
		for index, char in enumerate(current):
			if char == "(":
				depth += 1
			elif char == ")":
				depth -= 1
				if depth == 0 and index != len(current) - 1:
					wrapped = False
					break
		if not wrapped:
			break
		current = current[1:-1].strip()
	return current


def _conditioned_confidence(
	entity: EntityRecord,
	profile: ProfileRecord,
	effective: _ConditionFragment,
	evaluation: _ConditionEvaluation,
) -> float:
	profile_confidence = _metadata_float(profile.metadata, "confidence") or 1.0
	confidence = min(profile_confidence, effective.confidence)
	if effective.inherited_from:
		confidence = min(confidence, 0.91)
	if evaluation.status == "unknown":
		confidence = min(confidence, 0.65)
	if entity.entity_type == "Module":
		confidence = min(confidence, 0.95)
	return round(confidence, 2)


def _profile_conditioned_relation_id(
	*,
	src_entity_id: str,
	dst_entity_id: str,
	profile_id: str,
) -> str:
	return f"relation:profile:{profile_id}:{src_entity_id}:{dst_entity_id}"


def _is_conditionable_entity(entity: EntityRecord) -> bool:
	if entity.entity_type == "Directory":
		return False
	return entity.metadata.get("macro_role") != "guard"


def _metadata_text(metadata: Mapping[str, object], key: str) -> str | None:
	value = metadata.get(key)
	return None if value is None else str(value)


def _metadata_float(metadata: Mapping[str, object], key: str) -> float | None:
	value = metadata.get(key)
	if value is None:
		return None
	if isinstance(value, bool):
		return float(value)
	if isinstance(value, (int, float)):
		return float(value)
	if isinstance(value, str):
		try:
			return float(value)
		except ValueError:
			return None
	return None


def _metadata_bool(metadata: Mapping[str, object], key: str) -> bool | None:
	value = metadata.get(key)
	if value is None:
		return None
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	if isinstance(value, str):
		normalized = value.strip().lower()
		if normalized in {"true", "1", "yes", "y", "on"}:
			return True
		if normalized in {"false", "0", "no", "n", "off"}:
			return False
	return None


def _metadata_strings(metadata: Mapping[str, object], key: str) -> tuple[str, ...]:
	value = metadata.get(key)
	if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
		return ()
	return tuple(str(item) for item in value if item is not None)


def _unique_targets(targets: Iterable[_MacroTarget]) -> list[_MacroTarget]:
	ordered: list[_MacroTarget] = []
	seen: set[tuple[str, str]] = set()
	for target in targets:
		key = (target.macro_name, target.dst_entity_id)
		if key in seen:
			continue
		seen.add(key)
		ordered.append(target)
	return ordered


def _unique_in_order(values: Iterable[object]) -> list[str]:
	ordered: list[str] = []
	seen: set[str] = set()
	for value in values:
		if value is None:
			continue
		text = str(value)
		if text in seen:
			continue
		seen.add(text)
		ordered.append(text)
	return ordered
