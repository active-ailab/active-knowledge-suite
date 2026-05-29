"""CLI progress renderers for indexing workflows."""

from __future__ import annotations

import sys
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import TextIO

from active_knowledge_server.indexing.progress import IndexProgressEvent

try:
    from rich.cells import cell_len as _rich_cell_len
except ImportError:
    _rich_cell_len = None

_TRUNCATION_MARKER = "..."


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

    def recent_lines(self) -> tuple[str, ...]:
        """Return the recent section rows for the current renderer state."""

        if (
            self.last_event is not None
            and self.last_event.current_path is None
            and self.last_event.message
        ):
            recent_paths = tuple(f"  -> {path}" for path in self.recent_paths)
            return (f"  -> {self.last_event.message}", *recent_paths)
        if self.recent_paths:
            return tuple(f"  -> {path}" for path in self.recent_paths)
        if self.last_event is not None and self.last_event.message:
            return (f"  -> {self.last_event.message}",)
        return ("  waiting for file progress...",)


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
        eta = _format_eta(event.eta_seconds)
        suffix = "" if event.current_path is None else f"  {event.current_path}"
        eta_suffix = "" if eta is None else f"  ETA {eta}"
        return f"[{label}] {counter}{eta_suffix}{suffix}"


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
        from rich.table import Column, Table
        from rich.text import Text

        self._stream = stream or sys.stdout
        self._state = IndexProgressRenderer()
        self._console = Console(file=self._stream)
        self._group = Group
        self._table = Table
        self._text = Text
        self._progress = Progress(
            TextColumn(
                "[bold]{task.fields[label]}[/bold] {task.description}",
                table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis"),
            ),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self._console,
            expand=True,
        )
        self._overall_task = self._progress.add_task("waiting", total=None, label="Overall")
        self._stage_task = self._progress.add_task("waiting", total=None, label="Stage")
        self._live = Live(
            self._build_renderable(),
            console=self._console,
            refresh_per_second=refresh_per_second,
            auto_refresh=True,
            transient=False,
        )
        self._started = False

    @property
    def state(self) -> IndexProgressRenderer:
        return self._state

    def __enter__(self) -> RichIndexProgressReporter:
        self._live.start()
        self._started = True
        self._live.update(self._build_renderable(), refresh=True)
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
        stage_total = event.stage_total
        stage_done = event.stage_done or 0
        if event.phase in {"discover", "code_finalize", "doc_finalize"}:
            stage_total = None
            stage_done = 0
        self._progress.update(
            self._overall_task,
            description=event.phase.replace("_", " "),
            total=event.global_total,
            completed=event.global_done or 0,
        )
        self._progress.update(
            self._stage_task,
            description=_truncate_middle(
                _stage_description(event),
                self._progress_description_budget(),
            ),
            total=stage_total,
            completed=stage_done,
        )
        self._live.update(self._build_renderable(), refresh=True)

    def note(self, message: str) -> None:
        self._live.console.print(message)

    def emit_interrupt_summary(self) -> None:
        self.close()
        for line in self._state.interrupt_lines():
            self._console.print(line)

    def _build_renderable(self):
        recent_table = self._table.grid(padding=(0, 1), expand=True)
        recent_table.add_column(no_wrap=True, overflow="ellipsis")
        recent_table.add_row(self._text("Recent", style="bold", no_wrap=True))
        for line in self._state.recent_lines():
            recent_table.add_row(self._render_recent_line(line))
        return self._group(
            self._progress,
            recent_table,
        )

    def _progress_description_budget(self) -> int:
        return max(32, self._console.size.width - 40)

    def _recent_line_budget(self) -> int:
        return max(32, self._console.size.width - 6)

    def _render_recent_line(self, line: str):
        return self._text(
            _truncate_middle(line, self._recent_line_budget()),
            no_wrap=True,
            overflow="ellipsis",
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


def _stage_description(event: IndexProgressEvent) -> str:
    base = event.message or event.phase.replace("_", " ")
    if event.phase == "discover" and event.message:
        if event.message.startswith("Scanning workspace inventory:"):
            base = "Scanning workspace inventory"
        if event.message.startswith("Scanning source documents:"):
            base = "Scanning source documents"
    eta = _format_eta(event.eta_seconds)
    if eta is None:
        return base
    return f"{base} (ETA {eta})"


def _format_eta(value: float | None) -> str | None:
    if value is None:
        return None
    total_seconds = max(int(round(value)), 0)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _truncate_middle(value: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    if _cell_width(value) <= max_length:
        return value
    marker_width = _cell_width(_TRUNCATION_MARKER)
    if max_length <= marker_width:
        return value[: _prefix_index_for_width(value, max_length)]
    available_width = max_length - marker_width
    right_width = (available_width + 1) // 2
    left_width = available_width - right_width
    left_end = _prefix_index_for_width(value, left_width)
    right_start = _suffix_index_for_width(value, right_width, stop=left_end)
    return f"{value[:left_end]}{_TRUNCATION_MARKER}{value[right_start:]}"


def _cell_width(value: str) -> int:
    if _rich_cell_len is not None:
        return _rich_cell_len(value)
    return sum(_char_cell_width(char) for char in value)


def _prefix_index_for_width(value: str, max_width: int) -> int:
    if max_width <= 0:
        return 0
    current_width = 0
    for index, char in enumerate(value):
        char_width = _char_cell_width(char)
        if current_width + char_width > max_width:
            return index
        current_width += char_width
    return len(value)


def _suffix_index_for_width(value: str, max_width: int, *, stop: int = 0) -> int:
    if max_width <= 0:
        return len(value)
    current_width = 0
    index = len(value)
    while index > stop:
        char_width = _char_cell_width(value[index - 1])
        if current_width + char_width > max_width:
            break
        current_width += char_width
        index -= 1
    return index


def _char_cell_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    category = unicodedata.category(char)
    if category in {"Cc", "Cf"}:
        return 0
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 2
    return 1
