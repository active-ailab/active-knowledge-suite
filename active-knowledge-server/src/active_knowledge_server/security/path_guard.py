"""Path allowlist guard for source, docs, and workdir access."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from active_knowledge_server.config.schema import ActiveKnowledgeConfig


@dataclass(frozen=True)
class AllowlistRoot:
    """Normalized allowlist root."""

    label: str
    logical_path: Path
    real_path: Path


@dataclass(frozen=True)
class GuardedPath:
    """A path that passed allowlist and symlink checks."""

    requested_path: Path
    normalized_path: Path
    real_path: Path
    display_path: str
    root: AllowlistRoot

    def to_dict(self) -> dict[str, str]:
        """Return a safe display-oriented representation."""

        return {
            "display_path": self.display_path,
            "root": self.root.label,
            "normalized_path": str(self.normalized_path),
        }


@dataclass(frozen=True)
class PathBlockedWarning:
    """Structured blocked-level warning for path guard failures."""

    reason: str
    message: str
    display_path: str
    suggested_action: str = (
        "Use a path under the configured workspace, source docs, or workdir allowlist."
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a QueryResult-compatible blocked warning."""

        return {
            "level": "blocked",
            "code": "security.path_blocked",
            "message": self.message,
            "details": {
                "reason": self.reason,
                "display_path": self.display_path,
            },
            "actionable": True,
            "suggested_action": self.suggested_action,
            "affected_sources": [],
            "evidence_refs": [],
        }


class PathBlockedError(ValueError):
    """Raised when a requested path is outside the configured allowlist."""

    def __init__(self, warning: PathBlockedWarning) -> None:
        self.warning = warning
        super().__init__(warning.message)

    def to_blocked_response(self) -> dict[str, Any]:
        """Return the shared structured blocked response shape."""

        return {
            "result_status": "blocked",
            "status": "blocked",
            "summary": "The request was blocked by the path guard.",
            "items": [],
            "candidates": [],
            "evidence_refs": [],
            "warnings": [self.warning.to_dict()],
            "next_queries": ["Use a path under the configured allowlist."],
            "diagnostics": {
                "blocked_reason": self.warning.reason,
            },
        }


class PathGuard:
    """Guard filesystem paths against allowlist and symlink escapes."""

    def __init__(
        self,
        roots: Iterable[AllowlistRoot],
        *,
        cwd: Path | None = None,
        allow_symlink_escape: bool = False,
    ) -> None:
        self.cwd = (cwd or Path.cwd()).expanduser()
        self.allow_symlink_escape = allow_symlink_escape
        self.roots = tuple(roots)
        if not self.roots:
            raise ValueError("PathGuard requires at least one allowlist root")

    @classmethod
    def from_config(
        cls,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
        allow_symlink_escape: bool = False,
    ) -> PathGuard:
        """Build a path guard from runtime config and security.path_allowlist."""

        root = cwd or Path.cwd()
        labeled_roots = labeled_roots_from_config(config)
        roots: list[AllowlistRoot] = []
        seen: set[Path] = set()
        for label, path_value in labeled_roots:
            logical = normalize_logical_path(path_value, base=root)
            if logical in seen:
                continue
            seen.add(logical)
            roots.append(
                AllowlistRoot(
                    label=label,
                    logical_path=logical,
                    real_path=resolve_real_path(logical),
                )
            )
        return cls(roots, cwd=root, allow_symlink_escape=allow_symlink_escape)

    def guard(
        self,
        path: str | Path,
        *,
        base: str | Path | None = None,
        must_exist: bool = False,
        allow_symlink_escape: bool | None = None,
    ) -> GuardedPath:
        """Normalize and validate a requested path."""

        base_path = normalize_logical_path(base, base=self.cwd) if base is not None else self.cwd
        requested = Path(path).expanduser()
        logical_path = normalize_logical_path(requested, base=base_path)
        root = self._matching_root(logical_path, use_real_path=False)
        if root is None:
            raise_path_blocked(
                reason="path_outside_allowlist",
                path=logical_path,
                message="Requested path is outside the configured allowlist.",
            )

        if must_exist and not logical_path.exists():
            raise_path_blocked(
                reason="path_missing",
                path=logical_path,
                message="Requested path does not exist.",
            )

        real_path = resolve_real_path(logical_path)
        symlink_allowed = (
            self.allow_symlink_escape if allow_symlink_escape is None else allow_symlink_escape
        )
        if not symlink_allowed:
            real_root = self._matching_root(real_path, use_real_path=True)
            if real_root is None:
                raise_path_blocked(
                    reason="symlink_outside_allowlist",
                    path=logical_path,
                    message="Requested path resolves outside the configured allowlist.",
                )

        return GuardedPath(
            requested_path=requested,
            normalized_path=logical_path,
            real_path=real_path,
            display_path=display_path(logical_path, root),
            root=root,
        )

    def display_path(self, path: str | Path, *, base: str | Path | None = None) -> str:
        """Return the safe relative display path for a guarded path."""

        return self.guard(path, base=base).display_path

    def _matching_root(self, path: Path, *, use_real_path: bool) -> AllowlistRoot | None:
        for root in self.roots:
            candidate_root = root.real_path if use_real_path else root.logical_path
            if path_is_relative_to(path, candidate_root):
                return root
        return None


def labeled_roots_from_config(config: ActiveKnowledgeConfig) -> tuple[tuple[str, str], ...]:
    """Return labeled path roots from config, preserving configured allowlist order."""

    known_roots = {
        normalize_path_string(config.project.workspace_root): "workspace",
        normalize_path_string(config.runtime.source_docs_root): "source_docs",
        normalize_path_string(config.runtime.workdir): "workdir",
        normalize_path_string(config.runtime.baseline_dir): "baseline",
        normalize_path_string(config.runtime.local_dir): "local",
    }

    roots: list[tuple[str, str]] = []
    for raw_path in config.security.path_allowlist:
        normalized = normalize_path_string(raw_path)
        label = known_roots.get(normalized, "allowlist")
        roots.append((label, raw_path))

    if not roots:
        roots.extend(
            (
                ("workspace", config.project.workspace_root),
                ("source_docs", config.runtime.source_docs_root),
                ("workdir", config.runtime.workdir),
            )
        )
    return tuple(roots)


def normalize_logical_path(path: str | Path, *, base: str | Path) -> Path:
    """Return an absolute path with dot segments removed without following symlinks."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base).expanduser() / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def resolve_real_path(path: Path) -> Path:
    """Return the real path, resolving symlinks in existing parents."""

    return path.resolve(strict=False)


def path_is_relative_to(path: Path, root: Path) -> bool:
    """Return whether path is equal to or contained by root."""

    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def display_path(path: Path, root: AllowlistRoot) -> str:
    """Return a safe root-relative display path."""

    try:
        relative = path.relative_to(root.logical_path)
    except ValueError:
        try:
            relative = path.relative_to(root.real_path)
        except ValueError:
            relative = Path(path.name)
    rel_text = "." if str(relative) == "." else relative.as_posix()
    return f"{root.label}:{rel_text}"


def normalize_path_string(path: str | Path) -> str:
    """Normalize configured path strings for label matching."""

    return Path(path).expanduser().as_posix().rstrip("/")


def blocked_warning_for_path(*, reason: str, path: Path, message: str) -> PathBlockedWarning:
    """Build a blocked warning without leaking absolute paths."""

    return PathBlockedWarning(
        reason=reason,
        message=message,
        display_path=path.name or ".",
    )


def raise_path_blocked(*, reason: str, path: Path, message: str) -> NoReturn:
    """Raise a structured path guard exception."""

    raise PathBlockedError(blocked_warning_for_path(reason=reason, path=path, message=message))
