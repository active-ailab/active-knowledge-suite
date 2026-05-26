from __future__ import annotations

import pytest

from active_knowledge_server.cli_progress import (
    IndexProgressRenderer,
    PlainIndexProgressReporter,
    _cell_width,
    _truncate_middle,
)
from active_knowledge_server.indexing.progress import IndexProgressEvent


def test_renderer_keeps_recent_paths_bounded() -> None:
    renderer = IndexProgressRenderer()

    for index in range(12):
        renderer.handle(
            IndexProgressEvent(
                phase="code_collect",
                stage_total=12,
                stage_done=index + 1,
                current_path=f"components/file_{index}.c",
            )
        )

    assert list(renderer.recent_paths) == [
        f"components/file_{index}.c" for index in range(2, 12)
    ]


def test_renderer_interrupt_lines_include_snapshot() -> None:
    renderer = IndexProgressRenderer()
    renderer.handle(
        IndexProgressEvent(
            phase="doc_apply",
            stage_total=4,
            stage_done=3,
            global_total=9,
            global_done=7,
            current_path="knowledge-sources/api/sensor.md",
        )
    )

    lines = renderer.interrupt_lines()

    assert lines[0] == "Index interrupted."
    assert "Phase: doc_apply" in lines
    assert "Stage: 3/4" in lines
    assert "Overall: 7/9" in lines
    assert "Last path: knowledge-sources/api/sensor.md" in lines


def test_renderer_recent_lines_fall_back_to_latest_message() -> None:
    renderer = IndexProgressRenderer()
    renderer.handle(
        IndexProgressEvent(
            phase="discover",
            stage_total=3,
            stage_done=0,
            message="Scanning workspace inventory",
        )
    )

    assert renderer.recent_lines() == ("  -> Scanning workspace inventory",)


def test_renderer_recent_lines_prepend_status_message_over_stale_paths() -> None:
    renderer = IndexProgressRenderer()
    renderer.handle(
        IndexProgressEvent(
            phase="code_collect",
            stage_total=2,
            stage_done=1,
            current_path="components/health/main.c",
            message="Collecting code files with 2 workers",
        )
    )
    renderer.handle(
        IndexProgressEvent(
            phase="code_finalize",
            stage_total=2,
            stage_done=2,
            message="Assembling file and symbol records",
        )
    )

    assert renderer.recent_lines() == (
        "  -> Assembling file and symbol records",
        "  -> components/health/main.c",
    )


def test_truncate_middle_preserves_prefix_and_suffix() -> None:
    value = ".repo/project-objects/huamiOS/packages/apps/BodyTemp.git"

    truncated = _truncate_middle(value, 32)

    assert truncated.startswith(".repo/")
    assert truncated.endswith("Temp.git")
    assert _cell_width(truncated) <= 32


def test_truncate_middle_uses_terminal_cell_width_for_mixed_text() -> None:
    value = "阶段/中文目录/temperature/传感器状态总览.md"

    truncated = _truncate_middle(value, 20)

    assert truncated.endswith("总览.md")
    assert "..." in truncated
    assert _cell_width(truncated) <= 20


def test_plain_reporter_emits_done_event() -> None:
    class CaptureStream:
        def __init__(self) -> None:
            self.parts: list[str] = []

        def write(self, value: str) -> int:
            self.parts.append(value)
            return len(value)

        def flush(self) -> None:
            return None

    stream = CaptureStream()
    reporter = PlainIndexProgressReporter(stream=stream)
    reporter.handle(
        IndexProgressEvent(
            phase="done",
            stage_total=1,
            stage_done=1,
            global_total=3,
            global_done=3,
            message="Incremental indexing finished",
        )
    )

    assert "Incremental indexing finished" in "".join(stream.parts)


def test_rich_reporter_renders_initial_frame(monkeypatch) -> None:
    pytest.importorskip("rich")

    class CaptureStream:
        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            return None

    from active_knowledge_server.cli_progress import RichIndexProgressReporter

    reporter = RichIndexProgressReporter(stream=CaptureStream())
    refresh_calls: list[bool] = []
    original_update = reporter._live.update

    def spy_update(renderable, *, refresh: bool = False):
        refresh_calls.append(refresh)
        return original_update(renderable, refresh=refresh)

    monkeypatch.setattr(reporter._live, "update", spy_update)

    try:
        reporter.__enter__()
    finally:
        reporter.close()

    assert refresh_calls
    assert refresh_calls[0] is True
    assert reporter._live.auto_refresh is True
    assert len(reporter._progress.tasks) == 2
    assert reporter._progress.tasks[0].fields["label"] == "Overall"
    assert reporter._progress.tasks[1].fields["label"] == "Stage"


def test_rich_reporter_treats_discover_as_indeterminate() -> None:
    pytest.importorskip("rich")

    class CaptureStream:
        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            return None

    from active_knowledge_server.cli_progress import RichIndexProgressReporter

    reporter = RichIndexProgressReporter(stream=CaptureStream())
    try:
        reporter.__enter__()
        reporter.handle(
            IndexProgressEvent(
                phase="discover",
                stage_total=3,
                stage_done=0,
                message="Scanning workspace inventory: services/sysservice.git (3973 files, 3824 directories)",
            )
        )
        stage_task = reporter._progress.tasks[1]
        assert stage_task.total is None
        assert stage_task.completed == 0
        assert stage_task.description == "Scanning workspace inventory"
        reporter.handle(
            IndexProgressEvent(
                phase="code_finalize",
                stage_total=20,
                stage_done=20,
                message="Finalizing code index bundle for overlay apply",
            )
        )
        stage_task = reporter._progress.tasks[1]
        assert stage_task.total is None
        assert stage_task.completed == 0
    finally:
        reporter.close()
