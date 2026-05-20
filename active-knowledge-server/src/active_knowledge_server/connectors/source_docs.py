"""Knowledge source document discovery connector."""

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

SOURCE_DOCS_MANIFEST_SCHEMA_VERSION: Final = "source_docs_manifest.v1"
SUPPORTED_SOURCE_DOC_CATEGORIES: Final[tuple[str, ...]] = (
	"api",
	"widgets",
	"engineering",
	"product",
	"design",
	"project",
	"qa",
	"release",
	"learned-seeds",
)
_HASH_CHUNK_SIZE: Final = 1024 * 1024
_SOURCE_DOC_FORMATS: Final[Mapping[str, str]] = {
	".md": "markdown",
	".markdown": "markdown",
	".mdx": "markdown",
	".html": "html",
	".htm": "html",
	".rst": "restructuredtext",
	".adoc": "asciidoc",
	".asciidoc": "asciidoc",
	".txt": "text",
	".yaml": "yaml",
	".yml": "yaml",
	".json": "json",
	".toml": "toml",
}


@dataclass(frozen=True)
class SourceDocsWarning:
	"""Non-fatal issue encountered while scanning source documents."""

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
class SourceDocsCategory:
	"""Stable category-level summary for one source docs scan."""

	name: str
	relative_path: str
	display_path: str
	exists: bool
	file_count: int = 0
	directory_count: int = 0

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable category record."""

		return {
			"name": self.name,
			"relative_path": self.relative_path,
			"display_path": self.display_path,
			"exists": self.exists,
			"file_count": self.file_count,
			"directory_count": self.directory_count,
		}


@dataclass(frozen=True)
class SourceDocEntry:
	"""One guarded source document discovered under knowledge-sources."""

	relative_path: str
	display_path: str
	category: str
	size_bytes: int
	content_hash: str | None
	format: str | None = None
	is_symlink: bool = False

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable file record."""

		return {
			"relative_path": self.relative_path,
			"display_path": self.display_path,
			"category": self.category,
			"size_bytes": self.size_bytes,
			"content_hash": self.content_hash,
			"format": self.format,
			"is_symlink": self.is_symlink,
		}


@dataclass(frozen=True)
class SourceDocsManifest:
	"""Stable manifest for one knowledge-sources scan."""

	schema_version: str
	source_docs_root: str
	source_docs_display_path: str
	supported_categories: tuple[str, ...]
	categories: tuple[SourceDocsCategory, ...]
	files: tuple[SourceDocEntry, ...]
	manifest_hash: str
	warnings: tuple[SourceDocsWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable manifest payload."""

		return {
			"schema_version": self.schema_version,
			"source_docs_root": self.source_docs_root,
			"source_docs_display_path": self.source_docs_display_path,
			"supported_categories": list(self.supported_categories),
			"categories": [category.to_dict() for category in self.categories],
			"files": [entry.to_dict() for entry in self.files],
			"file_count": len(self.files),
			"manifest_hash": self.manifest_hash,
			"warnings": [warning.to_dict() for warning in self.warnings],
		}

	def to_baseline_manifest_fragment(self) -> dict[str, object]:
		"""Return the source docs summary that can be merged into a baseline manifest."""

		file_count_by_category = {
			category.name: category.file_count
			for category in self.categories
			if category.exists or category.file_count > 0
		}
		return {
			"source_docs": {
				"schema_version": self.schema_version,
				"manifest_hash": self.manifest_hash,
				"root": self.source_docs_root,
				"file_count": len(self.files),
				"supported_categories": list(self.supported_categories),
				"present_categories": [category.name for category in self.categories if category.exists],
				"file_count_by_category": file_count_by_category,
			}
		}


@dataclass
class _CategoryStats:
	name: str
	relative_path: str
	display_path: str
	exists: bool = False
	file_count: int = 0
	directory_count: int = 0


class SourceDocsConnector:
	"""Discover source documents under configured knowledge-sources root."""

	def __init__(
		self,
		source_docs_root: str | Path,
		guard: PathGuard,
		*,
		supported_categories: Iterable[str] = SUPPORTED_SOURCE_DOC_CATEGORIES,
	) -> None:
		self.source_docs_root = Path(source_docs_root).expanduser()
		self.guard = guard
		self.supported_categories = tuple(dict.fromkeys(supported_categories))
		if not self.supported_categories:
			raise ValueError("SourceDocsConnector requires at least one supported category")

	@classmethod
	def from_config(
		cls,
		config: ActiveKnowledgeConfig,
		*,
		cwd: Path | None = None,
		guard: PathGuard | None = None,
	) -> SourceDocsConnector:
		"""Build a source docs connector from validated runtime config."""

		root = cwd or Path.cwd()
		return cls(
			resolve_runtime_path(config.runtime.source_docs_root, root),
			guard or PathGuard.from_config(config, cwd=root),
		)

	def scan(self, *, create_if_missing: bool = True) -> SourceDocsManifest:
		"""Scan the source docs root into a deterministic manifest."""

		warnings: list[SourceDocsWarning] = []
		root = self.guard.guard(self.source_docs_root, must_exist=False)
		if not root.normalized_path.exists():
			if create_if_missing:
				root.normalized_path.mkdir(parents=True, exist_ok=True)
				warnings.append(
					SourceDocsWarning(
						code="source_docs.root_created",
						message="Source docs root was missing and has been created as an empty directory.",
						display_path=root.display_path,
					)
				)
			else:
				raise FileNotFoundError(f"source docs root does not exist: {root.display_path}")
		if not root.normalized_path.is_dir():
			raise NotADirectoryError(f"source docs root is not a directory: {root.display_path}")

		category_stats = {
			name: _CategoryStats(
				name=name,
				relative_path=name,
				display_path=display_for_relative(root.display_path, name),
			)
			for name in self.supported_categories
		}
		scanned_files: list[SourceDocEntry] = []

		try:
			children = sorted(root.normalized_path.iterdir(), key=lambda child: child.name)
		except OSError as exc:
			warnings.append(
				SourceDocsWarning(
					code="source_docs.directory_read_failed",
					message=f"Cannot read source docs root: {exc}",
					display_path=root.display_path,
				)
			)
			children = []

		for child in children:
			relative_path = child.relative_to(root.normalized_path).as_posix()
			guarded = self._guard_child(child, root, relative_path, warnings)
			if guarded is None:
				continue

			category_name = relative_path.split("/", maxsplit=1)[0]
			if category_name not in category_stats:
				warnings.append(
					SourceDocsWarning(
						code="source_docs.unsupported_area",
						message="Skipping unsupported top-level source docs area.",
						display_path=display_for_relative(root.display_path, relative_path),
						details={
							"area": category_name,
							"supported_categories": list(self.supported_categories),
						},
					)
				)
				continue

			stats = category_stats[category_name]
			stats.exists = True
			self._scan_category_directory(
				root=root,
				current=guarded,
				relative_path=relative_path,
				category_name=category_name,
				stats=stats,
				scanned_files=scanned_files,
				warnings=warnings,
			)

		files = tuple(sorted(scanned_files, key=lambda entry: entry.relative_path))
		categories = tuple(
			SourceDocsCategory(
				name=stats.name,
				relative_path=stats.relative_path,
				display_path=stats.display_path,
				exists=stats.exists,
				file_count=stats.file_count,
				directory_count=stats.directory_count,
			)
			for stats in category_stats.values()
		)
		manifest_hash = compute_source_docs_hash(
			supported_categories=self.supported_categories,
			categories=categories,
			files=files,
		)
		return SourceDocsManifest(
			schema_version=SOURCE_DOCS_MANIFEST_SCHEMA_VERSION,
			source_docs_root=str(root.normalized_path),
			source_docs_display_path=root.display_path,
			supported_categories=self.supported_categories,
			categories=categories,
			files=files,
			manifest_hash=manifest_hash,
			warnings=tuple(warnings),
		)

	def _scan_category_directory(
		self,
		*,
		root: GuardedPath,
		current: GuardedPath,
		relative_path: str,
		category_name: str,
		stats: _CategoryStats,
		scanned_files: list[SourceDocEntry],
		warnings: list[SourceDocsWarning],
	) -> None:
		try:
			lstat_result = current.normalized_path.lstat()
		except OSError as exc:
			warnings.append(
				SourceDocsWarning(
					code="source_docs.stat_failed",
					message=f"Cannot stat source docs path: {exc}",
					display_path=display_for_relative(root.display_path, relative_path),
				)
			)
			return

		is_symlink = stat.S_ISLNK(lstat_result.st_mode)
		mode = lstat_result.st_mode
		target_stat = None
		if is_symlink:
			try:
				target_stat = current.real_path.stat()
			except OSError as exc:
				warnings.append(
					SourceDocsWarning(
						code="source_docs.symlink_target_missing",
						message=f"Cannot stat source docs symlink target: {exc}",
						display_path=display_for_relative(root.display_path, relative_path),
					)
				)
				return
			mode = target_stat.st_mode

		if stat.S_ISDIR(mode):
			if is_symlink:
				warnings.append(
					SourceDocsWarning(
						code="source_docs.symlink_dir_skipped",
						message="Symlinked source docs directories are skipped during scan.",
						display_path=display_for_relative(root.display_path, relative_path),
					)
				)
				return

			if relative_path != category_name:
				stats.directory_count += 1
			try:
				children = sorted(current.normalized_path.iterdir(), key=lambda child: child.name)
			except OSError as exc:
				warnings.append(
					SourceDocsWarning(
						code="source_docs.directory_read_failed",
						message=f"Cannot read source docs directory: {exc}",
						display_path=display_for_relative(root.display_path, relative_path),
					)
				)
				return

			for child in children:
				child_relative_path = child.relative_to(root.normalized_path).as_posix()
				guarded = self._guard_child(child, root, child_relative_path, warnings)
				if guarded is None:
					continue
				self._scan_category_directory(
					root=root,
					current=guarded,
					relative_path=child_relative_path,
					category_name=category_name,
					stats=stats,
					scanned_files=scanned_files,
					warnings=warnings,
				)
			return

		if not stat.S_ISREG(mode):
			return

		size_bytes = target_stat.st_size if target_stat is not None else lstat_result.st_size
		content_hash = hash_guarded_file(current, root, relative_path, warnings)
		if content_hash is None:
			return
		stats.file_count += 1
		scanned_files.append(
			SourceDocEntry(
				relative_path=relative_path,
				display_path=display_for_relative(root.display_path, relative_path),
				category=category_name,
				size_bytes=size_bytes,
				content_hash=content_hash,
				format=detect_source_doc_format(Path(relative_path)),
				is_symlink=is_symlink,
			)
		)

	def _guard_child(
		self,
		child: Path,
		root: GuardedPath,
		relative_path: str,
		warnings: list[SourceDocsWarning],
	) -> GuardedPath | None:
		try:
			return self.guard.guard(child, must_exist=True)
		except PathBlockedError as exc:
			warnings.append(
				SourceDocsWarning(
					code="security.path_blocked",
					message=exc.warning.message,
					display_path=display_for_relative(root.display_path, relative_path),
					details={"reason": exc.warning.reason},
				)
			)
			return None


def scan_source_docs(
	source_docs_root: str | Path,
	guard: PathGuard,
	*,
	supported_categories: Iterable[str] = SUPPORTED_SOURCE_DOC_CATEGORIES,
	create_if_missing: bool = True,
) -> SourceDocsManifest:
	"""Convenience wrapper for one-off source docs scans."""

	return SourceDocsConnector(
		source_docs_root,
		guard,
		supported_categories=supported_categories,
	).scan(create_if_missing=create_if_missing)


def detect_source_doc_format(path: Path) -> str | None:
	"""Infer a lightweight source docs format label from file suffix."""

	return _SOURCE_DOC_FORMATS.get(path.suffix.lower())


def hash_guarded_file(
	guarded: GuardedPath,
	root: GuardedPath,
	relative_path: str,
	warnings: list[SourceDocsWarning],
) -> str | None:
	"""Hash a guarded source doc so manifests remain deterministic."""

	digest = hashlib.sha256()
	try:
		with guarded.normalized_path.open("rb") as handle:
			for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
				digest.update(chunk)
	except OSError as exc:
		warnings.append(
			SourceDocsWarning(
				code="source_docs.file_read_failed",
				message=f"Cannot read source docs file: {exc}",
				display_path=display_for_relative(root.display_path, relative_path),
			)
		)
		return None
	return f"sha256:{digest.hexdigest()}"


def compute_source_docs_hash(
	*,
	supported_categories: tuple[str, ...],
	categories: tuple[SourceDocsCategory, ...],
	files: tuple[SourceDocEntry, ...],
) -> str:
	"""Compute a deterministic manifest hash from source docs facts only."""

	payload = {
		"schema_version": SOURCE_DOCS_MANIFEST_SCHEMA_VERSION,
		"supported_categories": supported_categories,
		"categories": [
			{
				"name": category.name,
				"exists": category.exists,
				"file_count": category.file_count,
				"directory_count": category.directory_count,
			}
			for category in categories
		],
		"files": [
			{
				"relative_path": entry.relative_path,
				"category": entry.category,
				"size_bytes": entry.size_bytes,
				"content_hash": entry.content_hash,
				"format": entry.format,
				"is_symlink": entry.is_symlink,
			}
			for entry in files
		],
	}
	encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
	return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
