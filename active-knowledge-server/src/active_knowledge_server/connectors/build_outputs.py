"""Build output discovery connector."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.workspace import display_for_relative
from active_knowledge_server.security.path_guard import GuardedPath, PathBlockedError, PathGuard

BUILD_OUTPUTS_MANIFEST_SCHEMA_VERSION: Final = "build_outputs_manifest.v1"
_HASH_CHUNK_SIZE: Final = 1024 * 1024


@dataclass(frozen=True)
class BuildOutputsWarning:
	"""Non-fatal issue encountered while discovering build artifacts."""

	code: str
	message: str
	display_path: str
	details: Mapping[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable warning."""

		return {
			"level": "warning",
			"code": self.code,
			"message": self.message,
			"display_path": self.display_path,
			"details": dict(self.details),
		}


@dataclass(frozen=True)
class BuildArtifactEntry:
	"""One guarded build artifact discovered under the workspace."""

	relative_path: str
	display_path: str
	artifact_kind: str
	size_bytes: int
	content_hash: str | None
	is_symlink: bool = False

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable build artifact record."""

		return {
			"relative_path": self.relative_path,
			"display_path": self.display_path,
			"artifact_kind": self.artifact_kind,
			"size_bytes": self.size_bytes,
			"content_hash": self.content_hash,
			"is_symlink": self.is_symlink,
		}


@dataclass(frozen=True)
class BuildOutputsManifest:
	"""Stable build artifact manifest for one workspace scan."""

	schema_version: str
	workspace_root: str
	workspace_display_path: str
	defconfig_roots: tuple[str, ...]
	dotconfig_candidates: tuple[str, ...]
	compile_db_candidates: tuple[str, ...]
	defconfigs: tuple[BuildArtifactEntry, ...]
	dotconfigs: tuple[BuildArtifactEntry, ...]
	compile_dbs: tuple[BuildArtifactEntry, ...]
	manifest_hash: str
	warnings: tuple[BuildOutputsWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable manifest payload."""

		return {
			"schema_version": self.schema_version,
			"workspace_root": self.workspace_root,
			"workspace_display_path": self.workspace_display_path,
			"defconfig_roots": list(self.defconfig_roots),
			"dotconfig_candidates": list(self.dotconfig_candidates),
			"compile_db_candidates": list(self.compile_db_candidates),
			"defconfigs": [entry.to_dict() for entry in self.defconfigs],
			"dotconfigs": [entry.to_dict() for entry in self.dotconfigs],
			"compile_dbs": [entry.to_dict() for entry in self.compile_dbs],
			"defconfig_count": len(self.defconfigs),
			"dotconfig_count": len(self.dotconfigs),
			"compile_db_count": len(self.compile_dbs),
			"manifest_hash": self.manifest_hash,
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


class BuildOutputsConnector:
	"""Discover build-related artifacts that influence profile and code indexing."""

	def __init__(
		self,
		workspace_root: str | Path,
		guard: PathGuard,
		*,
		defconfig_roots: Iterable[str] = (),
		dotconfig_candidates: Iterable[str] = (),
		compile_db_candidates: Iterable[str] = (),
	) -> None:
		self.workspace_root = Path(workspace_root).expanduser()
		self.guard = guard
		self.defconfig_roots = normalize_relative_candidates(defconfig_roots)
		self.dotconfig_candidates = normalize_relative_candidates(dotconfig_candidates)
		self.compile_db_candidates = normalize_relative_candidates(compile_db_candidates)

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		cwd: Path | None = None,
		guard: PathGuard | None = None,
	) -> BuildOutputsConnector:
		"""Build a build-outputs connector from validated runtime config."""

		root = cwd or Path.cwd()
		return cls(
			resolve_runtime_path(config.project.workspace_root, root),
			guard or PathGuard.from_config(config, cwd=root),
			defconfig_roots=config.profiles.discovery.defconfig_roots,
			dotconfig_candidates=config.profiles.discovery.dotconfig_candidates,
			compile_db_candidates=config.indexing.code.compile_db_candidates,
		)

	def scan(self) -> BuildOutputsManifest:
		"""Scan the workspace for defconfig, .config, and compile DB artifacts."""

		warnings: list[BuildOutputsWarning] = []
		root = self.guard.guard(self.workspace_root, must_exist=True)
		if not root.normalized_path.is_dir():
			raise NotADirectoryError(f"workspace root is not a directory: {root.display_path}")

		defconfigs = self._scan_defconfigs(root=root, warnings=warnings)
		dotconfigs = self._scan_candidate_files(
			root=root,
			candidates=self.dotconfig_candidates,
			artifact_kind="dotconfig",
			warnings=warnings,
		)
		compile_dbs = self._scan_candidate_files(
			root=root,
			candidates=self.compile_db_candidates,
			artifact_kind="compile_db",
			warnings=warnings,
		)
		if not compile_dbs:
			warnings.append(
				BuildOutputsWarning(
					code="compile_db.missing",
					message=(
						"No compile_commands.json candidates were found; code indexing can "
						"continue, but cross-translation-unit accuracy will be degraded."
					),
					display_path=root.display_path,
					details={
						"candidates": list(self.compile_db_candidates),
						"non_blocking": True,
					},
				)
			)

		manifest_hash = compute_build_outputs_hash(
			defconfig_roots=self.defconfig_roots,
			dotconfig_candidates=self.dotconfig_candidates,
			compile_db_candidates=self.compile_db_candidates,
			defconfigs=defconfigs,
			dotconfigs=dotconfigs,
			compile_dbs=compile_dbs,
		)
		return BuildOutputsManifest(
			schema_version=BUILD_OUTPUTS_MANIFEST_SCHEMA_VERSION,
			workspace_root=str(root.normalized_path),
			workspace_display_path=root.display_path,
			defconfig_roots=self.defconfig_roots,
			dotconfig_candidates=self.dotconfig_candidates,
			compile_db_candidates=self.compile_db_candidates,
			defconfigs=defconfigs,
			dotconfigs=dotconfigs,
			compile_dbs=compile_dbs,
			manifest_hash=manifest_hash,
			warnings=tuple(warnings),
		)

	def _scan_defconfigs(
		self,
		*,
		root: GuardedPath,
		warnings: list[BuildOutputsWarning],
	) -> tuple[BuildArtifactEntry, ...]:
		entries: dict[str, BuildArtifactEntry] = {}
		for configured_root in self.defconfig_roots:
			guarded_root = self._guard_path(
				root.normalized_path / configured_root,
				root=root,
				relative_path=configured_root,
				warnings=warnings,
				must_exist=False,
			)
			if guarded_root is None or not guarded_root.normalized_path.exists():
				continue
			if not guarded_root.normalized_path.is_dir():
				warnings.append(
					BuildOutputsWarning(
						code="build_outputs.defconfig_root_not_dir",
						message="Configured defconfig discovery root exists but is not a directory.",
						display_path=display_for_relative(root.display_path, configured_root),
						details={"configured_root": configured_root},
					)
				)
				continue
			self._scan_defconfig_directory(
				root=root,
				current=guarded_root,
				entries=entries,
				warnings=warnings,
			)
		return tuple(entries[path] for path in sorted(entries))

	def _scan_defconfig_directory(
		self,
		*,
		root: GuardedPath,
		current: GuardedPath,
		entries: dict[str, BuildArtifactEntry],
		warnings: list[BuildOutputsWarning],
	) -> None:
		relative_path = current.normalized_path.relative_to(root.normalized_path).as_posix()
		try:
			children = sorted(current.normalized_path.iterdir(), key=lambda child: child.name)
		except OSError as exc:
			warnings.append(
				BuildOutputsWarning(
					code="build_outputs.directory_read_failed",
					message=f"Cannot read build outputs directory: {exc}",
					display_path=display_for_relative(root.display_path, relative_path),
				)
			)
			return

		for child in children:
			child_relative_path = child.relative_to(root.normalized_path).as_posix()
			guarded = self._guard_path(
				child,
				root=root,
				relative_path=child_relative_path,
				warnings=warnings,
				must_exist=True,
			)
			if guarded is None:
				continue

			try:
				lstat_result = guarded.normalized_path.lstat()
			except OSError as exc:
				warnings.append(
					BuildOutputsWarning(
						code="build_outputs.stat_failed",
						message=f"Cannot stat build outputs path: {exc}",
						display_path=display_for_relative(root.display_path, child_relative_path),
					)
				)
				continue

			is_symlink = stat.S_ISLNK(lstat_result.st_mode)
			mode = lstat_result.st_mode
			target_stat = None
			if is_symlink:
				try:
					target_stat = guarded.real_path.stat()
				except OSError as exc:
					warnings.append(
						BuildOutputsWarning(
							code="build_outputs.symlink_target_missing",
							message=f"Cannot stat build outputs symlink target: {exc}",
							display_path=display_for_relative(root.display_path, child_relative_path),
						)
					)
					continue
				mode = target_stat.st_mode

			if stat.S_ISDIR(mode):
				if is_symlink:
					warnings.append(
						BuildOutputsWarning(
							code="build_outputs.symlink_dir_skipped",
							message="Symlinked build-output directories are skipped during scan.",
							display_path=display_for_relative(root.display_path, child_relative_path),
						)
					)
					continue
				self._scan_defconfig_directory(
					root=root,
					current=guarded,
					entries=entries,
					warnings=warnings,
				)
				continue

			if not stat.S_ISREG(mode) or not guarded.normalized_path.name.endswith("defconfig"):
				continue

			entry = build_artifact_entry(
				guarded=guarded,
				root=root,
				relative_path=child_relative_path,
				artifact_kind="defconfig",
				warnings=warnings,
			)
			if entry is not None:
				entries[entry.relative_path] = entry

	def _scan_candidate_files(
		self,
		*,
		root: GuardedPath,
		candidates: tuple[str, ...],
		artifact_kind: str,
		warnings: list[BuildOutputsWarning],
	) -> tuple[BuildArtifactEntry, ...]:
		entries: list[BuildArtifactEntry] = []
		for candidate in candidates:
			guarded = self._guard_path(
				root.normalized_path / candidate,
				root=root,
				relative_path=candidate,
				warnings=warnings,
				must_exist=False,
			)
			if guarded is None or not guarded.normalized_path.exists():
				continue
			if not guarded.normalized_path.is_file():
				warnings.append(
					BuildOutputsWarning(
						code="build_outputs.candidate_not_file",
						message="Configured build artifact candidate exists but is not a file.",
						display_path=display_for_relative(root.display_path, candidate),
						details={"artifact_kind": artifact_kind, "candidate": candidate},
					)
				)
				continue
			entry = build_artifact_entry(
				guarded=guarded,
				root=root,
				relative_path=candidate,
				artifact_kind=artifact_kind,
				warnings=warnings,
			)
			if entry is not None:
				entries.append(entry)
		return tuple(sorted(entries, key=lambda entry: entry.relative_path))

	def _guard_path(
		self,
		path: Path,
		*,
		root: GuardedPath,
		relative_path: str,
		warnings: list[BuildOutputsWarning],
		must_exist: bool,
	) -> GuardedPath | None:
		try:
			return self.guard.guard(path, must_exist=must_exist)
		except PathBlockedError as exc:
			warnings.append(
				BuildOutputsWarning(
					code="security.path_blocked",
					message=exc.warning.message,
					display_path=display_for_relative(root.display_path, relative_path),
					details={"reason": exc.warning.reason},
				)
			)
			return None


def scan_build_outputs(
	workspace_root: str | Path,
	guard: PathGuard,
	*,
	defconfig_roots: Iterable[str] = (),
	dotconfig_candidates: Iterable[str] = (),
	compile_db_candidates: Iterable[str] = (),
) -> BuildOutputsManifest:
	"""Convenience wrapper for one-off build-outputs scans."""

	return BuildOutputsConnector(
		workspace_root,
		guard,
		defconfig_roots=defconfig_roots,
		dotconfig_candidates=dotconfig_candidates,
		compile_db_candidates=compile_db_candidates,
	).scan()


def build_artifact_entry(
	*,
	guarded: GuardedPath,
	root: GuardedPath,
	relative_path: str,
	artifact_kind: str,
	warnings: list[BuildOutputsWarning],
) -> BuildArtifactEntry | None:
	"""Hash one guarded build artifact into a manifest entry."""

	try:
		lstat_result = guarded.normalized_path.lstat()
	except OSError as exc:
		warnings.append(
			BuildOutputsWarning(
				code="build_outputs.stat_failed",
				message=f"Cannot stat build outputs file: {exc}",
				display_path=display_for_relative(root.display_path, relative_path),
			)
		)
		return None

	is_symlink = stat.S_ISLNK(lstat_result.st_mode)
	size_bytes = lstat_result.st_size
	if is_symlink:
		try:
			size_bytes = guarded.real_path.stat().st_size
		except OSError as exc:
			warnings.append(
				BuildOutputsWarning(
					code="build_outputs.symlink_target_missing",
					message=f"Cannot stat build outputs symlink target: {exc}",
					display_path=display_for_relative(root.display_path, relative_path),
				)
			)
			return None

	content_hash = hash_guarded_file(
		guarded=guarded,
		root=root,
		relative_path=relative_path,
		warnings=warnings,
	)
	if content_hash is None:
		return None
	return BuildArtifactEntry(
		relative_path=relative_path,
		display_path=display_for_relative(root.display_path, relative_path),
		artifact_kind=artifact_kind,
		size_bytes=size_bytes,
		content_hash=content_hash,
		is_symlink=is_symlink,
	)


def hash_guarded_file(
	*,
	guarded: GuardedPath,
	root: GuardedPath,
	relative_path: str,
	warnings: list[BuildOutputsWarning],
) -> str | None:
	"""Hash a guarded build artifact so manifests remain deterministic."""

	digest = hashlib.sha256()
	try:
		with guarded.normalized_path.open("rb") as handle:
			for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
				digest.update(chunk)
	except OSError as exc:
		warnings.append(
			BuildOutputsWarning(
				code="build_outputs.file_read_failed",
				message=f"Cannot read build outputs file: {exc}",
				display_path=display_for_relative(root.display_path, relative_path),
			)
		)
		return None
	return f"sha256:{digest.hexdigest()}"


def compute_build_outputs_hash(
	*,
	defconfig_roots: tuple[str, ...],
	dotconfig_candidates: tuple[str, ...],
	compile_db_candidates: tuple[str, ...],
	defconfigs: tuple[BuildArtifactEntry, ...],
	dotconfigs: tuple[BuildArtifactEntry, ...],
	compile_dbs: tuple[BuildArtifactEntry, ...],
) -> str:
	"""Compute a deterministic manifest hash from discovered build facts only."""

	payload = {
		"schema_version": BUILD_OUTPUTS_MANIFEST_SCHEMA_VERSION,
		"defconfig_roots": defconfig_roots,
		"dotconfig_candidates": dotconfig_candidates,
		"compile_db_candidates": compile_db_candidates,
		"defconfigs": [entry.to_dict() for entry in defconfigs],
		"dotconfigs": [entry.to_dict() for entry in dotconfigs],
		"compile_dbs": [entry.to_dict() for entry in compile_dbs],
	}
	encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
	return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def normalize_relative_candidates(candidates: Iterable[str]) -> tuple[str, ...]:
	"""Normalize candidate paths to POSIX-style workspace-relative paths."""

	normalized: list[str] = []
	for candidate in candidates:
		value = candidate.strip().replace("\\", "/")
		if value.startswith("./"):
			value = value[2:]
		value = value.strip("/")
		if value:
			normalized.append(value)
	return tuple(dict.fromkeys(normalized))
