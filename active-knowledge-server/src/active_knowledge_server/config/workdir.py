"""Workdir initialization and readiness checks."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from active_knowledge_server.config.loader import (
    ConfigDict,
    ConfigError,
    ResolvedConfig,
    resolve_runtime_path,
    set_nested,
)

_ALLOWED_TRACKED_LOCAL_FILES = {
    ".gitignore",
    "README.md",
    "config/.gitkeep",
    "db/.gitkeep",
    "vectors/.gitkeep",
    "artifacts/.gitkeep",
    "cache/.gitkeep",
    "logs/.gitkeep",
    "tmp/.gitkeep",
    "locks/.gitkeep",
}


@dataclass(frozen=True)
class WorkdirLayout:
    """Resolved runtime directories used by CLI and ops commands."""

    workdir: Path
    baseline_dir: Path
    baseline_manifest_path: Path
    local_dir: Path
    local_config_dir: Path
    local_config_path: Path
    local_db_dir: Path
    local_vectors_dir: Path
    local_artifacts_dir: Path
    local_cache_dir: Path
    local_logs_dir: Path
    local_tmp_dir: Path
    local_locks_dir: Path


@dataclass(frozen=True)
class WorkdirWarning:
    """Non-blocking workdir initialization warning."""

    code: str
    message: str
    path: Path | None = None
    details: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable warning."""

        return {
            "level": "warning",
            "code": self.code,
            "message": self.message,
            "path": str(self.path) if self.path else None,
            "details": list(self.details),
        }


@dataclass(frozen=True)
class BaselineManifestStatus:
    """Baseline manifest readiness status."""

    path: Path
    exists: bool
    readable: bool

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable manifest status."""

        return {
            "path": str(self.path),
            "exists": self.exists,
            "readable": self.readable,
        }


@dataclass(frozen=True)
class WorkdirInitResult:
    """Result of an idempotent workdir init operation."""

    layout: WorkdirLayout
    created: tuple[Path, ...]
    warnings: tuple[WorkdirWarning, ...]
    baseline_manifest: BaselineManifestStatus


def layout_from_config(resolved: ResolvedConfig, *, cwd: Path | None = None) -> WorkdirLayout:
    """Resolve the filesystem layout from a validated runtime config."""

    root = cwd or Path.cwd()
    workdir = resolve_runtime_path(resolved.model.runtime.workdir, root)
    baseline_dir = resolve_runtime_path(resolved.model.runtime.baseline_dir, root)
    local_dir = resolve_runtime_path(resolved.model.runtime.local_dir, root)
    baseline_manifest_path = resolve_runtime_path(resolved.model.storage.baseline.manifest, root)
    return WorkdirLayout(
        workdir=workdir,
        baseline_dir=baseline_dir,
        baseline_manifest_path=baseline_manifest_path,
        local_dir=local_dir,
        local_config_dir=local_dir / "config",
        local_config_path=resolved.local_config_path,
        local_db_dir=local_dir / "db",
        local_vectors_dir=local_dir / "vectors",
        local_artifacts_dir=local_dir / "artifacts",
        local_cache_dir=local_dir / "cache",
        local_logs_dir=local_dir / "logs",
        local_tmp_dir=local_dir / "tmp",
        local_locks_dir=local_dir / "locks",
    )


def initialize_workdir(
    resolved: ResolvedConfig,
    *,
    cwd: Path | None = None,
    force: bool = False,
) -> WorkdirInitResult:
    """Create the workdir skeleton, local config, and readiness warnings."""

    root = cwd or Path.cwd()
    layout = layout_from_config(resolved, cwd=root)
    ensure_workdir_target_is_writable(layout.workdir)

    created: list[Path] = []
    directories = workdir_directories(layout)
    for directory in directories:
        create_directory(directory, created)
    create_log_files(layout.local_logs_dir, created)

    gitignore = layout.local_dir / ".gitignore"
    if not gitignore.exists():
        write_text_file(gitignore, local_gitignore_template(), created)

    if force or not layout.local_config_path.exists():
        write_text_file(
            layout.local_config_path,
            yaml.safe_dump(
                local_config_seed(resolved),
                sort_keys=False,
                allow_unicode=False,
            ),
            created,
        )

    baseline_status, baseline_warning = inspect_baseline_manifest(layout.baseline_manifest_path)
    warnings = [baseline_warning] if baseline_warning else []
    tracked_warning = inspect_tracked_local_files(layout.local_dir, cwd=root)
    if tracked_warning:
        warnings.append(tracked_warning)

    return WorkdirInitResult(
        layout=layout,
        created=tuple(created),
        warnings=tuple(warnings),
        baseline_manifest=baseline_status,
    )


def workdir_directories(layout: WorkdirLayout) -> tuple[Path, ...]:
    """Return directories that must exist after init."""

    return (
        layout.workdir,
        layout.baseline_dir,
        layout.baseline_dir / "config",
        layout.baseline_dir / "db",
        layout.baseline_dir / "vectors",
        layout.baseline_dir / "artifacts",
        layout.local_dir,
        layout.local_config_dir,
        layout.local_db_dir,
        layout.local_vectors_dir,
        layout.local_artifacts_dir,
        layout.local_cache_dir,
        layout.local_logs_dir,
        layout.local_tmp_dir,
        layout.local_locks_dir,
    )


def ensure_workdir_target_is_writable(workdir: Path) -> None:
    """Fail before partial initialization when the workdir target is not writable."""

    if workdir.exists() and not workdir.is_dir():
        raise ConfigError(f"workdir path exists but is not a directory: {workdir}")

    existing = workdir if workdir.exists() else first_existing_parent(workdir)
    if not existing.is_dir():
        raise ConfigError(f"workdir parent is not a directory: {existing}")
    if not os.access(existing, os.W_OK | os.X_OK):
        raise ConfigError(f"workdir is not writable: {workdir}")


def first_existing_parent(path: Path) -> Path:
    """Return the nearest existing parent for a path."""

    current = path.expanduser()
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current


def create_directory(directory: Path, created: list[Path]) -> None:
    """Create a directory and record it only when newly created."""

    try:
        existed = directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create workdir directory {directory}: {exc}") from exc
    if not directory.is_dir():
        raise ConfigError(f"workdir path exists but is not a directory: {directory}")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise ConfigError(f"workdir directory is not writable: {directory}")
    if not existed:
        created.append(directory)


def create_log_files(logs_dir: Path, created: list[Path]) -> None:
    """Create the fixed log files expected by runtime logging."""

    from active_knowledge_server.observability.logging import log_file_paths

    for path in log_file_paths(logs_dir).values():
        existed = path.exists()
        try:
            path.touch(exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"cannot create log file {path}: {exc}") from exc
        if not existed:
            created.append(path)


def write_text_file(path: Path, content: str, created: list[Path]) -> None:
    """Write a text file and record it when newly created."""

    existed = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot write workdir file {path}: {exc}") from exc
    if not existed:
        created.append(path)


def inspect_baseline_manifest(
    manifest_path: Path,
) -> tuple[BaselineManifestStatus, WorkdirWarning | None]:
    """Check whether the baseline manifest exists and is readable."""

    if not manifest_path.exists():
        status = BaselineManifestStatus(path=manifest_path, exists=False, readable=False)
        warning = WorkdirWarning(
            code="baseline.manifest_missing",
            message=(
                "Baseline manifest is missing; init can continue, but queries will rely "
                "on local overlay until a baseline is built or downloaded."
            ),
            path=manifest_path,
        )
        return status, warning

    try:
        manifest_path.open("rb").close()
    except OSError as exc:
        status = BaselineManifestStatus(path=manifest_path, exists=True, readable=False)
        warning = WorkdirWarning(
            code="baseline.manifest_unreadable",
            message=f"Baseline manifest exists but is not readable: {exc}",
            path=manifest_path,
        )
        return status, warning

    return BaselineManifestStatus(path=manifest_path, exists=True, readable=True), None


def inspect_tracked_local_files(local_dir: Path, *, cwd: Path) -> WorkdirWarning | None:
    """Warn when runtime local files are tracked by git."""

    tracked = tracked_files_under(local_dir, cwd=cwd)
    unexpected = tuple(
        path for path in tracked if normalize_git_path(path) not in _ALLOWED_TRACKED_LOCAL_FILES
    )
    if not unexpected:
        return None
    return WorkdirWarning(
        code="workdir.local_tracked",
        message=(
            "Machine-local workdir files appear to be tracked by git; keep only the "
            "local skeleton files under version control."
        ),
        path=local_dir,
        details=unexpected,
    )


def tracked_files_under(path: Path, *, cwd: Path) -> tuple[str, ...]:
    """Return tracked git files under a path, relative to that path."""

    git_root = find_git_root(cwd)
    if git_root is None:
        return ()

    try:
        relative_to_root = path.resolve().relative_to(git_root)
    except ValueError:
        return ()

    result = run_git(
        git_root,
        ("ls-files", "--", relative_to_root.as_posix()),
    )
    if result is None or not result.stdout.strip():
        return ()

    prefix = relative_to_root.as_posix().rstrip("/") + "/"
    tracked: list[str] = []
    for line in result.stdout.splitlines():
        if line == relative_to_root.as_posix():
            tracked.append(Path(line).name)
        elif line.startswith(prefix):
            tracked.append(line.removeprefix(prefix))
    return tuple(sorted(tracked))


def find_git_root(cwd: Path) -> Path | None:
    """Return the nearest git root for cwd, if any."""

    result = run_git(cwd, ("rev-parse", "--show-toplevel"))
    if result is None:
        return None
    output = result.stdout.strip()
    return Path(output).resolve() if output else None


def run_git(cwd: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a git command and return None when cwd is not in a repository."""

    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None


def normalize_git_path(path: str) -> str:
    """Normalize git path separators for cross-platform allowlist checks."""

    return path.replace("\\", "/")


def local_config_seed(resolved: ResolvedConfig) -> ConfigDict:
    """Build the initial user-local config file."""

    seed: ConfigDict = {}
    for source, target in (
        ("runtime.workdir", ("runtime", "workdir")),
        ("runtime.source_docs_root", ("runtime", "source_docs_root")),
        ("project.workspace_root", ("project", "workspace_root")),
        ("project.default_profile", ("project", "default_profile")),
        ("server.transport", ("server", "transport")),
        ("server.http.host", ("server", "http", "host")),
        ("server.http.port", ("server", "http", "port")),
    ):
        value = resolved.get(source)
        if value is not None:
            set_nested(seed, target, value)
    return seed


def local_gitignore_template() -> str:
    """Return the gitignore used for machine-local overlay files."""

    return "\n".join(
        [
            "*",
            "!.gitignore",
            "!README.md",
            "!config/",
            "!config/.gitkeep",
            "!db/",
            "!db/.gitkeep",
            "!vectors/",
            "!vectors/.gitkeep",
            "!artifacts/",
            "!artifacts/.gitkeep",
            "!cache/",
            "!cache/.gitkeep",
            "!logs/",
            "!logs/.gitkeep",
            "!tmp/",
            "!tmp/.gitkeep",
            "!locks/",
            "!locks/.gitkeep",
            "",
        ]
    )
