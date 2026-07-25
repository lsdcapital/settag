"""The track table: which columns exist and which of them fit.

Column choice is width-driven, so it is kept away from the app's state machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual import events
from textual.message import Message
from textual.widgets import DataTable


@dataclass(frozen=True)
class TrackTableColumn:
    key: str
    label: str
    cell_index: int
    min_width: int
    max_width: int


class ResponsiveTrackTable(DataTable):
    """Announce that this table received a new layout width.

    The widget posts a message rather than calling back into the app, so column
    layout stays independent of the app that happens to host it.
    """

    class Resized(Message):
        """The track table settled at a new width."""

    def on_resize(self, _event: events.Resize) -> None:
        self.post_message(self.Resized())


TRACK_TABLE_COLUMNS = (
    TrackTableColumn("selected", "", 0, 1, 1),
    TrackTableColumn("track", "Track", 1, 8, 1_000),
    TrackTableColumn("file_genre", "File genre", 2, 10, 18),
    TrackTableColumn("analysis", "Analysis", 3, 12, 24),
    TrackTableColumn("suggested", "Suggested", 4, 10, 20),
    TrackTableColumn("score", "Score", 5, 5, 5),
    TrackTableColumn("changes", "Changes", 6, 7, 7),
)

TRACK_TABLE_COLUMN_BY_KEY = {column.key: column for column in TRACK_TABLE_COLUMNS}

TRACK_TABLE_COLUMN_PRIORITY = (
    "analysis",
    "file_genre",
    "suggested",
    "changes",
    "score",
)


def _track_table_layout(
    viewport_width: int,
    *,
    cell_padding: int = 1,
    scrollbar_width: int = 2,
) -> tuple[tuple[TrackTableColumn, int], ...]:
    """Fit the most useful columns inside the table's visible width."""
    available = max(1, viewport_width - scrollbar_width)
    column_padding = 2 * cell_padding
    widths = {
        "selected": TRACK_TABLE_COLUMN_BY_KEY["selected"].min_width,
        "track": max(
            TRACK_TABLE_COLUMN_BY_KEY["track"].min_width,
            available // 3,
        ),
    }

    def render_width(key: str) -> int:
        return widths[key] + column_padding

    total_width = sum(render_width(key) for key in widths)
    if total_width > available:
        widths["track"] = max(
            1,
            available - render_width("selected") - column_padding,
        )
        total_width = sum(render_width(key) for key in widths)

    for key in TRACK_TABLE_COLUMN_PRIORITY:
        column = TRACK_TABLE_COLUMN_BY_KEY[key]
        candidate_width = column.min_width + column_padding
        if total_width + candidate_width > available:
            break
        widths[key] = column.min_width
        total_width += candidate_width

    remaining = max(0, available - total_width)
    for key in ("analysis", "file_genre", "suggested"):
        if key not in widths:
            continue
        column = TRACK_TABLE_COLUMN_BY_KEY[key]
        expansion = min(remaining, column.max_width - widths[key])
        widths[key] += expansion
        remaining -= expansion
    widths["track"] += remaining

    return tuple(
        (column, widths[column.key]) for column in TRACK_TABLE_COLUMNS if column.key in widths
    )
