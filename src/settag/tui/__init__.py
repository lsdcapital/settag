"""SetTag's interactive Textual app.

Split across ``app`` (phases and background work), ``screens`` (modal
dialogs), ``table`` (column layout), ``entries`` (displayed state), and
``style`` (the stylesheet). The package keeps the original module name so
existing imports of ``settag.tui`` continue to resolve.
"""

from settag.tui.app import SetTagApp
from settag.tui.entries import TrackEntry, TuiOutcome
from settag.tui.screens import (
    ConfirmUndoScreen,
    ConfirmWriteScreen,
    ErrorScreen,
    GenreEditScreen,
    UndoScreen,
)

__all__ = [
    "ConfirmUndoScreen",
    "ConfirmWriteScreen",
    "ErrorScreen",
    "GenreEditScreen",
    "SetTagApp",
    "TrackEntry",
    "TuiOutcome",
    "UndoScreen",
]
