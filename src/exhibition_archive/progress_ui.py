"""Optional Rich Live renderer for structured workflow progress events.

This module is deliberately independent of the CLI and backend adapters. A
future caller can send :class:`ProgressEvent` values while keeping durable
diagnostic logging in a separate ``event_sink`` callback.

Rich is imported only for an interactive TTY. If it is not installed, events
still reach the callback and no terminal output is produced. Production
integration must declare Rich as a project dependency before relying on the
interactive display.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib import import_module
import math
import sys
import time
from typing import Callable, Literal, TextIO


ProgressState = Literal["running", "completed", "failed", "cancelled"]
_PROGRESS_STATES = {"running", "completed", "failed", "cancelled"}
_TERMINAL_STATES = {"completed", "failed", "cancelled"}


def _finite_number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


@dataclass(frozen=True)
class ProgressEvent:
    """One renderer-neutral update from a workflow stage.

    ``current`` and ``total`` are real counters supplied by the caller. A
    percentage is available only when both are finite and ``0 <= current <=
    total`` with a positive total. ``rate_per_second`` must be an observed
    positive rate (normally measured across multiple counter samples); it is
    used to calculate ETA only when determinate progress is also available.

    ``stage_number`` and ``stage_total`` are optional overall-pipeline context.
    ``state`` marks a terminal update without implying that a stage's counter
    reached 100 percent.
    """

    stage_id: str
    stage_label: str
    operation: str
    stage_number: int | None = None
    stage_total: int | None = None
    current: float | None = None
    total: float | None = None
    unit: str | None = None
    rate_per_second: float | None = None
    state: ProgressState = "running"
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.stage_id.strip():
            raise ValueError("stage_id must be non-empty")
        if not self.stage_label.strip():
            raise ValueError("stage_label must be non-empty")
        if not self.operation.strip():
            raise ValueError("operation must be non-empty")
        if self.state not in _PROGRESS_STATES:
            raise ValueError(f"unsupported progress state: {self.state!r}")

    @property
    def percentage(self) -> float | None:
        """Return measured percentage, or ``None`` when progress is unknown."""
        if not (_finite_number(self.current) and _finite_number(self.total)):
            return None
        current, total = float(self.current), float(self.total)
        if total <= 0 or current < 0 or current > total:
            return None
        return current / total * 100

    @property
    def eta_seconds(self) -> float | None:
        """Return remaining time only from a supplied observed counter rate."""
        if self.percentage is None or not _finite_number(self.rate_per_second):
            return None
        rate = float(self.rate_per_second)
        if rate <= 0:
            return None
        eta = max(0.0, float(self.total) - float(self.current)) / rate
        return eta if math.isfinite(eta) else None


ProgressEventSink = Callable[[ProgressEvent], None]


def _duration(seconds: float) -> str:
    whole_seconds = max(0, int(seconds))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _number_text(value: float) -> str:
    return f"{value:g}"


class RichProgressRenderer:
    """Render :class:`ProgressEvent` updates in one Rich Live terminal region.

    The renderer writes exclusively to ``stream`` (stderr by default), never
    redirects stdout, and does no screen updates when that stream is not a TTY.
    ``event_sink`` receives structured events independently of display mode,
    allowing a caller to retain diagnostic logs without mixing them into the
    live view.

    Typical future integration::

        with RichProgressRenderer(event_sink=append_diagnostic_event) as view:
            view.update(ProgressEvent(
                stage_id="sfm", stage_label="Reconstruction",
                operation="Registering cameras", stage_number=2,
                stage_total=6, current=12, total=40, unit="images",
                rate_per_second=0.8,
            ))

    ``tty`` is an optional detection override for embedding and tests. It does
    not force ANSI output in Rich; the supplied stream still controls that.
    """

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        event_sink: ProgressEventSink | None = None,
        refresh_per_second: float = 8,
        tty: bool | None = None,
    ) -> None:
        if refresh_per_second <= 0:
            raise ValueError("refresh_per_second must be positive")
        self.stream = stream if stream is not None else sys.stderr
        self.event_sink = event_sink
        self.refresh_per_second = refresh_per_second
        self._tty = bool(self.stream.isatty()) if tty is None else tty
        self._started = False
        self._closed = False
        self._interactive = False
        self._started_at: float | None = None
        self._last_event: ProgressEvent | None = None
        self._console = None
        self._live = None
        self._rich = None

    @property
    def interactive(self) -> bool:
        """Whether Rich Live is active for this renderer."""
        return self._interactive

    def start(self) -> bool:
        """Start the live view when running on a TTY with Rich installed."""
        if self._closed:
            raise RuntimeError("progress renderer is already closed")
        if self._started:
            return self._interactive
        self._started = True
        self._started_at = time.monotonic()
        if not self._tty:
            return False

        try:
            console_module = import_module("rich.console")
            live_module = import_module("rich.live")
            panel_module = import_module("rich.panel")
            spinner_module = import_module("rich.spinner")
            text_module = import_module("rich.text")
        except ImportError:
            # Keep non-Rich installs usable; structured events can still be
            # retained through event_sink and stdout remains untouched.
            return False

        self._rich = {
            "Console": console_module.Console,
            "Group": console_module.Group,
            "Live": live_module.Live,
            "Panel": panel_module.Panel,
            "Spinner": spinner_module.Spinner,
            "Text": text_module.Text,
        }
        self._console = self._rich["Console"](
            file=self.stream, markup=False, highlight=False
        )
        self._live = self._rich["Live"](
            console=self._console,
            get_renderable=self._render_frame,
            refresh_per_second=self.refresh_per_second,
            transient=False,
            screen=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.start(refresh=True)
        self._interactive = True
        return True

    def update(self, event: ProgressEvent) -> None:
        """Accept an event, log it separately, and refresh the live region."""
        if self._closed:
            raise RuntimeError("progress renderer is already closed")
        if not isinstance(event, ProgressEvent):
            raise TypeError("event must be a ProgressEvent")
        if not self._started:
            self.start()
        self._last_event = event
        if self.event_sink is not None:
            self.event_sink(event)
        if self._live is not None:
            self._live.refresh()

    def finish(self, state: ProgressState, detail: str | None = None) -> None:
        """Show a terminal state and stop Rich Live, restoring the terminal."""
        if state not in _TERMINAL_STATES:
            raise ValueError("finish state must be completed, failed, or cancelled")
        if self._closed:
            return
        if not self._started:
            self.start()

        try:
            if self._last_event is None:
                self._last_event = ProgressEvent(
                    stage_id="pipeline", stage_label="Pipeline",
                    operation="No progress events received", state=state,
                    detail=detail,
                )
            else:
                self._last_event = replace(
                    self._last_event, state=state,
                    detail=detail if detail is not None else self._last_event.detail,
                )
            if self.event_sink is not None:
                self.event_sink(self._last_event)
            if self._live is not None:
                self._live.refresh()
        finally:
            self._stop_live()
            self._closed = True

    def close(self) -> None:
        """Stop the display without inventing a terminal progress event."""
        if self._closed:
            return
        self._stop_live()
        self._closed = True

    def __enter__(self) -> RichProgressRenderer:
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            state: ProgressState = "cancelled" if issubclass(exc_type, KeyboardInterrupt) else "failed"
            try:
                self.finish(state, str(exc) or state.title())
            except Exception:
                # Preserve the original workload exception while still
                # restoring terminal state if an optional log sink fails.
                self._stop_live()
                self._closed = True
        elif self._last_event is not None and self._last_event.state == "running":
            self.finish("completed")
        elif self._last_event is None:
            self.finish("completed")
        else:
            self.close()
        return False

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._interactive = False

    def _render_frame(self):
        """Build a fresh renderable; Live calls this on timer refreshes too."""
        if self._rich is None:
            return ""
        Text = self._rich["Text"]
        Group = self._rich["Group"]
        Panel = self._rich["Panel"]
        Spinner = self._rich["Spinner"]

        event = self._last_event
        if event is None:
            return Panel(Text("Waiting for progress events…", style="dim"),
                         title="Exhibition Archive")

        if (isinstance(event.stage_number, int) and not isinstance(event.stage_number, bool)
                and isinstance(event.stage_total, int) and not isinstance(event.stage_total, bool)
                and 0 < event.stage_number <= event.stage_total):
            stage = f"Stage {event.stage_number}/{event.stage_total} · {event.stage_label}"
        else:
            stage = event.stage_label
        status = event.state.title()
        status_style = {
            "running": "bold cyan",
            "completed": "bold green",
            "failed": "bold red",
            "cancelled": "bold yellow",
        }[event.state]
        rows = [Text(stage, style="bold"), Text(event.operation)]

        percentage = event.percentage
        if percentage is not None:
            bar_width = 24
            filled = max(0, min(bar_width, int(round(bar_width * percentage / 100))))
            bar = Text("━" * filled, style="bar.complete")
            bar.append("─" * (bar_width - filled), style="bar.back")
            summary = Text()
            summary.append(bar)
            summary.append(f"  {percentage:5.1f}%  ")
            summary.append(f"{_number_text(float(event.current))}/{_number_text(float(event.total))}")
            if event.unit:
                summary.append(f" {event.unit}")
            eta = event.eta_seconds
            if eta is not None:
                summary.append(f"  ·  ETA {_duration(eta)}")
            rows.append(summary)
        else:
            rows.append(Spinner("dots", text=Text(" Working", style="cyan")))
            if _finite_number(event.current) and event.unit:
                rows.append(Text(
                    f"Observed {_number_text(float(event.current))} {event.unit}; total unavailable",
                    style="dim",
                ))

        elapsed = 0.0 if self._started_at is None else max(0.0, time.monotonic() - self._started_at)
        rows.append(Text(f"Elapsed {_duration(elapsed)}", style="dim"))
        if event.detail:
            rows.append(Text(event.detail, style=status_style if event.state != "running" else "dim"))
        return Panel(Group(*rows), title=status, border_style=status_style)
