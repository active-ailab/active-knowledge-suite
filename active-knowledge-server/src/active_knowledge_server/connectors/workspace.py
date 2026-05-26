"""Workspace source discovery connector."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.security.path_guard import GuardedPath, PathBlockedError, PathGuard

WORKSPACE_INVENTORY_SCHEMA_VERSION: Final = "workspace_inventory.v1"
_HASH_CHUNK_SIZE: Final = 1024 * 1024
_GIT_TIMEOUT_SECONDS: Final = 5
_HARD_EXCLUDE_PATTERNS: Final = (".git", ".hg", ".svn")

GitBoundaryKind = Literal["directory", "file", "submodule_status"]
WorkspaceScanProgressKind = Literal["directory", "file"]

_LANGUAGE_BY_NAME: Final[Mapping[str, str]] = {
    "Kconfig": "kconfig",
    "Makefile": "makefile",
    "CMakeLists.txt": "cmake",
}
_LANGUAGE_BY_SUFFIX: Final[Mapping[str, str]] = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "c-header",
    ".hpp": "cpp-header",
    ".hh": "cpp-header",
    ".py": "python",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".ts": "typescript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".sh": "shell",
    ".cmake": "cmake",
    ".mk": "makefile",
    ".S": "assembly",
    ".s": "assembly",
}


@dataclass(frozen=True)
class WorkspaceScanOptions:
    """Options controlling deterministic workspace discovery."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    hash_files: bool = True


@dataclass(frozen=True)
class WorkspaceWarning:
    """Non-fatal issue encountered while scanning a workspace."""

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
class WorkspaceArea:
    """Top-level workspace area discovered under the workspace root."""

    name: str
    relative_path: str
    display_path: str
    file_count: int = 0
    directory_count: int = 0

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable area record."""

        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "display_path": self.display_path,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
        }


@dataclass(frozen=True)
class RepositoryInfo:
    """Git repository or submodule boundary and its current revision."""

    relative_path: str
    display_path: str
    commit: str | None
    branch: str | None = None
    dirty: bool | None = None
    boundary_kind: GitBoundaryKind = "directory"
    is_workspace_root: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable repository record."""

        return {
            "relative_path": self.relative_path,
            "display_path": self.display_path,
            "commit": self.commit,
            "branch": self.branch,
            "dirty": self.dirty,
            "boundary_kind": self.boundary_kind,
            "is_workspace_root": self.is_workspace_root,
            "error": self.error,
        }


@dataclass(frozen=True)
class FileInventoryEntry:
    """One guarded source file discovered in the workspace."""

    relative_path: str
    display_path: str
    size_bytes: int
    content_hash: str | None
    repo_relative_path: str | None
    area: str | None
    language: str | None = None
    is_symlink: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable file inventory record."""

        return {
            "relative_path": self.relative_path,
            "display_path": self.display_path,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "repo_relative_path": self.repo_relative_path,
            "area": self.area,
            "language": self.language,
            "is_symlink": self.is_symlink,
        }


@dataclass(frozen=True)
class WorkspaceScanProgress:
    """One progress tick emitted while scanning the workspace."""

    kind: WorkspaceScanProgressKind
    relative_path: str
    display_path: str
    files_scanned: int
    directories_scanned: int


@dataclass(frozen=True)
class WorkspaceInventory:
    """Stable source manifest for one workspace scan."""

    schema_version: str
    workspace_root: str
    workspace_display_path: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    areas: tuple[WorkspaceArea, ...]
    repositories: tuple[RepositoryInfo, ...]
    files: tuple[FileInventoryEntry, ...]
    inventory_hash: str
    warnings: tuple[WorkspaceWarning, ...] = ()

    @property
    def commit_map(self) -> dict[str, str | None]:
        """Return the repo-relative commit map expected by snapshot indexing."""

        return {repository.relative_path: repository.commit for repository in self.repositories}

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable inventory manifest."""

        return {
            "schema_version": self.schema_version,
            "workspace_root": self.workspace_root,
            "workspace_display_path": self.workspace_display_path,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "areas": [area.to_dict() for area in self.areas],
            "repositories": [repository.to_dict() for repository in self.repositories],
            "commit_map": self.commit_map,
            "files": [entry.to_dict() for entry in self.files],
            "file_count": len(self.files),
            "inventory_hash": self.inventory_hash,
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(frozen=True)
class _ScannedFile:
    relative_path: str
    display_path: str
    size_bytes: int
    content_hash: str | None
    area: str | None
    language: str | None
    is_symlink: bool


@dataclass
class _AreaStats:
    name: str
    relative_path: str
    display_path: str
    file_count: int = 0
    directory_count: int = 0


@dataclass(frozen=True)
class _RepoCandidate:
    relative_path: str
    display_path: str
    boundary_kind: GitBoundaryKind


@dataclass
class _WorkspaceScanProgressState:
    files_scanned: int = 0
    directories_scanned: int = 0


WorkspaceScanProgressCallback = Callable[[WorkspaceScanProgress], None]


def noop_workspace_scan_progress_callback(_: WorkspaceScanProgress) -> None:
    """Default callback used when callers do not observe workspace scan progress."""

    return None


class WorkspaceConnector:
    """Discover workspace areas, git boundaries, and guarded file inventory."""

    def __init__(
        self,
        workspace_root: str | Path,
        guard: PathGuard,
        *,
        options: WorkspaceScanOptions | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser()
        self.guard = guard
        self.options = options or WorkspaceScanOptions()

    @classmethod
    def from_config(
        cls,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
        guard: PathGuard | None = None,
    ) -> WorkspaceConnector:
        """Build a workspace connector from validated runtime config."""

        root = cwd or Path.cwd()
        return cls(
            resolve_runtime_path(config.project.workspace_root, root),
            guard or PathGuard.from_config(config, cwd=root),
            options=WorkspaceScanOptions(
                include=normalize_patterns(config.paths.include),
                exclude=normalize_patterns(config.paths.exclude),
            ),
        )

    def scan(
        self,
        *,
        progress_callback: WorkspaceScanProgressCallback | None = None,
    ) -> WorkspaceInventory:
        """Scan the workspace into a stable inventory manifest."""

        callback = progress_callback or noop_workspace_scan_progress_callback
        warnings: list[WorkspaceWarning] = []
        root = self.guard.guard(self.workspace_root, must_exist=True)
        if not root.normalized_path.is_dir():
            raise NotADirectoryError(f"workspace root is not a directory: {root.display_path}")

        scanned_files: list[_ScannedFile] = []
        repo_candidates: dict[str, _RepoCandidate] = {}
        area_stats: dict[str, _AreaStats] = {}
        progress_state = _WorkspaceScanProgressState()
        callback(
            WorkspaceScanProgress(
                kind="directory",
                relative_path=".",
                display_path=root.display_path,
                files_scanned=0,
                directories_scanned=0,
            )
        )
        self._scan_directory(
            root=root,
            current=root,
            relative_dir="",
            scanned_files=scanned_files,
            repo_candidates=repo_candidates,
            area_stats=area_stats,
            warnings=warnings,
            progress_callback=callback,
            progress_state=progress_state,
        )

        repositories = discover_repositories(root, repo_candidates.values(), self.guard, warnings)
        files = tuple(
            FileInventoryEntry(
                relative_path=file.relative_path,
                display_path=file.display_path,
                size_bytes=file.size_bytes,
                content_hash=file.content_hash,
                repo_relative_path=nearest_repository(file.relative_path, repositories),
                area=file.area,
                language=file.language,
                is_symlink=file.is_symlink,
            )
            for file in sorted(scanned_files, key=lambda item: item.relative_path)
        )
        areas = tuple(
            WorkspaceArea(
                name=stats.name,
                relative_path=stats.relative_path,
                display_path=stats.display_path,
                file_count=stats.file_count,
                directory_count=stats.directory_count,
            )
            for stats in sorted(area_stats.values(), key=lambda item: item.relative_path)
        )
        inventory_hash = compute_inventory_hash(
            areas=areas,
            repositories=repositories,
            files=files,
            include=self.options.include,
            exclude=combined_exclude_patterns(self.options.exclude),
        )
        return WorkspaceInventory(
            schema_version=WORKSPACE_INVENTORY_SCHEMA_VERSION,
            workspace_root=str(root.normalized_path),
            workspace_display_path=root.display_path,
            include=self.options.include,
            exclude=combined_exclude_patterns(self.options.exclude),
            areas=areas,
            repositories=repositories,
            files=files,
            inventory_hash=inventory_hash,
            warnings=tuple(warnings),
        )

    def _scan_directory(
        self,
        *,
        root: GuardedPath,
        current: GuardedPath,
        relative_dir: str,
        scanned_files: list[_ScannedFile],
        repo_candidates: dict[str, _RepoCandidate],
        area_stats: dict[str, _AreaStats],
        warnings: list[WorkspaceWarning],
        progress_callback: WorkspaceScanProgressCallback,
        progress_state: _WorkspaceScanProgressState,
    ) -> None:
        marker_kind = git_marker_kind(current, self.guard, warnings)
        if marker_kind is not None:
            repo_candidates[relative_dir or "."] = _RepoCandidate(
                relative_path=relative_dir or ".",
                display_path=display_for_relative(root.display_path, relative_dir),
                boundary_kind=marker_kind,
            )

        try:
            children = sorted(current.normalized_path.iterdir(), key=lambda child: child.name)
        except OSError as exc:
            warnings.append(
                WorkspaceWarning(
                    code="workspace.directory_read_failed",
                    message=f"Cannot read workspace directory: {exc}",
                    display_path=display_for_relative(root.display_path, relative_dir),
                )
            )
            return

        for child in children:
            relative_path = child.relative_to(root.normalized_path).as_posix()
            guarded = self._guard_child(child, root, relative_path, warnings)
            if guarded is None:
                continue

            try:
                lstat_result = guarded.normalized_path.lstat()
            except OSError as exc:
                warnings.append(
                    WorkspaceWarning(
                        code="workspace.stat_failed",
                        message=f"Cannot stat workspace path: {exc}",
                        display_path=display_for_relative(root.display_path, relative_path),
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
                        WorkspaceWarning(
                            code="workspace.symlink_target_missing",
                            message=f"Cannot stat symlink target: {exc}",
                            display_path=display_for_relative(root.display_path, relative_path),
                        )
                    )
                    continue
                mode = target_stat.st_mode

            is_dir = stat.S_ISDIR(mode)
            if is_excluded(relative_path, is_dir=is_dir, exclude=self.options.exclude):
                continue

            if is_dir:
                if is_symlink:
                    warnings.append(
                        WorkspaceWarning(
                            code="workspace.symlink_dir_skipped",
                            message="Symlinked directories are skipped during workspace scan.",
                            display_path=display_for_relative(root.display_path, relative_path),
                        )
                    )
                    continue
                register_area(area_stats, root.display_path, relative_path, is_file=False)
                progress_state.directories_scanned += 1
                progress_callback(
                    WorkspaceScanProgress(
                        kind="directory",
                        relative_path=relative_path,
                        display_path=display_for_relative(root.display_path, relative_path),
                        files_scanned=progress_state.files_scanned,
                        directories_scanned=progress_state.directories_scanned,
                    )
                )
                self._scan_directory(
                    root=root,
                    current=guarded,
                    relative_dir=relative_path,
                    scanned_files=scanned_files,
                    repo_candidates=repo_candidates,
                    area_stats=area_stats,
                    warnings=warnings,
                    progress_callback=progress_callback,
                    progress_state=progress_state,
                )
                continue

            if not stat.S_ISREG(mode):
                continue
            if not is_included(relative_path, include=self.options.include):
                continue

            size_bytes = target_stat.st_size if target_stat is not None else lstat_result.st_size
            content_hash = None
            if self.options.hash_files:
                content_hash = hash_file(guarded, root, relative_path, warnings)
                if content_hash is None:
                    continue
            register_area(area_stats, root.display_path, relative_path, is_file=True)
            scanned_files.append(
                _ScannedFile(
                    relative_path=relative_path,
                    display_path=display_for_relative(root.display_path, relative_path),
                    size_bytes=size_bytes,
                    content_hash=content_hash,
                    area=area_for_relative_path(relative_path),
                    language=detect_language(Path(relative_path)),
                    is_symlink=is_symlink,
                )
            )
            progress_state.files_scanned += 1
            progress_callback(
                WorkspaceScanProgress(
                    kind="file",
                    relative_path=relative_path,
                    display_path=display_for_relative(root.display_path, relative_path),
                    files_scanned=progress_state.files_scanned,
                    directories_scanned=progress_state.directories_scanned,
                )
            )

    def _guard_child(
        self,
        child: Path,
        root: GuardedPath,
        relative_path: str,
        warnings: list[WorkspaceWarning],
    ) -> GuardedPath | None:
        try:
            return self.guard.guard(child, must_exist=True)
        except PathBlockedError as exc:
            warnings.append(
                WorkspaceWarning(
                    code="security.path_blocked",
                    message=exc.warning.message,
                    display_path=display_for_relative(root.display_path, relative_path),
                    details={"reason": exc.warning.reason},
                )
            )
            return None


def scan_workspace(
    workspace_root: str | Path,
    guard: PathGuard,
    *,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    hash_files: bool = True,
    progress_callback: WorkspaceScanProgressCallback | None = None,
) -> WorkspaceInventory:
    """Convenience wrapper for one-off workspace scans."""

    return WorkspaceConnector(
        workspace_root,
        guard,
        options=WorkspaceScanOptions(
            include=normalize_patterns(include),
            exclude=normalize_patterns(exclude),
            hash_files=hash_files,
        ),
    ).scan(progress_callback=progress_callback)


def discover_repositories(
    root: GuardedPath,
    candidates: Iterable[_RepoCandidate],
    guard: PathGuard,
    warnings: list[WorkspaceWarning],
) -> tuple[RepositoryInfo, ...]:
    """Resolve git metadata for discovered repository boundaries."""

    by_path: dict[str, RepositoryInfo] = {}
    for candidate in sorted(candidates, key=lambda item: item.relative_path):
        by_path[candidate.relative_path] = read_repository_info(root, candidate, guard, warnings)

    root_repo = by_path.get(".")
    if root_repo is not None and root_repo.commit is not None:
        for submodule in read_submodule_status(root, guard, warnings):
            by_path.setdefault(submodule.relative_path, submodule)

    return tuple(by_path[path] for path in sorted(by_path, key=repo_sort_key))


def read_repository_info(
    root: GuardedPath,
    candidate: _RepoCandidate,
    guard: PathGuard,
    warnings: list[WorkspaceWarning],
) -> RepositoryInfo:
    """Read a repository commit, branch, and dirty flag using git."""

    repo_path = (
        root.normalized_path
        if candidate.relative_path == "."
        else (root.normalized_path / candidate.relative_path)
    )
    guarded = guard.guard(repo_path, must_exist=True)
    commit_result = run_git(guarded, ("rev-parse", "--verify", "HEAD"))
    branch_result = run_git(guarded, ("branch", "--show-current"))
    status_result = run_git(guarded, ("status", "--porcelain", "--untracked-files=no"))

    error = commit_result.error
    if error is not None:
        warnings.append(
            WorkspaceWarning(
                code="workspace.git_metadata_unavailable",
                message="Cannot read git metadata for repository boundary.",
                display_path=candidate.display_path,
                details={"error": error},
            )
        )

    return RepositoryInfo(
        relative_path=candidate.relative_path,
        display_path=candidate.display_path,
        commit=commit_result.stdout or None,
        branch=branch_result.stdout or None,
        dirty=bool(status_result.stdout) if status_result.error is None else None,
        boundary_kind=candidate.boundary_kind,
        is_workspace_root=candidate.relative_path == ".",
        error=error,
    )


def read_submodule_status(
    root: GuardedPath,
    guard: PathGuard,
    warnings: list[WorkspaceWarning],
) -> tuple[RepositoryInfo, ...]:
    """Read root-repo submodule status as fallback boundaries."""

    result = run_git(root, ("submodule", "status", "--recursive"))
    if result.error is not None:
        return ()

    repositories: list[RepositoryInfo] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        commit_and_path = line[1:] if line[0] in {"-", "+", "U"} else line
        parts = commit_and_path.split(maxsplit=2)
        if len(parts) < 2:
            continue
        commit, relative_path = parts[0], normalize_relative_path(parts[1])
        if not relative_path:
            continue
        try:
            guarded = guard.guard(root.normalized_path / relative_path)
        except PathBlockedError as exc:
            warnings.append(
                WorkspaceWarning(
                    code="security.path_blocked",
                    message=exc.warning.message,
                    display_path=display_for_relative(root.display_path, relative_path),
                    details={"reason": exc.warning.reason, "source": "git_submodule_status"},
                )
            )
            continue
        repositories.append(
            RepositoryInfo(
                relative_path=relative_path,
                display_path=display_for_relative(root.display_path, relative_path),
                commit=commit,
                boundary_kind="submodule_status",
                is_workspace_root=False,
                error=None if guarded.normalized_path.exists() else "submodule_path_missing",
            )
        )
    return tuple(repositories)


@dataclass(frozen=True)
class _GitResult:
    stdout: str
    error: str | None = None


def run_git(repo: GuardedPath, args: tuple[str, ...]) -> _GitResult:
    """Run a bounded git command for a guarded repository path."""

    try:
        result = subprocess.run(
            ("git", "-C", str(repo.normalized_path), *args),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _GitResult(stdout="", error="git_not_available")
    except subprocess.TimeoutExpired:
        return _GitResult(stdout="", error="git_timeout")

    stdout = result.stdout.strip()
    if result.returncode != 0:
        stderr = result.stderr.strip()
        return _GitResult(stdout=stdout, error=stderr or f"git exited {result.returncode}")
    return _GitResult(stdout=stdout)


def git_marker_kind(
    directory: GuardedPath,
    guard: PathGuard,
    warnings: list[WorkspaceWarning],
) -> GitBoundaryKind | None:
    """Return whether a directory contains a git metadata marker."""

    marker = directory.normalized_path / ".git"
    try:
        guarded = guard.guard(marker)
    except PathBlockedError as exc:
        warnings.append(
            WorkspaceWarning(
                code="security.path_blocked",
                message=exc.warning.message,
                display_path=display_for_relative(directory.display_path, ".git"),
                details={"reason": exc.warning.reason},
            )
        )
        return None

    if not guarded.normalized_path.exists():
        return None
    try:
        mode = guarded.normalized_path.lstat().st_mode
    except OSError as exc:
        warnings.append(
            WorkspaceWarning(
                code="workspace.stat_failed",
                message=f"Cannot stat git marker: {exc}",
                display_path=display_for_relative(directory.display_path, ".git"),
            )
        )
        return None
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return None


def hash_file(
    guarded: GuardedPath,
    root: GuardedPath,
    relative_path: str,
    warnings: list[WorkspaceWarning],
) -> str | None:
    """Hash a guarded file using bytes so binary sources remain safe to inventory."""

    digest = hashlib.sha256()
    try:
        with guarded.normalized_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        warnings.append(
            WorkspaceWarning(
                code="workspace.file_read_failed",
                message=f"Cannot read workspace file: {exc}",
                display_path=display_for_relative(root.display_path, relative_path),
            )
        )
        return None
    return f"sha256:{digest.hexdigest()}"


def compute_inventory_hash(
    *,
    areas: tuple[WorkspaceArea, ...],
    repositories: tuple[RepositoryInfo, ...],
    files: tuple[FileInventoryEntry, ...],
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> str:
    """Compute a deterministic manifest hash from source facts only."""

    payload = {
        "schema_version": WORKSPACE_INVENTORY_SCHEMA_VERSION,
        "include": include,
        "exclude": exclude,
        "areas": [
            {
                "relative_path": area.relative_path,
                "file_count": area.file_count,
                "directory_count": area.directory_count,
            }
            for area in areas
        ],
        "repositories": [
            {
                "relative_path": repository.relative_path,
                "commit": repository.commit,
                "dirty": repository.dirty,
                "boundary_kind": repository.boundary_kind,
            }
            for repository in repositories
        ],
        "files": [
            {
                "relative_path": file.relative_path,
                "size_bytes": file.size_bytes,
                "content_hash": file.content_hash,
                "repo_relative_path": file.repo_relative_path,
                "area": file.area,
                "language": file.language,
                "is_symlink": file.is_symlink,
            }
            for file in files
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def nearest_repository(
    relative_path: str,
    repositories: tuple[RepositoryInfo, ...],
) -> str | None:
    """Return the deepest repository boundary containing a relative path."""

    for repository in sorted(
        repositories,
        key=lambda repo: repo_depth(repo.relative_path),
        reverse=True,
    ):
        if repository.relative_path == ".":
            return "."
        if relative_path == repository.relative_path or relative_path.startswith(
            f"{repository.relative_path}/"
        ):
            return repository.relative_path
    return None


def repo_depth(relative_path: str) -> int:
    """Return path depth for repo boundary ordering."""

    if relative_path == ".":
        return 0
    return relative_path.count("/") + 1


def repo_sort_key(relative_path: str) -> tuple[int, str]:
    """Sort root repository first, then nested repositories lexicographically."""

    return (0 if relative_path == "." else 1, relative_path)


def register_area(
    area_stats: dict[str, _AreaStats],
    root_display_path: str,
    relative_path: str,
    *,
    is_file: bool,
) -> None:
    """Update top-level area counters for a discovered path."""

    area = area_for_relative_path(relative_path)
    if area is None:
        return
    stats = area_stats.setdefault(
        area,
        _AreaStats(
            name=area,
            relative_path=area,
            display_path=display_for_relative(root_display_path, area),
        ),
    )
    if is_file:
        stats.file_count += 1
    else:
        stats.directory_count += 1


def area_for_relative_path(relative_path: str) -> str | None:
    """Return the top-level workspace area for a relative path."""

    parts = relative_path.split("/", maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[0]


def detect_language(path: Path) -> str | None:
    """Infer a lightweight language label from common source file names."""

    if path.name in _LANGUAGE_BY_NAME:
        return _LANGUAGE_BY_NAME[path.name]
    return _LANGUAGE_BY_SUFFIX.get(path.suffix)


def is_included(relative_path: str, *, include: tuple[str, ...]) -> bool:
    """Return whether a file passes include rules."""

    if not include:
        return True
    return any(path_matches_pattern(relative_path, pattern) for pattern in include)


def is_excluded(relative_path: str, *, is_dir: bool, exclude: tuple[str, ...]) -> bool:
    """Return whether a path is excluded by hard or configured patterns."""

    return any(
        path_matches_pattern(relative_path, pattern, is_dir=is_dir)
        for pattern in combined_exclude_patterns(exclude)
    )


def combined_exclude_patterns(exclude: tuple[str, ...]) -> tuple[str, ...]:
    """Return connector hard excludes plus configured excludes."""

    return normalize_patterns((*_HARD_EXCLUDE_PATTERNS, *exclude))


def normalize_patterns(patterns: Iterable[str]) -> tuple[str, ...]:
    """Normalize include/exclude patterns to POSIX-style relative globs."""

    normalized: list[str] = []
    for pattern in patterns:
        value = pattern.strip().replace("\\", "/")
        if value.startswith("./"):
            value = value[2:]
        value = value.strip("/")
        if value:
            normalized.append(value)
    return tuple(dict.fromkeys(normalized))


def normalize_relative_path(relative_path: str) -> str:
    """Normalize a relative path emitted by git or config."""

    return relative_path.strip().replace("\\", "/").strip("/")


def path_matches_pattern(relative_path: str, pattern: str, *, is_dir: bool = False) -> bool:
    """Match a relative path against shell-style source include/exclude patterns."""

    relative = normalize_relative_path(relative_path)
    normalized_pattern = normalize_relative_path(pattern)
    if not relative or not normalized_pattern:
        return False

    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return matches_prefix_pattern(relative, prefix)

    if "/" not in normalized_pattern:
        return any(fnmatch.fnmatchcase(part, normalized_pattern) for part in relative.split("/"))

    if fnmatch.fnmatchcase(relative, normalized_pattern):
        return True
    if PurePosixPath(relative).match(normalized_pattern):
        return True
    if is_dir:
        return fnmatch.fnmatchcase(f"{relative}/", normalized_pattern.rstrip("/") + "/")
    return False


def matches_prefix_pattern(relative_path: str, prefix_pattern: str) -> bool:
    """Return whether a path is equal to or below a glob prefix."""

    if prefix_pattern.startswith("**/"):
        component_pattern = prefix_pattern[3:]
        components = relative_path.split("/")
        for index in range(len(components)):
            suffix = "/".join(components[index:])
            if suffix == component_pattern or suffix.startswith(f"{component_pattern}/"):
                return True
        return False
    return relative_path == prefix_pattern or relative_path.startswith(f"{prefix_pattern}/")


def display_for_relative(root_display_path: str, relative_path: str) -> str:
    """Join a guarded root display path with a workspace-relative path."""

    relative = normalize_relative_path(relative_path)
    if not relative:
        return root_display_path
    if root_display_path.endswith(":."):
        return f"{root_display_path[:-1]}{relative}"
    return f"{root_display_path}/{relative}"
