from __future__ import annotations

from active_knowledge_server.cli_progress import IndexProgressRenderer, PlainIndexProgressReporter
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
