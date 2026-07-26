"""The track table: which columns exist and which of them fit.

Column choice is width-driven, so it is kept away from the app's state machine.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from textual import events
from textual.message import Message
from textual.widgets import DataTable

from settag.policy import Prediction, select_predictions
from settag.tags import OwnedValues, task_evidence_from_owned
from settag.tasks import AnalysisTask
from settag.tui.entries import TrackEntry, latest_analyzed_at, suggested_label


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

    # ui-count: terminal column arithmetic, unrelated to any domain object
    total_width = sum(render_width(key) for key in widths)
    if total_width > available:
        widths["track"] = max(
            1,
            available - render_width("selected") - column_padding,
        )
        # ui-count: terminal column arithmetic, unrelated to any domain object
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


@dataclass(frozen=True)
class RowContext:
    """The review settings every rendered row depends on.

    Passing these explicitly is what lets a row be rendered, and tested,
    without constructing an app.
    """

    tasks: tuple[AnalysisTask, ...]
    review_top: int
    score_cutoff: float

    def select_for_review(self, evidence: Sequence[Prediction]) -> list[Prediction]:
        return select_predictions(
            evidence,
            threshold=self.score_cutoff,
            top=self.review_top,
        )


def primary_review_predictions(entry: TrackEntry, context: RowContext) -> list[Prediction]:
    """Evidence from the first task in play that has anything worth reviewing."""
    owned: OwnedValues | None
    if entry.plan is not None:
        owned = entry.plan.desired
    elif entry.metadata is not None:
        owned = entry.metadata.owned
    else:
        owned = None
    if owned is None:
        return []

    evidence_by_task = task_evidence_from_owned(owned)
    for task in context.tasks:
        evidence = evidence_by_task.get(task, ())
        if not evidence and task == "genre" and entry.metadata is not None:
            evidence = entry.metadata.stored_predictions
        selected = context.select_for_review(evidence)
        if selected:
            return selected
    return []


def entry_analyzed_at(entry: TrackEntry, context: RowContext) -> str:
    if entry.plan is not None:
        value = latest_analyzed_at(entry.plan.desired, context.tasks)
    elif entry.metadata is not None and entry.metadata.cached_plan is not None:
        value = latest_analyzed_at(entry.metadata.cached_plan.desired, context.tasks)
    else:
        value = entry.metadata.analyzed_at if entry.metadata is not None else None
    return value[:10] if value else "—"


def entry_analysis(entry: TrackEntry, context: RowContext) -> str:
    if entry.metadata_error is not None:
        return "Metadata error"
    if entry.analysis_error is not None:
        return "Analysis error"
    if entry.metadata is not None and entry.metadata.is_sample:
        duration = entry.metadata.duration_seconds
        return f"Sample · {round(duration)}s" if duration is not None else "Sample"

    analyzed_at = entry_analyzed_at(entry, context)
    if entry.plan is not None:
        state = "Ready" if entry.plan_cached else "New"
    else:
        assert entry.metadata is not None
        if entry.metadata.cache_status == "stale":
            state = "Reanalyze"
        elif entry.metadata.status == "not_analyzed":
            return "Never"
        else:
            state = {
                "current": "Up to date",
                "stale": "Reanalyze",
                "invalid": "Incomplete",
            }[entry.metadata.status]

    return f"{state} · {analyzed_at}" if analyzed_at != "—" else state


def row_cells(
    entry: TrackEntry,
    *,
    selected: bool,
    context: RowContext,
) -> tuple[str, str, str, str, str, str, str]:
    """Every cell for one track, in fixed column order."""
    plan = entry.plan
    metadata = entry.metadata
    predictions = primary_review_predictions(entry, context)
    primary = predictions[0] if predictions else None

    if plan is not None:
        before = ", ".join(plan.file_genre) or "None"
        if plan.target_file_genre is not None:
            after = ", ".join(plan.target_file_genre) or "None"
            file_genre = f"{before} → {after}"
        else:
            file_genre = before
    elif metadata is not None:
        file_genre = ", ".join(metadata.genre_state.standard) or "None"
    else:
        file_genre = "Unknown"

    return (
        "✓" if selected else "",
        entry.path.name,
        file_genre,
        entry_analysis(entry, context),
        suggested_label(predictions) or "—",
        f"{primary.score:.3f}" if primary else "—",
        str(len(plan.readable_changes)) if plan is not None else "—",
    )


def visible_row_cells(
    entry: TrackEntry,
    *,
    selected: bool,
    context: RowContext,
    layout: Sequence[tuple[TrackTableColumn, int]],
) -> tuple[str, ...]:
    """The cells for the columns that currently fit."""
    cells = row_cells(entry, selected=selected, context=context)
    return tuple(cells[column.cell_index] for column, _width in layout)
