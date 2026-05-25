"""Shared progress event contract for indexing workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

IndexProgressPhase = Literal[
    "plan",
    "discover",
    "code_collect",
    "code_apply",
    "doc_collect",
    "doc_apply",
    "vectors_apply",
    "profile_relations",
    "workspace_map",
    "done",
]

INDEX_PROGRESS_PHASES: Final[tuple[IndexProgressPhase, ...]] = (
    "plan",
    "discover",
    "code_collect",
    "code_apply",
    "doc_collect",
    "doc_apply",
    "vectors_apply",
    "profile_relations",
    "workspace_map",
    "done",
)


def utc_timestamp() -> str:
    """Return one stable UTC timestamp for progress events."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class IndexProgressEvent:
    """One JSON-safe progress event emitted during indexing."""

    phase: IndexProgressPhase
    stage_total: int | None = None
    stage_done: int | None = None
    global_total: int | None = None
    global_done: int | None = None
    current_path: str | None = None
    message: str | None = None
    warnings_count: int = 0
    started_at: str | None = None
    updated_at: str | None = None
    eta_seconds: float | None = None

    def __post_init__(self) -> None:
        _validate_non_negative("stage_total", self.stage_total)
        _validate_non_negative("stage_done", self.stage_done)
        _validate_non_negative("global_total", self.global_total)
        _validate_non_negative("global_done", self.global_done)
        _validate_non_negative("warnings_count", self.warnings_count)
        _validate_non_negative("eta_seconds", self.eta_seconds)
        _validate_progress_pair("stage", self.stage_done, self.stage_total)
        _validate_progress_pair("global", self.global_done, self.global_total)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable payload."""

        payload: dict[str, object] = {
            "phase": self.phase,
            "stage_total": self.stage_total,
            "stage_done": self.stage_done,
            "global_total": self.global_total,
            "global_done": self.global_done,
            "current_path": self.current_path,
            "message": self.message,
            "warnings_count": self.warnings_count,
            "started_at": self.started_at,
            "updated_at": self.updated_at or utc_timestamp(),
            "eta_seconds": self.eta_seconds,
        }
        return payload


IndexProgressCallback = Callable[[IndexProgressEvent], None]


def noop_progress_callback(_: IndexProgressEvent) -> None:
    """Default callback used when the caller does not observe progress."""

    return None


def _validate_non_negative(name: str, value: int | float | None) -> None:
    if value is None:
        return
    if value < 0:
        raise ValueError(f"{name} must be >= 0")


def _validate_progress_pair(prefix: str, done: int | None, total: int | None) -> None:
    if done is None or total is None:
        return
    if done > total:
        raise ValueError(f"{prefix}_done cannot exceed {prefix}_total")
