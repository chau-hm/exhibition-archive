from __future__ import annotations

import io
from types import ModuleType
import unittest
from unittest.mock import patch

from exhibition_archive import progress_ui


class TTYStream(io.StringIO):
    def isatty(self):
        return True


class FakeText:
    def __init__(self, text="", style=None):
        self.parts = [str(text)]
        self.style = style

    @property
    def plain(self):
        return "".join(self.parts)

    def append(self, text, style=None):
        self.parts.append(flatten(text))


class FakeGroup:
    def __init__(self, *items):
        self.items = items


class FakePanel:
    def __init__(self, renderable, title="", border_style=None):
        self.renderable = renderable
        self.title = title
        self.border_style = border_style


class FakeSpinner:
    def __init__(self, name, text="", **kwargs):
        self.name = name
        self.text = text


def flatten(renderable):
    if isinstance(renderable, FakeText):
        return renderable.plain
    if isinstance(renderable, FakeGroup):
        return "\n".join(flatten(item) for item in renderable.items)
    if isinstance(renderable, FakePanel):
        return f"{renderable.title}\n{flatten(renderable.renderable)}"
    if isinstance(renderable, FakeSpinner):
        return f"[{renderable.name}]{flatten(renderable.text)}"
    return str(renderable)


class FakeConsole:
    instances = []

    def __init__(self, *, file, **kwargs):
        self.file = file
        self.kwargs = kwargs
        self.instances.append(self)


class FakeLive:
    instances = []

    def __init__(self, *, console, get_renderable, **kwargs):
        self.console = console
        self.get_renderable = get_renderable
        self.kwargs = kwargs
        self.frames = []
        self.started = False
        self.stopped = False
        self.instances.append(self)

    def start(self, refresh=False):
        self.started = True
        if refresh:
            self.refresh()

    def refresh(self):
        self.frames.append(self.get_renderable())

    def stop(self):
        self.stopped = True


def fake_rich_modules():
    modules = {}
    package = ModuleType("rich")
    package.__path__ = []
    modules["rich"] = package

    console = ModuleType("rich.console")
    console.Console = FakeConsole
    console.Group = FakeGroup
    live = ModuleType("rich.live")
    live.Live = FakeLive
    panel = ModuleType("rich.panel")
    panel.Panel = FakePanel
    spinner = ModuleType("rich.spinner")
    spinner.Spinner = FakeSpinner
    text = ModuleType("rich.text")
    text.Text = FakeText
    modules.update({
        "rich.console": console,
        "rich.live": live,
        "rich.panel": panel,
        "rich.spinner": spinner,
        "rich.text": text,
    })
    return modules


def event(**changes):
    fields = {
        "stage_id": "sfm",
        "stage_label": "Reconstruction",
        "operation": "Registering cameras",
        "stage_number": 2,
        "stage_total": 6,
    }
    fields.update(changes)
    return progress_ui.ProgressEvent(**fields)


class ProgressEventTests(unittest.TestCase):
    def test_measured_percentage_and_observed_rate_produce_eta(self):
        update = event(current=12, total=40, unit="images", rate_per_second=0.8)
        self.assertEqual(update.percentage, 30)
        self.assertEqual(update.eta_seconds, 35)

    def test_absent_or_invalid_measurements_do_not_fabricate_percentage_or_eta(self):
        cases = (
            (event(current=12, unit="images", rate_per_second=0.8), None),
            (event(current=12, total=0, unit="images", rate_per_second=0.8), None),
            (event(current=12, total=10, unit="images", rate_per_second=0.8), None),
            (event(current=12, total=40, unit="images", rate_per_second=None), 30),
            (event(current=12, total=40, unit="images", rate_per_second=0), 30),
        )
        for update, expected_percentage in cases:
            with self.subTest(update=update):
                self.assertEqual(update.percentage, expected_percentage)
                self.assertIsNone(update.eta_seconds)


class RichProgressRendererTests(unittest.TestCase):
    def setUp(self):
        FakeConsole.instances.clear()
        FakeLive.instances.clear()

    def test_stage_transition_progress_eta_and_success_are_rendered(self):
        stream = TTYStream()
        logged = []
        with patch.dict("sys.modules", fake_rich_modules()):
            with progress_ui.RichProgressRenderer(stream=stream, event_sink=logged.append) as view:
                view.update(event(current=12, total=40, unit="images", rate_per_second=0.8))
                view.update(event(stage_id="training", stage_label="Training", operation="Optimizing", stage_number=4,
                                  current=50, total=100, unit="iterations", rate_per_second=2))

        live = FakeLive.instances[-1]
        frame_text = [flatten(frame) for frame in live.frames]
        self.assertTrue(any("Stage 2/6 · Reconstruction" in frame for frame in frame_text))
        self.assertTrue(any("Stage 4/6 · Training" in frame and "50.0%" in frame for frame in frame_text))
        self.assertTrue(any("ETA 00:00:25" in frame for frame in frame_text))
        self.assertEqual(live.frames[-1].title, "Completed")
        self.assertIn("completed", [item.state for item in logged])
        self.assertEqual(live.kwargs["screen"], False)
        self.assertEqual(live.kwargs["transient"], False)
        self.assertFalse(live.kwargs["redirect_stdout"])
        self.assertFalse(live.kwargs["redirect_stderr"])
        self.assertTrue(live.stopped)

    def test_indeterminate_progress_has_spinner_without_percentage_or_eta(self):
        stream = TTYStream()
        with patch.dict("sys.modules", fake_rich_modules()):
            view = progress_ui.RichProgressRenderer(stream=stream, tty=True)
            view.start()
            view.update(event(current=8, unit="images", rate_per_second=2))
            frame = flatten(FakeLive.instances[-1].frames[-1])
            view.finish("completed")

        self.assertIn("[dots] Working", frame)
        self.assertIn("total unavailable", frame)
        self.assertNotIn("%", frame)
        self.assertNotIn("ETA", frame)

    def test_failure_and_keyboard_interrupt_set_terminal_state_and_restore_live(self):
        cases = ((RuntimeError("synthetic failure"), "failed"),
                 (KeyboardInterrupt(), "cancelled"))
        for exception, expected_state in cases:
            with self.subTest(state=expected_state), patch.dict("sys.modules", fake_rich_modules()):
                view = progress_ui.RichProgressRenderer(stream=TTYStream(), tty=True)
                with self.assertRaises(type(exception)):
                    with view:
                        view.update(event(current=1, total=3, unit="images"))
                        raise exception
                self.assertEqual(view._last_event.state, expected_state)
                self.assertEqual(FakeLive.instances[-1].frames[-1].title, expected_state.title())
                self.assertTrue(FakeLive.instances[-1].stopped)

    def test_non_tty_records_events_without_screen_or_ansi_output(self):
        stream = io.StringIO()
        logged = []
        with patch.dict("sys.modules", fake_rich_modules()):
            with progress_ui.RichProgressRenderer(stream=stream, event_sink=logged.append) as view:
                view.update(event(current=1, total=2, unit="images"))

        self.assertFalse(view.interactive)
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(len(FakeLive.instances), 0)
        self.assertEqual([row.state for row in logged], ["running", "completed"])

    def test_stdout_is_untouched_and_rich_is_bound_to_the_given_stderr_stream(self):
        stream = TTYStream()
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch.dict("sys.modules", fake_rich_modules()):
            with progress_ui.RichProgressRenderer(stream=stream, tty=True) as view:
                view.update(event(current=1, total=2, unit="images"))

        self.assertEqual(stdout.getvalue(), "")
        self.assertIs(FakeConsole.instances[-1].file, stream)

    def test_tty_without_rich_degrades_to_event_sink_only(self):
        stream = TTYStream()
        logged = []
        with patch.object(progress_ui, "import_module", side_effect=ImportError("Rich not installed")):
            with progress_ui.RichProgressRenderer(stream=stream, event_sink=logged.append, tty=True) as view:
                view.update(event(current=1, total=2, unit="images"))

        self.assertFalse(view.interactive)
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual([row.state for row in logged], ["running", "completed"])


if __name__ == "__main__":
    unittest.main()
