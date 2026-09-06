"""The track table: which columns exist and which of them fit.

Column choice is width-driven, so it is kept away from the app's state machine.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import DataTable

from settag.freshness import current_catalog_evidence
from settag.plans import catalog_genre_summary, catalog_genres, standard_genre_from_model_label
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
    TrackTableColumn("suggested_genre", "Suggested genre", 4, 16, 20),
    TrackTableColumn("analysis", "Enrichment", 3, 12, 24),
    TrackTableColumn("write_plan", "Write plan", 5, 16, 16),
)

TRACK_TABLE_COLUMN_BY_KEY = {column.key: column for column in TRACK_TABLE_COLUMNS}

TRACK_TABLE_COLUMN_PRIORITY = (
    "file_genre",
    "suggested_genre",
    "analysis",
    "write_plan",
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
            available // 4,
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
    for key in ("suggested_genre", "file_genre", "analysis"):
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


def entry_analysis(entry: TrackEntry, _context: RowContext) -> str:
    if entry.metadata_error is not None:
        return "Metadata error"
    if entry.analysis_error is not None:
        return "Analysis error"
    if entry.metadata is not None and entry.metadata.is_sample:
        duration = entry.metadata.duration_seconds
        return f"Sample · {round(duration)}s" if duration is not None else "Sample"

    if entry.plan is not None:
        state = (
            "Partial"
            if entry.plan.enrichment_status == "partial"
            else "Needs enrichment"
            if entry.plan_cached and entry.plan.enrichment_status != "current"
            else "Ready"
        )
    else:
        assert entry.metadata is not None
        if entry.metadata.enrichment_status == "partial":
            state = "Partial"
        elif entry.metadata.cache_status == "stale":
            state = "Needs enrichment"
        elif entry.metadata.status == "not_analyzed":
            return "Never"
        else:
            state = {
                "current": {
                    "current": "Current",
                    "needs_enrichment": "Needs enrichment",
                    "partial": "Partial",
                }[entry.metadata.enrichment_status],
                "stale": "Reanalyze",
                "invalid": "Incomplete",
            }[entry.metadata.status]

    return state


GENRE_MATCH_STYLE = "#8ea09b"
GENRE_REVIEW_STYLE = "#e0b36b"


@dataclass(frozen=True)
class GenreCheck:
    """Compare the observed file genre with genre evidence, independently of freshness."""

    summary: str
    explanation: str
    model_genre: str | None = None
    suggested_genre: str | None = None
    relation: Literal["unknown", "match", "different", "missing"] = "unknown"

    @property
    def suggestion_text(self) -> str:
        if self.suggested_genre is None:
            return self.summary
        prefix = "✓ " if self.relation == "match" else ""
        return f"{prefix}{self.suggested_genre}"

    @property
    def suggestion_style(self) -> str:
        return (
            GENRE_REVIEW_STYLE if self.relation in {"different", "missing"} else GENRE_MATCH_STYLE
        )

    @property
    def details(self) -> tuple[str, ...]:
        lines = [f"Genre check: {self.summary}"]
        if self.model_genre is not None:
            lines.append(f"Model genre: {self.model_genre}")
        if self.suggested_genre is not None:
            lines.append(f"Suggested file genre: {self.suggested_genre}")
        lines.append(self.explanation)
        return tuple(lines)


def genre_check(entry: TrackEntry, context: RowContext) -> GenreCheck:
    metadata = entry.metadata
    plan = entry.plan
    if entry.metadata_error is not None or entry.analysis_error is not None:
        return GenreCheck("Unavailable", "Resolve the track error before comparing its genre.")
    owned = (
        plan.evidence_view
        if plan is not None
        else metadata.evidence_view
        if metadata is not None
        else {}
    )
    catalog = current_catalog_evidence(owned)
    if plan is None and metadata is not None and not metadata.catalog_current:
        catalog = None
    if catalog:
        current_genres = (
            plan.file_genre if plan else metadata.genre_state.standard if metadata else ()
        )
        genres = catalog_genres(owned, current_genres)
        matches = {g.casefold() for g in genres} == {g.casefold() for g in current_genres}
        return GenreCheck(
            "Matches catalog" if matches else catalog_genre_summary(owned),
            "Verified matching releases provide: " + ", ".join(genres),
            suggested_genre=", ".join(genres),
            relation="match" if matches else "different" if current_genres else "missing",
        )
    if "genre" not in context.tasks:
        return GenreCheck("Not assessed", "No genre evidence is available.")
    if plan is not None:
        current = plan.file_genre
        evidence = task_evidence_from_owned(plan.desired).get("genre", plan.evidence)
    elif metadata is not None:
        current = metadata.genre_state.standard
        if metadata.cache_status == "stale" or metadata.status in {"stale", "invalid"}:
            return GenreCheck(
                "Reanalyze", "Stored analysis needs refreshing before genre comparison."
            )
        if metadata.is_sample:
            return GenreCheck("Not assessed", "This sample is too short for genre analysis.")
        if metadata.status == "not_analyzed":
            return GenreCheck("Not analyzed", "Run genre analysis to obtain a suggestion.")
        evidence = task_evidence_from_owned(metadata.owned).get(
            "genre", metadata.stored_predictions
        )
    else:
        return GenreCheck("Unavailable", "No readable metadata is available.")
    chosen = context.select_for_review(evidence)
    if not chosen:
        return GenreCheck("No suggestion", "No genre candidate met the review cutoff.")
    model = suggested_label(chosen)
    suggestion = standard_genre_from_model_label(chosen[0].label)
    if model is None or suggestion is None:
        return GenreCheck("No suggestion", "No usable genre label is available.")

    values = {value.strip().casefold() for value in current if value.strip()}
    relation: Literal["match", "different", "missing"] = "match"
    if values == {model.casefold()}:
        summary = "Matches model"
        explanation = "The file already has the model's detailed genre; no replacement is needed."
    elif values == {suggestion.casefold()}:
        summary = "Matches suggestion"
        explanation = f"{model} maps to {suggestion} for the file tag; the existing genre matches."
    elif model.casefold() in values or suggestion.casefold() in values:
        summary = "Includes suggestion"
        explanation = "One of the file's genres matches; additional genres are also present."
    elif not values:
        relation = "missing"
        summary = f"Missing → {suggestion}"
        explanation = "The file has no genre. The suggestion is a choice to review."
    else:
        relation = "different"
        summary = f"Differs → {suggestion}"
        explanation = "The file genre differs from the suggestion. This does not mean it is wrong."
    return GenreCheck(summary, explanation, model, suggestion, relation)


def row_cells(
    entry: TrackEntry,
    *,
    selected: bool,
    context: RowContext,
) -> tuple[str, str, str, str, str, str]:
    """Every cell for one track, in fixed column order."""
    plan = entry.plan
    metadata = entry.metadata

    if plan is not None:
        file_genre = ", ".join(plan.file_genre) or "None"
    elif metadata is not None:
        file_genre = ", ".join(metadata.genre_state.standard) or "None"
    else:
        file_genre = "Unknown"

    return (
        "✓" if selected else "",
        entry.path.name,
        file_genre,
        entry_analysis(entry, context),
        genre_check(entry, context).suggestion_text,
        plan.write_plan_label if plan is not None else "—",
    )


def visible_row_cells(
    entry: TrackEntry,
    *,
    selected: bool,
    context: RowContext,
    layout: Sequence[tuple[TrackTableColumn, int]],
) -> tuple[str | Text, ...]:
    """The cells for the columns that currently fit, with genre review cues."""
    cells = row_cells(entry, selected=selected, context=context)
    visible: list[str | Text] = []
    for column, width in layout:
        value = cells[column.cell_index]
        # ui-count: fit display text within the visible column width
        if column.key == "analysis" and len(value) > width:
            value = value.split(" · ", 1)[0]
        if column.key == "suggested_genre":
            visible.append(Text(value, style=genre_check(entry, context).suggestion_style))
        else:
            visible.append(value)
    return tuple(visible)
