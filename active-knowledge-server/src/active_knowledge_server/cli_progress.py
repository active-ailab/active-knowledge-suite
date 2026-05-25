"""CLI progress renderers for indexing workflows."""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass, field
from typing import TextIO

from active_knowledge_server.indexing.progress import IndexProgressEvent


@dataclass
class IndexProgressRenderer:
    """Track the current indexing progress state for CLI renderers."""

    recent_paths: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    last_event: IndexProgressEvent | None = None

    def handle(self, event: IndexProgressEvent) -> None:
        """Update renderer state from one progress event."""

        self.last_event = event
        if event.current_path:
            self.recent_paths.append(event.current_path)

    def interrupt_lines(self) -> tuple[str, ...]:
        """Return a short interruption snapshot for users."""

        if self.last_event is None:
            return ("Index interrupted.",)
        event = self.last_event
        lines = ["Index interrupted."]
        lines.append(f"Phase: {event.phase}")
        lines.append(
            f"Stage: {_format_counter(event.stage_done, event.stage_total)}"
        )
        lines.append(
            f"Overall: {_format_counter(event.global_done, event.global_total)}"
        )
        if event.current_path:
            lines.append(f"Last path: {event.current_path}")
        return tuple(lines)


class PlainIndexProgressReporter:
    """Low-frequency line progress for non-TTY or fallback text output."""

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._state = IndexProgressRenderer()
        self._last_phase: str | None = None
        self._last_stage_done = -1

    @property
    def state(self) -> IndexProgressRenderer:
        return self._state

    def __enter__(self) -> PlainIndexProgressReporter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def handle(self, event: IndexProgressEvent) -> None:
        self._state.handle(event)
        if not self._should_print(event):
            return
        self._last_phase = event.phase
        self._last_stage_done = -1 if event.stage_done is None else event.stage_done
        print(self._format_event(event), file=self._stream, flush=True)

    def note(self, message: str) -> None:
        print(message, file=self._stream, flush=True)

    def emit_interrupt_summary(self) -> None:
        for line in self._state.interrupt_lines():
            print(line, file=self._stream, flush=True)

    def _should_print(self, event: IndexProgressEvent) -> bool:
        if event.phase == "done":
            return True
        if event.phase != self._last_phase:
            return True
        if event.stage_total is None or event.stage_done is None:
            return False
        if event.stage_done in {1, event.stage_total}:
            return True
        interval = max(1, min(100, event.stage_total // 5 or 1))
        return event.stage_done >= self._last_stage_done + interval

    def _format_event(self, event: IndexProgressEvent) -> str:
        label = event.message or event.phase.replace("_", " ")
        counter = _format_counter(event.stage_done, event.stage_total)
        suffix = "" if event.current_path is None else f"  {event.current_path}"
        return f"[{label}] {counter}{suffix}"


class RichIndexProgressReporter:
    """TTY progress renderer backed by Rich Live and Progress."""

    def __init__(self, *, stream: TextIO | None = None, refresh_per_second: int = 5) -> None:
        from rich.console import Console, Group
        from rich.live import Live
        from rich.progress import (
            BarColumn,
            Progress,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )
        from rich.table import Table
        from rich.text import Text

        self._stream = stream or sys.stdout
        self._state = IndexProgressRenderer()
        self._console = Console(file=self._stream)
        self._group = Group
        self._table = Table
        self._text = Text
        self._overall_progress = Progress(
            TextColumn("[bold]Overall[/bold] {task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self._console,
        )
        self._stage_progress = Progress(
            TextColumn("[bold]Stage[/bold] {task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self._console,
        )
        self._overall_task = self._overall_progress.add_task("waiting", total=None)
        self._stage_task = self._stage_progress.add_task("waiting", total=None)
        self._live = Live(
            self._build_renderable(),
            console=self._console,
            refresh_per_second=refresh_per_second,
            auto_refresh=False,
            transient=False,
        )
        self._started = False

    @property
    def state(self) -> IndexProgressRenderer:
        return self._state

    def __enter__(self) -> RichIndexProgressReporter:
        self._live.start()
        self._started = True
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if not self._started:
            return
        self._live.stop()
        self._started = False

    def handle(self, event: IndexProgressEvent) -> None:
        self._state.handle(event)
        self._overall_progress.update(
            self._overall_task,
            description=event.phase.replace("_", " "),
            total=event.global_total,
            completed=event.global_done or 0,
        )
        self._stage_progress.update(
            self._stage_task,
            description=event.message or event.phase.replace("_", " "),
            total=event.stage_total,
            completed=event.stage_done or 0,
        )
        self._live.update(self._build_renderable(), refresh=True)

    def note(self, message: str) -> None:
        self._live.console.print(message)

    def emit_interrupt_summary(self) -> None:
        self.close()
        for line in self._state.interrupt_lines():
            self._console.print(line)

    def _build_renderable(self):
        recent_table = self._table.grid(padding=(0, 1))
        recent_table.add_row(self._text("Recent", style="bold"))
        if not self._state.recent_paths:
            recent_table.add_row("  waiting for file progress...")
        else:
            for path in self._state.recent_paths:
                recent_table.add_row(f"  -> {path}")
        return self._group(
            self._overall_progress,
            self._stage_progress,
            recent_table,
        )


def create_index_progress_reporter(
    *,
    output_mode: str,
    stream: TextIO | None = None,
):
    """Create a reporter for the selected output mode."""

    if output_mode == "text_plain":
        return PlainIndexProgressReporter(stream=stream)
    if output_mode != "text_dynamic":
        raise ValueError(f"unsupported progress output mode: {output_mode}")
    try:
        return RichIndexProgressReporter(stream=stream)
    except ImportError:
        reporter = PlainIndexProgressReporter(stream=stream)
        reporter.note("Rich is unavailable; falling back to plain progress output.")
        return reporter


def _format_counter(done: int | None, total: int | None) -> str:
    if done is None and total is None:
        return "?"
    if done is None:
        return f"?/{total}"
    if total is None:
        return str(done)
    return f"{done}/{total}"
