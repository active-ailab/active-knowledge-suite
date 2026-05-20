"""Profile indexing boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig, KnownProfileConfig
from active_knowledge_server.connectors import BuildArtifactEntry, BuildOutputsConnector, BuildOutputsManifest
from active_knowledge_server.parsers import KconfigParseWarning, ParsedProfileConfig, parse_profile_config
from active_knowledge_server.security.path_guard import PathGuard
from active_knowledge_server.storage import ProfileRecord, StorageWriter

PROFILE_COLLECTOR_SCHEMA_VERSION: Final = "profile_collector.v1"
_MISSING_HASH: Final = "missing"
_AUTO_ELIGIBLE_CONFIDENCE: Final = 0.80
_SOURCE_PRIORITY: Final[dict[str, int]] = {
	"manual_seed": 3,
	"dotconfig_scan": 2,
	"defconfig_scan": 1,
}
_STATUS_ORDER: Final[dict[str, int]] = {
	"candidate": 0,
	"resolved": 0,
	"stale": 1,
	"unresolved": 2,
	"invalid": 3,
}


@dataclass(frozen=True)
class ProfileCollectorWarning:
	"""Non-fatal profile discovery or resolution warning."""

	code: str
	message: str
	level: str = "caution"
	details: Mapping[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable warning payload."""

		return {
			"level": self.level,
			"code": self.code,
			"message": self.message,
			"details": dict(self.details),
		}


@dataclass(frozen=True)
class ProfileCandidate:
	"""One discovered or configured profile candidate."""

	snapshot_id: str
	profile_record_id: str
	profile_id: str
	defconfig_hash: str
	dotconfig_hash: str
	defconfig_path: str | None = None
	dotconfig_path: str | None = None
	app: str | None = None
	board: str | None = None
	source: str = "dotconfig_scan"
	priority: int | None = None
	last_used_at: str | None = None
	match_reason: str | None = None
	confidence: float = 0.0
	status: str = "candidate"
	config_hash: str | None = None
	macro_summary_hash: str | None = None
	warnings: tuple[ProfileCollectorWarning, ...] = ()
	metadata: Mapping[str, object] = field(default_factory=dict)
	mtime: float | None = None

	@property
	def auto_eligible(self) -> bool:
		"""Return whether the candidate is trusted enough for auto resolution."""

		return (
			self.dotconfig_path is not None
			and self.status == "candidate"
			and self.confidence >= _AUTO_ELIGIBLE_CONFIDENCE
		)

	def to_profile_record(self, *, manifest_hash: str) -> ProfileRecord:
		"""Convert the candidate into the stable storage record shape."""

		metadata = {
			"collector_schema_version": PROFILE_COLLECTOR_SCHEMA_VERSION,
			"source": self.source,
			"priority": self.priority,
			"status": self.status,
			"confidence": self.confidence,
			"match_reason": self.match_reason,
			"last_used_at": self.last_used_at,
			"config_hash": self.config_hash,
			"macro_summary_hash": self.macro_summary_hash,
			"profile_manifest_hash": manifest_hash,
			"warnings": [warning.to_dict() for warning in self.warnings],
			**dict(self.metadata),
		}
		return ProfileRecord(
			profile_record_id=self.profile_record_id,
			snapshot_id=self.snapshot_id,
			profile_id=self.profile_id,
			defconfig_hash=self.defconfig_hash,
			dotconfig_hash=self.dotconfig_hash,
			defconfig_path=self.defconfig_path,
			dotconfig_path=self.dotconfig_path,
			app=self.app,
			board=self.board,
			metadata={key: value for key, value in metadata.items() if value is not None},
		)

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable candidate payload."""

		return {
			"profile_id": self.profile_id,
			"profile_record_id": self.profile_record_id,
			"app": self.app,
			"board": self.board,
			"defconfig_path": self.defconfig_path,
			"dotconfig_path": self.dotconfig_path,
			"defconfig_hash": self.defconfig_hash,
			"dotconfig_hash": self.dotconfig_hash,
			"source": self.source,
			"priority": self.priority,
			"last_used_at": self.last_used_at,
			"match_reason": self.match_reason,
			"confidence": self.confidence,
			"status": self.status,
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


@dataclass(frozen=True)
class ProfileResolution:
	"""Deterministic resolution result for one requested profile context."""

	requested: str
	status: str
	resolved_profile_id: str | None = None
	profile_record_id: str | None = None
	source: str | None = None
	confidence: float | None = None
	candidates: tuple[ProfileCandidate, ...] = ()
	warnings: tuple[ProfileCollectorWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable resolution payload."""

		return {
			"requested": self.requested,
			"status": self.status,
			"resolved_profile_id": self.resolved_profile_id,
			"profile_record_id": self.profile_record_id,
			"source": self.source,
			"confidence": self.confidence,
			"candidates": [candidate.to_dict() for candidate in self.candidates],
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


@dataclass(frozen=True)
class CollectedProfiles:
	"""Collected profile records plus the current auto-resolution result."""

	schema_version: str
	snapshot_id: str
	manifest_hash: str
	profile_records: tuple[ProfileRecord, ...]
	resolution: ProfileResolution
	warnings: tuple[ProfileCollectorWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable collection payload."""

		return {
			"schema_version": self.schema_version,
			"snapshot_id": self.snapshot_id,
			"manifest_hash": self.manifest_hash,
			"profile_count": len(self.profile_records),
			"profiles": [profile_record_to_dict(record) for record in self.profile_records],
			"resolution": self.resolution.to_dict(),
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


@dataclass(frozen=True)
class _ConfigArtifactState:
	relative_path: str
	content_hash: str
	text: str
	mtime: float | None
	parsed_profile: ParsedProfileConfig


class ProfileCollector:
	"""Discover snapshot-bound profiles and resolve the active profile deterministically."""

	def __init__(
		self,
		config: ActiveKnowledgeConfig,
		*,
		cwd: Path | None = None,
		build_outputs_connector: BuildOutputsConnector | None = None,
		guard: PathGuard | None = None,
	) -> None:
		self._config = config
		self._cwd = (cwd or Path.cwd()).expanduser()
		self._workspace_root = resolve_runtime_path(config.project.workspace_root, self._cwd)
		self._guard = guard or PathGuard.from_config(config, cwd=self._cwd)
		self._build_outputs = build_outputs_connector or BuildOutputsConnector.from_config(
			config,
			cwd=self._cwd,
			guard=self._guard,
		)

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		cwd: Path | None = None,
		build_outputs_connector: BuildOutputsConnector | None = None,
		guard: PathGuard | None = None,
	) -> ProfileCollector:
		"""Build a profile collector from validated config."""

		return cls(
			config,
			cwd=cwd,
			build_outputs_connector=build_outputs_connector,
			guard=guard,
		)

	def collect(
		self,
		snapshot_id: str | None = None,
		*,
		requested_profile_id: str | None = None,
		build_outputs_manifest: BuildOutputsManifest | None = None,
		client_context: Mapping[str, object] | None = None,
	) -> CollectedProfiles:
		"""Collect profile records and the deterministic resolution result."""

		resolved_snapshot_id = snapshot_id or self._config.project.default_snapshot
		manifest = build_outputs_manifest or self._build_outputs.scan()
		candidates, warnings = self._discover_candidates(
			snapshot_id=resolved_snapshot_id,
			manifest=manifest,
		)
		sorted_candidates = tuple(
			sorted(
				candidates,
				key=lambda item: candidate_sort_key(item, client_context=client_context),
			)
		)
		manifest_hash = compute_profile_manifest_hash(sorted_candidates)
		profile_records = tuple(
			candidate.to_profile_record(manifest_hash=manifest_hash)
			for candidate in sorted_candidates
		)
		resolution = self._resolve_requested_profile(
			snapshot_id=resolved_snapshot_id,
			requested_profile_id=requested_profile_id,
			candidates=sorted_candidates,
		)
		return CollectedProfiles(
			schema_version=PROFILE_COLLECTOR_SCHEMA_VERSION,
			snapshot_id=resolved_snapshot_id,
			manifest_hash=manifest_hash,
			profile_records=profile_records,
			resolution=resolution,
			warnings=warnings,
		)

	def collect_and_store(
		self,
		writer: StorageWriter,
		snapshot_id: str | None = None,
		*,
		requested_profile_id: str | None = None,
		build_outputs_manifest: BuildOutputsManifest | None = None,
		client_context: Mapping[str, object] | None = None,
	) -> CollectedProfiles:
		"""Collect profile records and persist them through the storage writer."""

		collected = self.collect(
			snapshot_id=snapshot_id,
			requested_profile_id=requested_profile_id,
			build_outputs_manifest=build_outputs_manifest,
			client_context=client_context,
		)
		for record in collected.profile_records:
			writer.upsert_profile(record)
		return collected

	def _discover_candidates(
		self,
		*,
		snapshot_id: str,
		manifest: BuildOutputsManifest,
	) -> tuple[tuple[ProfileCandidate, ...], tuple[ProfileCollectorWarning, ...]]:
		warnings = tuple(
			ProfileCollectorWarning(
				code=warning.code,
				message=warning.message,
				level="warning",
				details={
					"display_path": warning.display_path,
					**dict(warning.details),
				},
			)
			for warning in manifest.warnings
		)
		defconfig_states = {
			entry.relative_path: state
			for entry in manifest.defconfigs
			if (state := self._load_artifact_state(entry, artifact_kind="defconfig")) is not None
		}
		dotconfig_states = {
			entry.relative_path: state
			for entry in manifest.dotconfigs
			if (state := self._load_artifact_state(entry, artifact_kind="dotconfig")) is not None
		}

		candidates_by_id: dict[str, ProfileCandidate] = {}
		signature_to_id: dict[tuple[str | None, str | None, str | None, str | None], str] = {}
		matched_defconfigs: set[str] = set()

		for seed in self._config.profiles.known:
			candidate = self._candidate_from_known_seed(
				snapshot_id=snapshot_id,
				seed=seed,
				defconfig_states=defconfig_states,
				dotconfig_states=dotconfig_states,
			)
			if candidate is None:
				continue
			add_profile_candidate(
				candidates_by_id,
				signature_to_id,
				candidate,
			)

		for dotconfig_path in sorted(dotconfig_states):
			dotconfig_state = dotconfig_states[dotconfig_path]
			matched_defconfig = self._match_defconfig(
				dotconfig_state,
				defconfig_states=defconfig_states,
			)
			if matched_defconfig is not None:
				matched_defconfigs.add(matched_defconfig.relative_path)
			candidate = self._build_candidate(
				snapshot_id=snapshot_id,
				defconfig_state=matched_defconfig,
				dotconfig_state=dotconfig_state,
				source="dotconfig_scan",
				priority=None,
				match_reason=(
					"paired dotconfig candidate with matching defconfig"
					if matched_defconfig is not None
					else "dotconfig candidate from configured path"
				),
			)
			add_profile_candidate(
				candidates_by_id,
				signature_to_id,
				candidate,
			)

		for defconfig_path in sorted(defconfig_states):
			if defconfig_path in matched_defconfigs:
				continue
			candidate = self._build_candidate(
				snapshot_id=snapshot_id,
				defconfig_state=defconfig_states[defconfig_path],
				dotconfig_state=None,
				source="defconfig_scan",
				priority=None,
				match_reason="discovered defconfig without current dotconfig",
			)
			add_profile_candidate(
				candidates_by_id,
				signature_to_id,
				candidate,
			)

		return tuple(candidates_by_id.values()), warnings

	def _candidate_from_known_seed(
		self,
		*,
		snapshot_id: str,
		seed: KnownProfileConfig,
		defconfig_states: Mapping[str, _ConfigArtifactState],
		dotconfig_states: Mapping[str, _ConfigArtifactState],
	) -> ProfileCandidate | None:
		defconfig_state = None if seed.defconfig is None else defconfig_states.get(seed.defconfig)
		dotconfig_state = None if seed.dotconfig is None else dotconfig_states.get(seed.dotconfig)
		if seed.defconfig is not None and defconfig_state is None:
			defconfig_state = self._load_config_state(seed.defconfig, artifact_kind="defconfig")
		if seed.dotconfig is not None and dotconfig_state is None:
			dotconfig_state = self._load_config_state(seed.dotconfig, artifact_kind="dotconfig")
		if defconfig_state is None and dotconfig_state is None:
			return None
		return self._build_candidate(
			snapshot_id=snapshot_id,
			defconfig_state=defconfig_state,
			dotconfig_state=dotconfig_state,
			source="manual_seed",
			priority=seed.priority,
			match_reason="matched configured known profile seed",
			override_profile_id=seed.id,
			override_app=seed.app,
			override_board=seed.board,
		)

	def _match_defconfig(
		self,
		dotconfig_state: _ConfigArtifactState,
		*,
		defconfig_states: Mapping[str, _ConfigArtifactState],
	) -> _ConfigArtifactState | None:
		best_match: _ConfigArtifactState | None = None
		best_score = 0
		dotconfig_clues = dotconfig_state.parsed_profile.clues
		dotconfig_profile_id = derive_profile_id(
			app=dotconfig_clues.app,
			board=dotconfig_clues.board,
			fallback_path=dotconfig_state.relative_path,
		)
		for defconfig_state in defconfig_states.values():
			defconfig_clues = defconfig_state.parsed_profile.clues
			defconfig_profile_id = derive_profile_id(
				app=defconfig_clues.app,
				board=defconfig_clues.board,
				fallback_path=defconfig_state.relative_path,
			)
			score = 0
			if (
				dotconfig_clues.board is not None
				and defconfig_clues.board is not None
				and dotconfig_clues.board == defconfig_clues.board
			):
				score += 6
			if (
				dotconfig_clues.app is not None
				and defconfig_clues.app is not None
				and dotconfig_clues.app == defconfig_clues.app
			):
				score += 4
			if dotconfig_profile_id == defconfig_profile_id:
				score += 3
			if score <= 0:
				continue
			if score > best_score:
				best_match = defconfig_state
				best_score = score
				continue
			if score == best_score and best_match is not None:
				if defconfig_state.relative_path < best_match.relative_path:
					best_match = defconfig_state
		return best_match

	def _resolve_requested_profile(
		self,
		*,
		snapshot_id: str,
		requested_profile_id: str | None,
		candidates: tuple[ProfileCandidate, ...],
	) -> ProfileResolution:
		requested = requested_profile_id or self._config.project.default_profile
		if requested and requested != "auto":
			matching_candidates = tuple(
				candidate for candidate in candidates if candidate.profile_id == requested
			)
			if len(matching_candidates) == 1:
				candidate = matching_candidates[0]
				return ProfileResolution(
					requested=requested,
					status="resolved",
					resolved_profile_id=candidate.profile_id,
					profile_record_id=candidate.profile_record_id,
					source="explicit" if requested_profile_id is not None else "local_config",
					confidence=max(candidate.confidence, 0.99),
				)
			warning = ProfileCollectorWarning(
				code="profile.invalid",
				message="The requested profile could not be resolved for the current snapshot.",
				details={
					"requested_profile_id": requested,
					"snapshot_id": snapshot_id,
				},
			)
			return ProfileResolution(
				requested=requested,
				status="invalid",
				candidates=matching_candidates,
				warnings=(warning,),
			)

		auto_candidates = tuple(candidate for candidate in candidates if candidate.auto_eligible)
		if len(auto_candidates) == 1:
			candidate = auto_candidates[0]
			return ProfileResolution(
				requested="auto",
				status="resolved",
				resolved_profile_id=candidate.profile_id,
				profile_record_id=candidate.profile_record_id,
				source=candidate.source,
				confidence=candidate.confidence,
			)
		if len(auto_candidates) > 1:
			warning = ProfileCollectorWarning(
				code="profile.multiple_candidates",
				message="Multiple profile candidates were found; no profile was selected automatically.",
				details={
					"snapshot_id": snapshot_id,
					"candidate_count": len(auto_candidates),
				},
			)
			return ProfileResolution(
				requested="auto",
				status="multiple_candidates",
				candidates=auto_candidates,
				warnings=(warning,),
			)

		baseline_default = read_baseline_default_profile(self._config, cwd=self._cwd)
		if baseline_default:
			baseline_matches = tuple(
				candidate for candidate in candidates if candidate.profile_id == baseline_default
			)
			if len(baseline_matches) == 1:
				candidate = baseline_matches[0]
				return ProfileResolution(
					requested="auto",
					status="resolved",
					resolved_profile_id=candidate.profile_id,
					profile_record_id=candidate.profile_record_id,
					source="baseline_default",
					confidence=max(candidate.confidence, 0.75),
				)

		warning = ProfileCollectorWarning(
			code="profile.unresolved",
			message="No unique profile candidate could be resolved automatically.",
			details={
				"snapshot_id": snapshot_id,
				"candidate_count": len(candidates),
			},
		)
		return ProfileResolution(
			requested="auto",
			status="unresolved",
			candidates=tuple(candidate for candidate in candidates if candidate.status != "invalid"),
			warnings=(warning,),
		)

	def _build_candidate(
		self,
		*,
		snapshot_id: str,
		defconfig_state: _ConfigArtifactState | None,
		dotconfig_state: _ConfigArtifactState | None,
		source: str,
		priority: int | None,
		match_reason: str,
		override_profile_id: str | None = None,
		override_app: str | None = None,
		override_board: str | None = None,
	) -> ProfileCandidate:
		parsed_profile = parse_profile_config(
			defconfig_path=None if defconfig_state is None else Path(defconfig_state.relative_path),
			defconfig_text=None if defconfig_state is None else defconfig_state.text,
			dotconfig_path=None if dotconfig_state is None else Path(dotconfig_state.relative_path),
			dotconfig_text=None if dotconfig_state is None else dotconfig_state.text,
		)
		app = override_app or parsed_profile.clues.app
		board = override_board or parsed_profile.clues.board
		profile_id = normalize_profile_id(
			override_profile_id
			or derive_profile_id(
				app=app,
				board=board,
				fallback_path=(
					dotconfig_state.relative_path
					if dotconfig_state is not None
					else defconfig_state.relative_path if defconfig_state is not None else "profile"
				),
			)
		)
		defconfig_hash = (
			defconfig_state.content_hash if defconfig_state is not None else _MISSING_HASH
		)
		dotconfig_hash = dotconfig_state.content_hash if dotconfig_state is not None else _MISSING_HASH
		config_hash = compute_profile_config_hash(
			defconfig_hash=defconfig_hash,
			dotconfig_hash=dotconfig_hash,
			macro_summary_hash=parsed_profile.macro_summary_hash,
		)
		status, confidence = classify_profile_candidate(
			has_dotconfig=dotconfig_state is not None,
			app=app,
			board=board,
			has_parse_warnings=bool(parsed_profile.warnings),
		)
		candidate_warnings = tuple(
			warning_from_kconfig_warning(warning)
			for warning in parsed_profile.warnings
		)
		metadata = {
			"macro_summary": {
				"app": parsed_profile.clues.app,
				"board": parsed_profile.clues.board,
				"features": list(parsed_profile.clues.features),
				"app_candidates": list(parsed_profile.clues.app_candidates),
				"board_candidates": list(parsed_profile.clues.board_candidates),
				"assignment_count": len(parsed_profile.merged_assignments),
			},
		}
		return ProfileCandidate(
			snapshot_id=snapshot_id,
			profile_record_id=compute_profile_record_id(
				snapshot_id=snapshot_id,
				profile_id=profile_id,
				defconfig_hash=defconfig_hash,
				dotconfig_hash=dotconfig_hash,
			),
			profile_id=profile_id,
			defconfig_hash=defconfig_hash,
			dotconfig_hash=dotconfig_hash,
			defconfig_path=None if defconfig_state is None else defconfig_state.relative_path,
			dotconfig_path=None if dotconfig_state is None else dotconfig_state.relative_path,
			app=app,
			board=board,
			source=source,
			priority=priority,
			last_used_at=None,
			match_reason=match_reason,
			confidence=confidence,
			status=status,
			config_hash=config_hash,
			macro_summary_hash=parsed_profile.macro_summary_hash,
			warnings=candidate_warnings,
			metadata=metadata,
			mtime=(
				dotconfig_state.mtime
				if dotconfig_state is not None
				else None if defconfig_state is None else defconfig_state.mtime
			),
		)

	def _load_artifact_state(
		self,
		entry: BuildArtifactEntry,
		*,
		artifact_kind: str,
	) -> _ConfigArtifactState | None:
		return self._load_config_state(
			entry.relative_path,
			artifact_kind=artifact_kind,
			content_hash=entry.content_hash,
		)

	def _load_config_state(
		self,
		relative_path: str,
		*,
		artifact_kind: str,
		content_hash: str | None = None,
	) -> _ConfigArtifactState | None:
		guarded = self._guard.guard(self._workspace_root / relative_path, must_exist=True)
		try:
			text = guarded.real_path.read_text(encoding="utf-8")
		except (OSError, UnicodeDecodeError):
			return None
		try:
			mtime = guarded.real_path.stat().st_mtime
		except OSError:
			mtime = None
		parsed_profile = (
			parse_profile_config(defconfig_path=Path(relative_path), defconfig_text=text)
			if artifact_kind == "defconfig"
			else parse_profile_config(dotconfig_path=Path(relative_path), dotconfig_text=text)
		)
		return _ConfigArtifactState(
			relative_path=relative_path,
			content_hash=content_hash or file_hash_for_text(text),
			text=text,
			mtime=mtime,
			parsed_profile=parsed_profile,
		)


def compute_profile_record_id(
	*,
	snapshot_id: str,
	profile_id: str,
	defconfig_hash: str,
	dotconfig_hash: str,
) -> str:
	"""Return the stable physical record ID for one snapshot-bound profile."""

	identity_key = f"{snapshot_id}|{profile_id}|{defconfig_hash}|{dotconfig_hash}"
	digest = hashlib.sha1(identity_key.encode("utf-8")).hexdigest()
	return f"profile:{digest}"


def compute_profile_manifest_hash(candidates: tuple[ProfileCandidate, ...]) -> str:
	"""Return a deterministic manifest hash for the collected profile set."""

	payload = [candidate.to_dict() for candidate in sorted(candidates, key=lambda item: item.profile_record_id)]
	return stable_hash(payload)


def compute_profile_config_hash(
	*,
	defconfig_hash: str,
	dotconfig_hash: str,
	macro_summary_hash: str,
) -> str:
	"""Return one combined hash for profile-affecting config state."""

	return stable_hash(
		{
			"defconfig_hash": defconfig_hash,
			"dotconfig_hash": dotconfig_hash,
			"macro_summary_hash": macro_summary_hash,
		}
	)


def read_baseline_default_profile(config: ActiveKnowledgeConfig, *, cwd: Path) -> str | None:
	"""Read the baseline default profile from the baseline manifest when present."""

	manifest_path = resolve_runtime_path(config.storage.baseline.manifest, cwd)
	if not manifest_path.exists():
		return None
	try:
		payload = json.loads(manifest_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return None
	if not isinstance(payload, dict):
		return None
	value = payload.get("default_profile")
	return None if value is None else normalize_profile_id(str(value))


def classify_profile_candidate(
	*,
	has_dotconfig: bool,
	app: str | None,
	board: str | None,
	has_parse_warnings: bool,
) -> tuple[str, float]:
	"""Classify candidate quality for sorting and auto-resolution."""

	if app and board and has_dotconfig:
		return ("candidate", 0.88 if has_parse_warnings else 0.93)
	if app and board:
		return ("candidate", 0.72 if has_parse_warnings else 0.76)
	if app or board:
		return ("stale", 0.60 if has_dotconfig else 0.55)
	return ("invalid", 0.30)


def candidate_sort_key(
	candidate: ProfileCandidate,
	*,
	client_context: Mapping[str, object] | None,
) -> tuple[object, ...]:
	"""Return the stable candidate sort key from contract Appendix C."""

	priority = candidate.priority if candidate.priority is not None else 10_000
	relevance = candidate_context_relevance(candidate, client_context=client_context)
	mtime = candidate.mtime if candidate.mtime is not None else 0.0
	return (
		priority,
		_STATUS_ORDER.get(candidate.status, 99),
		-relevance,
		-mtime,
		candidate.profile_id,
		candidate.dotconfig_path or "",
		candidate.defconfig_path or "",
	)


def candidate_context_relevance(
	candidate: ProfileCandidate,
	*,
	client_context: Mapping[str, object] | None,
) -> int:
	"""Score how well a candidate matches active file or cwd hints."""

	if not client_context:
		return 0
	score = 0
	for key in ("active_file", "cwd"):
		raw_value = client_context.get(key)
		if not isinstance(raw_value, str):
			continue
		normalized = normalize_profile_id(raw_value)
		if candidate.app and candidate.app in normalized:
			score += 2
		if candidate.board and candidate.board in normalized:
			score += 2
		if candidate.profile_id and candidate.profile_id in normalized:
			score += 1
	return score


def derive_profile_id(*, app: str | None, board: str | None, fallback_path: str) -> str:
	"""Derive one stable human-readable profile ID."""

	if board and app:
		return f"{normalize_profile_id(board)}_{normalize_profile_id(app)}"
	if board:
		return normalize_profile_id(board)
	if app:
		return normalize_profile_id(app)
	fallback_name = Path(fallback_path).name
	if fallback_name.endswith("_defconfig"):
		fallback_name = fallback_name[: -len("_defconfig")]
	return normalize_profile_id(fallback_name or "profile")


def normalize_profile_id(value: str) -> str:
	"""Normalize a profile ID or clue to a stable snake-case identifier."""

	normalized = [character.lower() if character.isalnum() else "_" for character in value.strip()]
	collapsed = "".join(normalized).strip("_")
	while "__" in collapsed:
		collapsed = collapsed.replace("__", "_")
	return collapsed or "profile"


def add_profile_candidate(
	candidates_by_id: dict[str, ProfileCandidate],
	signature_to_id: dict[tuple[str | None, str | None, str | None, str | None], str],
	candidate: ProfileCandidate,
) -> None:
	"""Insert or merge a candidate while preserving the richer duplicate."""

	signature = (
		candidate.dotconfig_path,
		candidate.defconfig_path,
		candidate.app,
		candidate.board,
	)
	existing_id = signature_to_id.get(signature)
	if existing_id is not None and existing_id in candidates_by_id:
		preferred = prefer_profile_candidate(candidates_by_id[existing_id], candidate)
		candidates_by_id[preferred.profile_record_id] = preferred
		if preferred.profile_record_id != existing_id:
			del candidates_by_id[existing_id]
		signature_to_id[signature] = preferred.profile_record_id
		return
	existing = candidates_by_id.get(candidate.profile_record_id)
	if existing is not None:
		candidates_by_id[candidate.profile_record_id] = prefer_profile_candidate(existing, candidate)
		signature_to_id[signature] = candidate.profile_record_id
		return
	candidates_by_id[candidate.profile_record_id] = candidate
	signature_to_id[signature] = candidate.profile_record_id


def prefer_profile_candidate(current: ProfileCandidate, candidate: ProfileCandidate) -> ProfileCandidate:
	"""Choose the more informative duplicate candidate."""

	current_key = candidate_preference_key(current)
	candidate_key = candidate_preference_key(candidate)
	return candidate if candidate_key > current_key else current


def candidate_preference_key(candidate: ProfileCandidate) -> tuple[object, ...]:
	"""Return the merge preference tuple for duplicate candidates."""

	return (
		_SOURCE_PRIORITY.get(candidate.source, 0),
		candidate.dotconfig_path is not None,
		candidate.defconfig_path is not None,
		candidate.app is not None,
		candidate.board is not None,
		-(candidate.priority if candidate.priority is not None else 10_000),
		candidate.confidence,
	)


def warning_from_kconfig_warning(warning: KconfigParseWarning) -> ProfileCollectorWarning:
	"""Convert parser warnings into profile collector warnings."""

	details = dict(warning.details)
	if warning.line_number is not None:
		details["line_number"] = warning.line_number
	return ProfileCollectorWarning(
		code=warning.code,
		message=warning.message,
		level="warning",
		details=details,
	)


def profile_record_to_dict(record: ProfileRecord) -> dict[str, object]:
	"""Return a JSON-serializable storage record payload."""

	return {
		"profile_record_id": record.profile_record_id,
		"snapshot_id": record.snapshot_id,
		"profile_id": record.profile_id,
		"defconfig_hash": record.defconfig_hash,
		"dotconfig_hash": record.dotconfig_hash,
		"defconfig_path": record.defconfig_path,
		"dotconfig_path": record.dotconfig_path,
		"app": record.app,
		"board": record.board,
		"metadata": dict(record.metadata),
	}


def file_hash_for_text(text: str) -> str:
	"""Return the stable content hash used for directly loaded profile files."""

	digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
	return f"sha256:{digest}"


def stable_hash(payload: object) -> str:
	"""Return a stable SHA-256 hash for one JSON-serializable payload."""

	encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
	return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


