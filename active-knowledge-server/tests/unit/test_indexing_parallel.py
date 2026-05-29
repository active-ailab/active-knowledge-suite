from __future__ import annotations

import pytest

from active_knowledge_server.indexing.parallel import (
    parallel_map_ordered,
    resolve_indexing_workers,
)


def test_resolve_indexing_workers_uses_serial_for_small_task_sets() -> None:
    assert resolve_indexing_workers("auto", task_count=0, phase="docs").workers == 1
    assert resolve_indexing_workers("auto", task_count=1, phase="code").workers == 1
    assert resolve_indexing_workers("auto", task_count=3, phase="docs").workers == 1


def test_resolve_indexing_workers_caps_auto_conservatively() -> None:
    resolved = resolve_indexing_workers("auto", task_count=20, phase="docs", cpu_count=32)

    assert resolved.workers == 6
    assert resolved.parallel is True
    assert resolved.to_dict()["reason"] == "auto"


def test_resolve_indexing_workers_honors_explicit_worker_count() -> None:
    resolved = resolve_indexing_workers(8, task_count=3, phase="code")

    assert resolved.workers == 3
    assert resolved.parallel is True


def test_resolve_indexing_workers_uses_process_for_code_when_enabled() -> None:
    resolved = resolve_indexing_workers(
        "auto",
        configured_mode="hybrid",
        task_count=8,
        phase="code",
        allow_process=True,
        cpu_count=8,
    )

    assert resolved.executor_kind == "process"


def test_resolve_indexing_workers_falls_back_to_thread_for_docs_process_mode() -> None:
    resolved = resolve_indexing_workers(
        4,
        configured_mode="process",
        task_count=8,
        phase="docs",
        allow_process=False,
    )

    assert resolved.executor_kind == "thread"
    assert resolved.reason == "process_mode_fallback_to_thread"


def test_parallel_map_ordered_returns_stable_key_order() -> None:
    workers = resolve_indexing_workers(2, task_count=3, phase="docs")
    events: list[tuple[str, int]] = []

    results = parallel_map_ordered(
        ["b", "c", "a"],
        key=lambda item: item,
        mapper=lambda item: item.upper(),
        workers=workers,
        max_in_flight=2,
        callback=lambda path, done: events.append((path, done)),
    )

    assert [result.key for result in results] == ["a", "b", "c"]
    assert [result.value for result in results] == ["A", "B", "C"]
    assert sorted(done for _path, done in events) == [1, 2, 3]


def test_parallel_map_ordered_wraps_item_errors() -> None:
    workers = resolve_indexing_workers(2, task_count=3, phase="docs")

    def mapper(item: str) -> str:
        if item == "bad":
            raise ValueError("synthetic failure")
        return item

    results = parallel_map_ordered(
        ["ok", "bad"],
        key=lambda item: item,
        mapper=mapper,
        workers=workers,
    )

    failed = next(result for result in results if result.key == "bad")
    assert failed.ok is False
    assert isinstance(failed.error, ValueError)
    assert next(result for result in results if result.key == "ok").value == "ok"


def test_parallel_map_ordered_propagates_keyboard_interrupt() -> None:
    workers = resolve_indexing_workers(1, task_count=1, phase="docs")

    def mapper(_item: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        parallel_map_ordered(["a"], key=lambda item: item, mapper=mapper, workers=workers)
