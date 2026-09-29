"""SetTag's Textual app: composes the core state with each flow.

Textual reads class-level configuration (``BINDINGS``, ``CSS``, ``TITLE``,
...) from the concrete app class, so those attributes live here rather than
on ``SetTagAppCore`` or any flow mixin.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.worker import Worker, WorkerState

from settag.tui.analysis_flow import AnalysisFlow
from settag.tui.style import APP_CSS
from settag.tui.undo_flow import UndoFlow
from settag.tui.write_flow import WriteFlow


class SetTagApp(AnalysisFlow, WriteFlow, UndoFlow):
    """Metadata-first library browser and explicit analysis/write workflow."""

    TITLE = "SetTag"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("w", "write", "Write"),
        Binding("r", "analyze", "Enrich"),
        Binding("space", "toggle_track", "Toggle"),
        Binding("a", "toggle_all", "All/None"),
        Binding("i", "toggle_details", "Details"),
        Binding("f", "cycle_filter", "Library filter"),
        Binding("g", "cycle_genre_filter", "Genre filter"),
        Binding("escape", "cancel_analysis", "Cancel"),
        Binding("v", "review", "Review"),
        Binding("b", "library", "Library"),
        Binding("e", "edit_genre", "Genre"),
        Binding("s", "save", "Save plan"),
        Binding("h", "hygiene", "Hygiene"),
        Binding("u", "undo", "Undo"),
        Binding("q", "quit", "Quit"),
    ]

    CSS = APP_CSS

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Recover from an exception a worker did not catch itself.

        Every worker runs with ``exit_on_error=False`` and handles its expected failures.
        Anything else would leave ``busy`` or the analysis queue set, and quitting refuses
        while either is, so the app could only be killed. Route it to the flow's own failure
        handler, which resets that state and shows the error.
        """
        if event.state != WorkerState.ERROR:
            return
        error = event.worker.error
        message = f"{type(error).__name__}: {error}"
        group = event.worker.group
        if group == "analysis":
            self._analysis_failed(message)
        elif group in {"write", "save"}:
            self._write_failed(
                "Write stopped unexpectedly",
                f"{message}\n\nFiles already written were journaled and can be undone.",
            )
        elif group == "undo":
            self._undo_failed("Undo stopped unexpectedly", message)
        elif group == "metadata":
            self._show_fatal_error(message)
