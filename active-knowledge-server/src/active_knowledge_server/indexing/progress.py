"""Shared progress event contract for indexing workflows."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

IndexProgressPhase = Literal[
    "plan",
    "discover",
    "code_collect",
    "code_finalize",
    "code_apply",
    "doc_collect",
    "doc_finalize",
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
    "code_finalize",
    "code_apply",
    "doc_collect",
    "doc_finalize",
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


@dataclass
class SlidingWindowEtaEstimator:
    """Estimate remaining stage time from a sliding progress-rate window."""

    window_size: int = 6
    _observations: deque[tuple[float, int]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._observations = deque(maxlen=max(2, self.window_size))

    def observe(
        self,
        *,
        completed: int,
        total: int | None,
        now: float | None = None,
    ) -> float | None:
        """Record one progress sample and return the current ETA in seconds."""

        current_time = time.monotonic() if now is None else now
        self._observations.append((current_time, completed))
        if total is None or completed <= 0 or completed >= total or len(self._observations) < 2:
            return None
        start_time, start_completed = self._observations[0]
        delta_completed = completed - start_completed
        delta_seconds = current_time - start_time
        if delta_completed <= 0 or delta_seconds <= 0:
            return None
        rate = delta_completed / delta_seconds
        if rate <= 0:
            return None
        return max((total - completed) / rate, 0.0)


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
