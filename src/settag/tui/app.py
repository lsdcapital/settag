"""SetTag's Textual app: composes the core state with each flow.

Textual reads class-level configuration (``BINDINGS``, ``CSS``, ``TITLE``,
...) from the concrete app class, so those attributes live here rather than
on ``SetTagAppCore`` or any flow mixin.
"""

from __future__ import annotations

from textual.binding import Binding

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
