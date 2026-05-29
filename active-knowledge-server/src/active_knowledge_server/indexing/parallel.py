"""Controlled parallel helpers for indexing collect phases."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

IndexingPhaseKind = Literal["code", "docs"]
IndexingParallelMode = Literal["thread", "process", "hybrid"]
IndexingExecutorKind = Literal["serial", "thread", "process"]

_InputT = TypeVar("_InputT")
_OutputT = TypeVar("_OutputT")


@dataclass(frozen=True)
class ResolvedIndexingWorkers:
    """Effective worker decision for one collect phase."""

    configured: int | Literal["auto"]
    configured_mode: IndexingParallelMode
    task_count: int
    phase: IndexingPhaseKind
    workers: int
    executor_kind: IndexingExecutorKind
    reason: str

    @property
    def parallel(self) -> bool:
        """Return whether the phase should use a pool executor."""

        return self.executor_kind != "serial"

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe worker decision summary."""

        return {
            "configured": self.configured,
            "configured_mode": self.configured_mode,
            "task_count": self.task_count,
            "phase": self.phase,
            "workers": self.workers,
            "executor_kind": self.executor_kind,
            "parallel": self.parallel,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ParallelMapItemResult(Generic[_InputT, _OutputT]):
    """One ordered item result from a controlled parallel map."""

    key: str
    item: _InputT
    value: _OutputT | None = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        """Return whether the mapped item completed successfully."""

        return self.error is None


def resolve_indexing_workers(
    configured: int | Literal["auto"],
    *,
    configured_mode: IndexingParallelMode = "thread",
    task_count: int,
    phase: IndexingPhaseKind,
    cpu_count: int | None = None,
    allow_process: bool = False,
) -> ResolvedIndexingWorkers:
    """Resolve configured indexing workers to a conservative effective count."""

    if task_count <= 0:
        return ResolvedIndexingWorkers(
            configured=configured,
            configured_mode=configured_mode,
            task_count=task_count,
            phase=phase,
            workers=1,
            executor_kind="serial",
            reason="empty_task_set",
        )
    if task_count == 1:
        return ResolvedIndexingWorkers(
            configured=configured,
            configured_mode=configured_mode,
            task_count=task_count,
            phase=phase,
            workers=1,
            executor_kind="serial",
            reason="single_task",
        )
    if isinstance(configured, int):
        workers = max(1, min(configured, task_count))
        executor_kind = _resolve_executor_kind(
            configured_mode=configured_mode,
            phase=phase,
            workers=workers,
            allow_process=allow_process,
        )
        return ResolvedIndexingWorkers(
            configured=configured,
            configured_mode=configured_mode,
            task_count=task_count,
            phase=phase,
            workers=workers,
            executor_kind=executor_kind,
            reason=_configured_reason(configured_mode, executor_kind),
        )

    available_cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    phase_cap = 4 if phase == "code" else 6
    workers = max(1, min(task_count, phase_cap, available_cpus))
    if task_count < 4:
        workers = 1
        reason = "small_task_set"
    else:
        reason = "auto"
    executor_kind = _resolve_executor_kind(
        configured_mode=configured_mode,
        phase=phase,
        workers=workers,
        allow_process=allow_process,
    )
    return ResolvedIndexingWorkers(
        configured=configured,
        configured_mode=configured_mode,
        task_count=task_count,
        phase=phase,
        workers=workers,
        executor_kind=executor_kind,
        reason=reason,
    )


def parallel_map_ordered(
    items: Iterable[_InputT],
    *,
    key: Callable[[_InputT], str],
    mapper: Callable[[_InputT], _OutputT],
    workers: ResolvedIndexingWorkers,
    max_in_flight: int | None = None,
    callback: Callable[[str, int], None] | None = None,
) -> tuple[ParallelMapItemResult[_InputT, _OutputT], ...]:
    """Map items with bounded concurrency and return results in stable key order."""

    ordered_items = tuple(sorted(items, key=key))
    if not ordered_items:
        return ()

    def run_one(item: _InputT) -> ParallelMapItemResult[_InputT, _OutputT]:
        item_key = key(item)
        try:
            return ParallelMapItemResult(key=item_key, item=item, value=mapper(item))
        except Exception as exc:  # noqa: BLE001 - collect phases degrade per item.
            return ParallelMapItemResult(key=item_key, item=item, error=exc)

    if not workers.parallel:
        results: list[ParallelMapItemResult[_InputT, _OutputT]] = []
        for index, item in enumerate(ordered_items, start=1):
            result = run_one(item)
            results.append(result)
            if callback is not None:
                callback(result.key, index)
        return tuple(results)

    in_flight_limit = max(1, max_in_flight or workers.workers * 2)
    in_flight_limit = max(workers.workers, in_flight_limit)
    results_by_key: dict[str, ParallelMapItemResult[_InputT, _OutputT]] = {}
    next_index = 0
    done_count = 0
    futures: dict[Future[_OutputT], tuple[str, _InputT]] = {}
    executor_cls = (
        ProcessPoolExecutor if workers.executor_kind == "process" else ThreadPoolExecutor
    )

    with executor_cls(max_workers=workers.workers) as executor:
        while next_index < len(ordered_items) or futures:
            while next_index < len(ordered_items) and len(futures) < in_flight_limit:
                item = ordered_items[next_index]
                next_index += 1
                item_key = key(item)
                future = executor.submit(mapper, item)
                futures[future] = (item_key, item)

            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                item_key, item = futures.pop(future)
                try:
                    result = ParallelMapItemResult(
                        key=item_key,
                        item=item,
                        value=future.result(),
                    )
                except BaseException as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        for pending in futures:
                            pending.cancel()
                        raise
                    result = ParallelMapItemResult(
                        key=item_key,
                        item=item,
                        error=exc,
                    )
                results_by_key[result.key] = result
                done_count += 1
                if callback is not None:
                    callback(result.key, done_count)

    return tuple(results_by_key[key(item)] for item in ordered_items)


def _resolve_executor_kind(
    *,
    configured_mode: IndexingParallelMode,
    phase: IndexingPhaseKind,
    workers: int,
    allow_process: bool,
) -> IndexingExecutorKind:
    if workers <= 1:
        return "serial"
    if configured_mode == "thread":
        return "thread"
    if configured_mode == "process":
        return "process" if allow_process else "thread"
    if phase == "code" and allow_process:
        return "process"
    return "thread"


def _configured_reason(
    configured_mode: IndexingParallelMode,
    executor_kind: IndexingExecutorKind,
) -> str:
    if executor_kind == "thread" and configured_mode == "process":
        return "process_mode_fallback_to_thread"
    if executor_kind == "thread" and configured_mode == "hybrid":
        return "hybrid_thread_path"
    if executor_kind == "process" and configured_mode == "hybrid":
        return "hybrid_process_path"
    if executor_kind == "process":
        return "configured_process_mode"
    return "configured_worker_count"
