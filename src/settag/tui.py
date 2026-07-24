from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Literal

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    Static,
)

from settag.plans import (
    PlannedWrite,
    stage_default_file_genre,
    stage_file_genre,
    suggested_file_genre,
)
from settag.policy import Prediction, select_predictions
from settag.workflow import (
    AnalysisBatch,
    AnalysisFailure,
    CancelCallback,
    MetadataBatch,
    MetadataTrack,
    PartialWriteError,
    ProgressCallback,
    apply_prepared,
    preflight_plan,
    save_plan,
)

MetadataLoader = Callable[[ProgressCallback], MetadataBatch]
AnalysisLoader = Callable[
    [Sequence[Path], ProgressCallback, CancelCallback],
    AnalysisBatch,
]
PlanPersister = Callable[[PlannedWrite], None]
PlanDiscarder = Callable[[Sequence[Path]], None]
AppPhase = Literal["choose", "review"]
LibraryFilter = Literal["all", "needs_analysis", "missing_genre", "current"]

FILTER_ORDER: tuple[LibraryFilter, ...] = (
    "all",
    "needs_analysis",
    "missing_genre",
    "current",
)
FILTER_LABELS: dict[LibraryFilter, str] = {
    "all": "All",
    "needs_analysis": "Needs analysis",
    "missing_genre": "Missing genre",
    "current": "Up to date",
}
CHOOSE_ACTIONS = frozenset(
    {
        "toggle_track",
        "toggle_all",
        "toggle_details",
        "cycle_filter",
        "review",
        "analyze",
        "quit",
    }
)
REVIEW_ACTIONS = frozenset(
    {
        "toggle_track",
        "toggle_all",
        "toggle_details",
        "library",
        "edit_genre",
        "save",
        "write",
        "quit",
    }
)
STATUS_LABELS = {
    "not_analyzed": "Never analyzed",
    "current": "Up to date",
    "stale": "Reanalyze (model/config changed)",
    "invalid": "Incomplete metadata",
}


@dataclass(frozen=True)
class TuiOutcome:
    status: int
    message: str


@dataclass
class TrackEntry:
    path: Path
    metadata: MetadataTrack | None = None
    metadata_error: AnalysisFailure | None = None
    plan: PlannedWrite | None = None
    plan_cached: bool = False
    analysis_error: AnalysisFailure | None = None

    @property
    def can_analyze(self) -> bool:
        return self.metadata is not None and self.metadata_error is None

    @property
    def needs_analysis(self) -> bool:
        return self.metadata is not None and self.plan is None and self.metadata.needs_analysis


@dataclass(frozen=True)
class TrackTableColumn:
    key: str
    label: str
    cell_index: int
    min_width: int
    max_width: int


class ResponsiveTrackTable(DataTable):
    """Notify the app after this table receives its final layout width."""

    def on_resize(self, _event: events.Resize) -> None:
        app = self.app
        if isinstance(app, SetTagApp):
            app.call_after_refresh(app._sync_table_columns)


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


def _suggested_label(predictions: Sequence[Prediction]) -> str | None:
    """Return the direct child label without performing taxonomy mapping."""
    if not predictions:
        return None
    return predictions[0].label.rsplit("---", 1)[-1].strip() or None


class GenreEditScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, item: PlannedWrite) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        current = (
            self.item.target_file_genre
            if self.item.target_file_genre is not None
            else self.item.file_genre
        )
        suggestion = suggested_file_genre(self.item)
        with Vertical(id="genre-dialog"):
            yield Label("Set standard file genre", id="dialog-title")
            yield Static(
                "Enter one or more genres, separated by commas.\n"
                "An empty field clears the conventional genre tag.",
                markup=False,
                id="dialog-help",
            )
            yield Input(
                value=", ".join(current),
                placeholder="Genre",
                id="genre-input",
            )
            if suggestion:
                model_child = _suggested_label(self.item.selected)
                source = (
                    f" (from model label {model_child})"
                    if model_child and model_child != suggestion
                    else ""
                )
                yield Static(
                    f"Standard genre suggestion: {suggestion}{source}",
                    markup=False,
                    id="dialog-suggestion",
                )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                if suggestion:
                    yield Button("Use suggestion", id="use-suggestion")
                yield Button("Stage entered genre", variant="primary", id="stage")

    def on_mount(self) -> None:
        self.set_class(self.size.width < 64, "narrow")
        self.query_one("#genre-input", Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 64, "narrow")

    @on(Input.Submitted)
    def submit_genre(self) -> None:
        self._submit()

    @on(Button.Pressed, "#stage")
    def stage_genre(self) -> None:
        self._submit()

    @on(Button.Pressed, "#use-suggestion")
    def use_suggestion(self) -> None:
        suggestion = suggested_file_genre(self.item)
        if suggestion is not None:
            self.dismiss(suggestion)

    @on(Button.Pressed, "#cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        value = self.query_one("#genre-input", Input).value.strip()
        self.dismiss(value)


class ConfirmWriteScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("enter,y", "confirm", "Write"),
        Binding("n,escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        *,
        track_count: int,
        standard_genre_count: int,
        evidence_count: int,
    ) -> None:
        super().__init__()
        self.track_count = track_count
        self.standard_genre_count = standard_genre_count
        self.evidence_count = evidence_count

    def compose(self) -> ComposeResult:
        noun = "track" if self.track_count == 1 else "tracks"
        bundle_noun = "bundle" if self.track_count == 1 else "bundles"
        edit_noun = "edit" if self.standard_genre_count == 1 else "edits"
        with Vertical(id="confirm-dialog"):
            yield Label("Write selected tracks?", id="dialog-title")
            yield Static(
                f"{self.track_count} {noun}\n"
                f"{self.track_count} SetTag analysis {bundle_noun}"
                f" · {self.evidence_count} ranked scores\n"
                f"{self.standard_genre_count} standard genre {edit_noun}",
                markup=False,
                id="confirm-summary",
            )
            yield Static(
                "The files passed preflight. SetTag will verify each file after writing.",
                markup=False,
                id="dialog-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Back to review", id="cancel")
                yield Button(
                    f"Write {self.track_count} {noun}",
                    variant="primary",
                    id="confirm",
                )

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).focus()

    @on(Button.Pressed, "#confirm")
    def confirm_button(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def cancel_button(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ErrorScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,enter", "close", "Close")]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.error_title = title
        self.error_message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="error-dialog"):
            yield Label(self.error_title, id="dialog-title")
            yield Static(self.error_message, markup=False, id="error-message")
            yield Button("Close", id="close", variant="primary")

    @on(Button.Pressed, "#close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class SetTagApp(App[TuiOutcome]):
    """Metadata-first library browser and explicit analysis/write workflow."""

    TITLE = "SetTag"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("space", "toggle_track", "Toggle"),
        Binding("a", "toggle_all", "All/None"),
        Binding("i", "toggle_details", "Details"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("r", "analyze", "Analyze"),
        Binding("escape", "cancel_analysis", "Cancel"),
        Binding("v", "review", "Review"),
        Binding("b", "library", "Library"),
        Binding("e", "edit_genre", "Genre"),
        Binding("s", "save", "Save plan"),
        Binding("w", "write", "Write"),
        Binding("q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        background: #0b0f0e;
        color: #eef2f1;
    }

    Header {
        background: #16201e;
        color: #eef2f1;
    }

    Footer {
        background: #16201e;
        color: #c3cecb;
    }

    Footer > .footer--highlight,
    Footer > .footer--key {
        background: #d0794f;
        color: #1f0e05;
    }

    #loading {
        align: center middle;
        height: 1fr;
        padding: 2 6;
    }

    #loading-title {
        text-style: bold;
        color: #eef2f1;
        margin-bottom: 1;
    }

    #loading-path {
        color: #8ea09b;
        margin-bottom: 1;
    }

    #metadata-progress {
        width: 72;
        max-width: 90%;
        margin-top: 1;
    }

    #main {
        display: none;
        height: 1fr;
    }

    #context {
        height: 3;
        padding: 1 2 0 2;
        color: #8ea09b;
        background: #0f1413;
    }

    #analysis-activity {
        display: none;
        height: 6;
        padding: 1 2 0 2;
        background: #16201e;
    }

    #analysis-activity-title {
        height: 1;
        text-style: bold;
        color: #eef2f1;
    }

    #analysis-activity-file {
        height: 1;
        color: #c3cecb;
    }

    #analysis-progress {
        width: 100%;
        height: 1;
        margin-top: 1;
    }

    #workspace {
        height: 1fr;
    }

    #tracks-pane {
        width: 2fr;
        min-width: 48;
        padding: 0 1 1 1;
    }

    #inspector-pane {
        display: none;
        width: 1fr;
        min-width: 34;
        padding: 0 2 1 1;
        background: #0f1413;
    }

    SetTagApp.details-open #inspector-pane {
        display: block;
    }

    .section-title {
        height: 2;
        padding: 0 1;
        text-style: bold;
        color: #eef2f1;
    }

    DataTable {
        height: 1fr;
        background: #111716;
        color: #c3cecb;
        scrollbar-color: #3a4744;
        scrollbar-color-hover: #8ea09b;
        scrollbar-color-active: #d0794f;
    }

    DataTable > .datatable--header {
        background: #16201e;
        color: #eef2f1;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #d0794f;
        color: #1f0e05;
        text-style: bold;
    }

    #inspector {
        height: 1fr;
        padding: 0 1;
        color: #c3cecb;
        overflow-y: auto;
    }

    #status {
        height: 3;
        padding: 1 2;
        background: #16201e;
        color: #c3cecb;
    }

    ModalScreen {
        align: center middle;
        background: #000000 55%;
    }

    #genre-dialog,
    #confirm-dialog,
    #error-dialog {
        width: 64;
        max-width: 92%;
        height: auto;
        max-height: 86%;
        padding: 1 2;
        background: #16201e;
        border: solid #3a4744;
    }

    #dialog-title {
        text-style: bold;
        color: #eef2f1;
        margin-bottom: 1;
    }

    #dialog-help,
    #dialog-suggestion {
        color: #8ea09b;
        margin-bottom: 1;
    }

    #genre-input {
        margin-bottom: 1;
        border: tall #3a4744;
    }

    #genre-input:focus {
        border: tall #d0794f;
    }

    #confirm-summary,
    #error-message {
        margin-bottom: 1;
        color: #eef2f1;
        max-height: 16;
        overflow-y: auto;
    }

    .dialog-actions {
        height: 3;
        align-horizontal: right;
    }

    .dialog-actions Button {
        margin-left: 1;
    }

    GenreEditScreen.narrow .dialog-actions {
        height: 9;
        layout: vertical;
        align-horizontal: right;
    }

    #confirm-dialog #cancel {
        background: #25302d;
        color: #c3cecb;
    }

    #confirm-dialog #cancel:focus {
        background: #3a4744;
        color: #eef2f1;
    }

    #confirm-dialog #confirm,
    #confirm-dialog #confirm:focus {
        background: #d0794f;
        color: #1f0e05;
        text-style: bold;
    }

    Button.-primary {
        background: #d0794f;
        color: #1f0e05;
    }

    SetTagApp.narrow #workspace {
        layout: vertical;
    }

    SetTagApp.narrow #tracks-pane,
    SetTagApp.narrow #inspector-pane {
        width: 1fr;
        min-width: 0;
    }

    SetTagApp.narrow #tracks-pane {
        height: 3fr;
    }

    SetTagApp.narrow #inspector-pane {
        height: 2fr;
        padding: 0 1 1 1;
    }
    """

    def __init__(
        self,
        *,
        source: Path,
        analysis_loader: AnalysisLoader,
        metadata_loader: MetadataLoader | None = None,
        initial_metadata: MetadataBatch | None = None,
        persist_plan: PlanPersister | None = None,
        discard_plans: PlanDiscarder | None = None,
        review_top: int = 5,
        score_cutoff: float = 0.10,
    ) -> None:
        super().__init__()
        if metadata_loader is None and initial_metadata is None:
            raise ValueError("SetTagApp requires a metadata loader or initial metadata")
        self.source = source.expanduser().resolve()
        self.metadata_loader = metadata_loader
        self.analysis_loader = analysis_loader
        self.initial_metadata = initial_metadata
        self.persist_plan = persist_plan
        self.discard_plans = discard_plans
        self.review_top = review_top
        self.score_cutoff = score_cutoff
        self.entries: list[TrackEntry] = []
        self.visible_indices: list[int] = []
        self.analysis_selected: set[int] = set()
        self.write_selected: set[int] = set()
        self.review_indices: set[int] = set()
        self.phase: AppPhase = "choose"
        self.library_filter: LibraryFilter = "all"
        self.busy = False
        self._pending_analysis_indices: tuple[int, ...] = ()
        self._analysis_cancel_requested = Event()
        self._analysis_completed_count = 0
        self._analysis_success_count = 0
        self._analysis_failure_count = 0
        self._analysis_current_path: Path | None = None
        self._analysis_navigation_changed = False
        self._pending_write: tuple[PlannedWrite, ...] = ()
        self._written_count = 0
        self._table_layout: tuple[tuple[TrackTableColumn, int], ...] = ()
        self.sub_title = "Reading existing metadata"

    @property
    def analysis_running(self) -> bool:
        return bool(self._pending_analysis_indices)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="loading"):
            yield Static("Reading existing metadata", markup=False, id="loading-title")
            yield Static(str(self.source), markup=False, id="loading-path")
            yield Static("Opening file tags…", markup=False, id="loading-status")
            yield ProgressBar(total=100, show_eta=False, id="metadata-progress")
        with Vertical(id="main"):
            yield Static("", markup=False, id="context")
            with Vertical(id="analysis-activity"):
                yield Static(
                    "Preparing analysis",
                    markup=False,
                    id="analysis-activity-title",
                )
                yield Static("", markup=False, id="analysis-activity-file")
                yield ProgressBar(
                    total=1,
                    show_eta=False,
                    id="analysis-progress",
                )
            with Horizontal(id="workspace"):
                with Vertical(id="tracks-pane"):
                    yield Static("Library", markup=False, classes="section-title")
                    yield ResponsiveTrackTable(
                        cursor_type="row",
                        zebra_stripes=True,
                        id="tracks",
                    )
                with Vertical(id="inspector-pane"):
                    yield Static("Track details", markup=False, classes="section-title")
                    yield Static("", markup=False, id="inspector")
            yield Static("", markup=False, id="status")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.set_class(self.size.width < 100, "narrow")
        self._sync_table_columns()
        if self.initial_metadata is not None:
            self._show_metadata(self.initial_metadata)
        else:
            self._load_metadata()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 100, "narrow")
        self.call_after_refresh(self._sync_table_columns)

    def _sync_table_columns(self) -> None:
        table = self.query_one("#tracks", DataTable)
        layout = _track_table_layout(
            table.size.width,
            cell_padding=table.cell_padding,
        )
        if layout == self._table_layout:
            return

        preferred_index = self._current_index()
        self._table_layout = layout
        table.clear(columns=True)
        for column, width in layout:
            table.add_column(column.label, width=width, key=column.key)
        if self.entries:
            self._rebuild_table(
                preferred_index,
                refresh_surrounding=False,
            )

    def check_action(
        self,
        action: str,
        parameters: tuple[object, ...],
    ) -> bool | None:
        del parameters
        if action == "cancel_analysis":
            if not self.analysis_running:
                return False
            return None if self._analysis_cancel_requested.is_set() else True
        if action == "review":
            return self.phase == "choose" and not self.busy and bool(self.review_indices)
        if (
            self.analysis_running
            and self.phase == "choose"
            and action in {"toggle_track", "toggle_all", "analyze"}
        ):
            return False
        phase_actions = CHOOSE_ACTIONS if self.phase == "choose" else REVIEW_ACTIONS
        if action in CHOOSE_ACTIONS | REVIEW_ACTIONS:
            return action in phase_actions
        return True

    @work(thread=True, exclusive=True, group="metadata", exit_on_error=False)
    def _load_metadata(self) -> None:
        assert self.metadata_loader is not None
        try:
            metadata = self.metadata_loader(self._metadata_progress_from_worker)
        except Exception as error:
            self.call_from_thread(
                self._show_fatal_error,
                f"{type(error).__name__}: {error}",
            )
            return
        self.call_from_thread(self._show_metadata, metadata)

    def _metadata_progress_from_worker(
        self,
        completed: int,
        total: int,
        path: Path,
    ) -> None:
        self.call_from_thread(
            self._update_metadata_progress,
            completed,
            total,
            path.name,
        )

    def _update_metadata_progress(
        self,
        completed: int,
        total: int,
        name: str,
    ) -> None:
        self.query_one("#loading-status", Static).update(f"{completed} of {total} · {name}")
        self.query_one("#metadata-progress", ProgressBar).update(
            total=total,
            progress=completed,
        )

    def _show_fatal_error(self, message: str) -> None:
        self.exit(TuiOutcome(2, f"SetTag could not start: {message}"))

    def _show_metadata(self, metadata: MetadataBatch) -> None:
        entries = [
            TrackEntry(
                path=track.path,
                metadata=track,
                plan=self._ready_cached_plan(track),
                plan_cached=track.cache_status == "ready",
            )
            for track in metadata.tracks
        ]
        entries.extend(
            TrackEntry(path=failure.path, metadata_error=failure) for failure in metadata.failures
        )
        self.entries = sorted(entries, key=lambda entry: str(entry.path))
        self.analysis_selected = {
            index
            for index, entry in enumerate(self.entries)
            if entry.can_analyze and entry.needs_analysis
        }
        self.review_indices = {
            index for index, entry in enumerate(self.entries) if entry.plan is not None
        }
        self.write_selected = {
            index
            for index in self.review_indices
            if bool(self.entries[index].plan.readable_changes)
        }
        self.query_one("#loading").display = False
        self.query_one("#main").display = True
        self._show_library()
        if self.review_indices:
            restored = len(self.review_indices)
            self._update_status(
                f"Restored {restored} ready-to-review track"
                f"{'s' if restored != 1 else ''} from the local workbench"
                "  ·  Press V to review"
            )

    def _ready_cached_plan(self, track: MetadataTrack) -> PlannedWrite | None:
        if track.cache_status != "ready" or track.cached_plan is None:
            return None
        return replace(
            track.cached_plan,
            selected=tuple(self._select_for_review(track.cached_plan.evidence)),
        )

    def _select_for_review(
        self,
        evidence: Sequence[Prediction],
    ) -> list[Prediction]:
        return select_predictions(
            evidence,
            threshold=self.score_cutoff,
            top=self.review_top,
        )

    def _show_library(self) -> None:
        self.phase = "choose"
        self.sub_title = "Choose tracks to analyze"
        self.refresh_bindings()
        self.query_one("#tracks-pane .section-title", Static).update(
            "Library · choose tracks to analyze"
        )
        self._rebuild_table()

    def _show_review(self) -> None:
        self.phase = "review"
        self.sub_title = "Review analyzed tracks"
        self.refresh_bindings()
        self.query_one("#tracks-pane .section-title", Static).update(
            "Review · checked tracks will be written"
        )
        self._rebuild_table()

    def _filtered_indices(self) -> list[int]:
        if self.phase == "review":
            return sorted(self.review_indices)

        indices = range(len(self.entries))
        if self.library_filter == "all":
            return list(indices)
        if self.library_filter == "needs_analysis":
            return [index for index in indices if self.entries[index].needs_analysis]
        if self.library_filter == "missing_genre":
            return [
                index
                for index in indices
                if (
                    self.entries[index].metadata is not None
                    and not self.entries[index].metadata.genre_state.standard
                )
            ]
        return [
            index
            for index in indices
            if (
                self.entries[index].metadata is not None
                and self.entries[index].metadata.status == "current"
                and self.entries[index].plan is None
            )
        ]

    def _rebuild_table(
        self,
        preferred_index: int | None = None,
        *,
        refresh_surrounding: bool = True,
    ) -> None:
        if preferred_index is None:
            preferred_index = self._current_index()
        self.visible_indices = self._filtered_indices()
        table = self.query_one("#tracks", DataTable)
        table.clear()
        for index in self.visible_indices:
            table.add_row(*self._visible_row_cells(index), key=str(index))

        if refresh_surrounding:
            self._update_context()
            self._update_status()
        if not self.visible_indices:
            if refresh_surrounding:
                self.query_one("#inspector", Static).update("No tracks match this view.")
            return

        try:
            cursor_row = self.visible_indices.index(preferred_index)
        except ValueError:
            cursor_row = 0
        table.focus()
        table.move_cursor(row=cursor_row)
        if refresh_surrounding:
            self._update_inspector(self.visible_indices[cursor_row])

    def _visible_row_cells(self, index: int) -> tuple[str, ...]:
        cells = self._row_cells(index)
        return tuple(cells[column.cell_index] for column, _width in self._table_layout)

    def _row_cells(
        self,
        index: int,
    ) -> tuple[str, str, str, str, str, str, str]:
        entry = self.entries[index]
        plan = entry.plan
        metadata = entry.metadata
        selected = (
            index in self.analysis_selected
            if self.phase == "choose"
            else index in self.write_selected
        )
        predictions: Sequence[Prediction] = (
            plan.selected
            if plan is not None
            else self._select_for_review(metadata.stored_predictions)
            if metadata is not None
            else ()
        )
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
            self._entry_analysis(entry),
            _suggested_label(predictions) or "—",
            f"{primary.score:.3f}" if primary else "—",
            str(len(plan.readable_changes)) if plan is not None else "—",
        )

    def _entry_analysis(self, entry: TrackEntry) -> str:
        if entry.metadata_error is not None:
            return "Metadata error"
        if entry.analysis_error is not None:
            return "Analysis error"

        analyzed_at = self._entry_analyzed_at(entry)
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

    def _entry_analyzed_at(self, entry: TrackEntry) -> str:
        if entry.plan is not None:
            values = entry.plan.desired["SETTAG_ANALYZED_AT"]
            value = values[0] if values else None
        elif entry.metadata is not None and entry.metadata.cached_plan is not None:
            values = entry.metadata.cached_plan.desired["SETTAG_ANALYZED_AT"]
            value = values[0] if values else None
        else:
            value = entry.metadata.analyzed_at if entry.metadata is not None else None
        return value[:10] if value else "—"

    def _update_context(self) -> None:
        if self.phase == "choose":
            needs = sum(entry.needs_analysis for entry in self.entries)
            errors = sum(entry.metadata_error is not None for entry in self.entries)
            ready = len(self.review_indices)
            ready_text = f"  ·  {ready} ready to review" if ready else ""
            text = (
                f"{self.source}  ·  {len(self.entries)} tracks"
                f"  ·  {needs} need analysis"
                f"{ready_text}"
                f"  ·  {errors} metadata error{'s' if errors != 1 else ''}"
                f"  ·  Filter: {FILTER_LABELS[self.library_filter]}"
            )
        else:
            failures = sum(
                self.entries[index].analysis_error is not None for index in self.review_indices
            )
            text = (
                f"{self.source}  ·  {len(self.review_indices)} reviewed"
                f"  ·  {failures} analysis error{'s' if failures != 1 else ''}"
                "  ·  B returns to the library"
            )
        self.query_one("#context", Static).update(text)

    @on(DataTable.RowHighlighted, "#tracks")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        value = event.row_key.value
        if value is not None:
            self._update_inspector(int(value))

    @on(DataTable.RowSelected, "#tracks")
    def row_selected(self) -> None:
        if self.phase == "choose":
            self.action_analyze()
        else:
            self.action_write()

    def _current_index(self) -> int | None:
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self.visible_indices):
            return self.visible_indices[row]
        return None

    def _update_inspector(self, index: int) -> None:
        entry = self.entries[index]
        if self.phase == "choose":
            lines = self._metadata_inspector(entry, index)
        else:
            lines = self._review_inspector(entry, index)
        self.query_one("#inspector", Static).update("\n".join(lines))

    def _metadata_inspector(self, entry: TrackEntry, index: int) -> list[str]:
        lines = [entry.path.name, str(entry.path.parent), ""]
        if entry.metadata_error is not None:
            return [
                *lines,
                "Metadata could not be read",
                f"  {entry.metadata_error.description}",
                "",
                "This track cannot be analyzed safely until its metadata is readable.",
            ]

        assert entry.metadata is not None
        metadata = entry.metadata
        genre = ", ".join(metadata.genre_state.standard) or "None"
        cached_plan = metadata.cached_plan
        cache_status = (
            "Local result ready to review"
            if entry.plan is not None
            else (
                f"Local result needs reanalysis ({metadata.cache_reason})"
                if metadata.cache_status == "stale"
                else None
            )
        )
        predictions: Sequence[Prediction] = (
            entry.plan.evidence
            if entry.plan is not None
            else (
                cached_plan.evidence
                if cached_plan is not None and metadata.cache_status == "stale"
                else metadata.stored_predictions
            )
        )
        selected = self._select_for_review(predictions)
        lines.extend(
            [
                "Current file metadata",
                f"  Standard genre: {genre}",
                f"  SetTag status: {cache_status or STATUS_LABELS[metadata.status]}",
                f"  Last analyzed: {self._full_analyzed_at(entry)}",
                "",
                (
                    "Local review candidates (stale)"
                    if metadata.cache_status == "stale"
                    else (
                        "Local review candidates (ready)"
                        if entry.plan is not None
                        else "Review candidates from stored evidence"
                    )
                ),
            ]
        )
        if predictions:
            lines.extend(self._candidate_lines(predictions, selected))
        elif metadata.genre_state.settag:
            lines.extend(
                f"  • {label} (score metadata unavailable)" for label in metadata.genre_state.settag
            )
        else:
            lines.append("  None")

        lines.extend(
            [
                "",
                *(
                    [
                        "Last analysis attempt failed",
                        f"  {entry.analysis_error.description}",
                        "",
                    ]
                    if entry.analysis_error is not None
                    else []
                ),
                (
                    "Selected for analysis."
                    if index in self.analysis_selected
                    else "Not selected for analysis."
                ),
                *(["Press V to review this saved result."] if entry.plan is not None else []),
                "The audio model has not been loaded.",
            ]
        )
        return lines

    def _full_analyzed_at(self, entry: TrackEntry) -> str:
        if entry.plan is not None:
            values = entry.plan.desired["SETTAG_ANALYZED_AT"]
            return values[0] if values else "Never"
        if entry.metadata is not None and entry.metadata.cached_plan is not None:
            values = entry.metadata.cached_plan.desired["SETTAG_ANALYZED_AT"]
            return values[0] if values else "Never"
        if entry.metadata is not None:
            return entry.metadata.analyzed_at or "Never"
        return "Never"

    def _review_inspector(self, entry: TrackEntry, index: int) -> list[str]:
        lines = [entry.path.name, str(entry.path.parent), ""]
        if entry.analysis_error is not None:
            return [
                *lines,
                "Analysis failed",
                f"  {entry.analysis_error.description}",
                "",
                "Return to the library with B to retry or choose another track.",
            ]
        if entry.plan is None:
            return [*lines, "No analysis result is available."]

        item = entry.plan
        current = ", ".join(item.file_genre) or "None"
        if item.target_file_genre is None:
            genre_line = f"  {current} (unchanged)"
        else:
            target = ", ".join(item.target_file_genre) or "None"
            genre_line = f"  {current} → {target} (staged)"

        suggestion = suggested_file_genre(item)
        model_child = _suggested_label(item.selected)
        rollup_line = (
            f"  Suggested roll-up: {model_child} → {suggestion}"
            if suggestion and model_child and suggestion != model_child
            else None
        )
        lines.extend(
            [
                "Standard file genre",
                genre_line,
                *([rollup_line] if rollup_line else []),
                "",
                "Review candidates",
            ]
        )
        if item.evidence:
            lines.extend(self._candidate_lines(item.evidence, item.selected))
        else:
            lines.append("  No ranked evidence was returned by the model.")

        lines.extend(
            [
                "",
                "SetTag analysis bundle",
                f"  {len(item.evidence)} ranked scores with provenance",
                f"  {len(item.owned_changes)} internal field changes",
            ]
        )
        if item.readable_changes:
            lines.extend(f"  • {change}" for change in item.readable_changes)
        else:
            lines.append("  None")
        lines.extend(
            [
                "",
                ("Will be written." if index in self.write_selected else "Will not be written."),
                "The SetTag analysis bundle is always written together.",
                "The standard genre is a separate, editable staged change.",
            ]
        )
        return lines

    def _candidate_lines(
        self,
        evidence: Sequence[Prediction],
        selected: Sequence[Prediction],
    ) -> list[str]:
        cutoff = f"{self.score_cutoff:.3f}"
        if cutoff.endswith("0"):
            cutoff = cutoff[:-1]
        lines = [
            f"  Score cutoff ≥ {cutoff} · maximum {self.review_top}",
        ]
        if selected:
            width = max(len(prediction.label) for prediction in selected)
            lines.extend(
                f"  {rank:>2}. {prediction.label:<{width}}  {prediction.score:.3f}"
                for rank, prediction in enumerate(selected, start=1)
            )
        else:
            lines.append("  No candidate met the review cutoff.")

        hidden = len(evidence) - len(selected)
        if hidden:
            noun = "score" if hidden == 1 else "scores"
            lines.append(f"  {hidden} additional ranked {noun} stored for importing apps.")
        return lines

    def _refresh_row(self, index: int) -> None:
        if index not in self.visible_indices:
            return
        row = self.visible_indices.index(index)
        table = self.query_one("#tracks", DataTable)
        for column, value in enumerate(self._visible_row_cells(index)):
            table.update_cell_at(
                Coordinate(row, column),
                value,
            )
        self._update_inspector(index)
        self._update_status()

    def _update_status(self, message: str | None = None) -> None:
        if self.busy:
            if message is not None:
                self.query_one("#status", Static).update(message)
            return

        if self.analysis_running:
            if self._analysis_cancel_requested.is_set():
                progress = "Stopping after the current track"
            else:
                progress = (
                    "Analysis running in background"
                    f"  ·  {self._analysis_completed_count} of "
                    f"{len(self._pending_analysis_indices)} complete"
                )

            if self.phase == "choose":
                review_hint = (
                    f"  ·  V review {len(self.review_indices)} ready" if self.review_indices else ""
                )
                base = (
                    f"{progress}{review_hint}  ·  I details  ·  F filter  ·  Esc stop after current"
                )
            else:
                selected = len(self.write_selected)
                base = (
                    f"{progress}"
                    f"  ·  {selected} completed track"
                    f"{'s' if selected != 1 else ''} ready to write"
                    "  ·  Enter/W write completed"
                    "  ·  Esc stop after current"
                )
            self.query_one("#status", Static).update(f"{message}  ·  {base}" if message else base)
            return

        if self.phase == "choose":
            selected = sum(index in self.analysis_selected for index in self.visible_indices)
            review_hint = (
                f"  ·  V review {len(self.review_indices)} ready" if self.review_indices else ""
            )
            base = (
                f"{selected} selected in this view"
                f"  ·  Filter: {FILTER_LABELS[self.library_filter]}"
                f"{review_hint}"
                "  ·  Space toggle  ·  Enter/R analyze selected"
            )
        else:
            selected = len(self.write_selected)
            genre_edits = sum(
                (
                    self.entries[index].plan is not None
                    and self.entries[index].plan.standard_genre_change is not None
                )
                for index in self.write_selected
            )
            base = (
                f"{selected} will be written"
                f"  ·  {genre_edits} standard genre edits"
                "  ·  Space toggle  ·  Enter/W review write"
            )
        self.query_one("#status", Static).update(f"{message}  ·  {base}" if message else base)

    def action_toggle_track(self) -> None:
        if self.busy:
            return
        if self.phase == "choose" and self.analysis_running:
            return
        index = self._current_index()
        if index is None:
            return
        if self.phase == "choose":
            entry = self.entries[index]
            if not entry.can_analyze:
                self.notify(
                    "This track's metadata could not be read safely.",
                    severity="warning",
                )
                return
            selection = self.analysis_selected
        else:
            entry = self.entries[index]
            if entry.plan is None or not entry.plan.readable_changes:
                return
            selection = self.write_selected

        if index in selection:
            selection.remove(index)
        else:
            selection.add(index)
        self._refresh_row(index)

    def action_toggle_all(self) -> None:
        if self.busy:
            return
        if self.phase == "choose" and self.analysis_running:
            return
        if self.phase == "choose":
            eligible = {index for index in self.visible_indices if self.entries[index].can_analyze}
            selection = self.analysis_selected
        else:
            eligible = {
                index
                for index in self.visible_indices
                if (
                    self.entries[index].plan is not None
                    and bool(self.entries[index].plan.readable_changes)
                )
            }
            selection = self.write_selected

        if eligible and eligible.issubset(selection):
            selection.difference_update(eligible)
        else:
            selection.update(eligible)
        self._rebuild_table()

    def action_toggle_details(self) -> None:
        visible = not self.has_class("details-open")
        self.set_class(visible, "details-open")
        index = self._current_index()
        if visible and index is not None:
            self._update_inspector(index)
        self.query_one("#tracks", DataTable).focus()
        self.call_after_refresh(self._sync_table_columns)

    def action_cycle_filter(self) -> None:
        if self.busy:
            return
        if self.phase != "choose":
            self.notify("Filters apply to the library view. Press B first.")
            return
        position = FILTER_ORDER.index(self.library_filter)
        self.library_filter = FILTER_ORDER[(position + 1) % len(FILTER_ORDER)]
        self._rebuild_table()

    def action_analyze(self) -> None:
        if self.busy or self.analysis_running:
            return
        if self.phase != "choose":
            self.notify("Press B to choose another analysis batch.")
            return
        indices = tuple(
            index
            for index in self.visible_indices
            if (index in self.analysis_selected and self.entries[index].can_analyze)
        )
        if not indices:
            self.notify(
                "Select at least one visible, readable track first.",
                severity="warning",
            )
            return
        self._analysis_cancel_requested.clear()
        self._pending_analysis_indices = indices
        self._analysis_completed_count = 0
        self._analysis_success_count = 0
        self._analysis_failure_count = 0
        self._analysis_current_path = self.entries[indices[0]].path
        self._analysis_navigation_changed = False
        self.sub_title = "Analyzing in background"
        self.refresh_bindings()
        self._show_analysis_activity()
        self._update_status()
        self._analyze_selected(indices)

    def action_review(self) -> None:
        if self.busy or self.phase != "choose":
            return
        if not self.review_indices:
            self.notify("There are no analyzed tracks ready to review.")
            return
        if self.analysis_running:
            self._analysis_navigation_changed = True
        self._show_review()

    def action_cancel_analysis(self) -> None:
        if not self.analysis_running:
            return
        if self._analysis_cancel_requested.is_set():
            return
        self._analysis_cancel_requested.set()
        self.refresh_bindings()
        self._show_cancel_requested()
        self._update_status()

    @work(thread=True, exclusive=True, group="analysis", exit_on_error=False)
    def _analyze_selected(self, indices: tuple[int, ...]) -> None:
        total = len(indices)
        completed = 0
        cancelled = False
        for index in indices:
            if self._analysis_cancel_requested.is_set():
                cancelled = True
                break

            path = self.entries[index].path
            try:
                batch = self.analysis_loader(
                    (path,),
                    lambda _completed, _total, _path: None,
                    self._analysis_cancel_requested.is_set,
                )
            except Exception as error:
                self.call_from_thread(
                    self._analysis_failed,
                    f"{type(error).__name__}: {error}",
                )
                return

            has_result = bool(batch.planned or batch.failures)
            if not has_result and batch.cancelled:
                cancelled = True
                break

            completed += 1
            self.call_from_thread(
                self._analysis_item_complete,
                index,
                batch,
                completed,
                total,
            )
            if batch.cancelled:
                cancelled = True
                break

        self.call_from_thread(
            self._analysis_finished,
            completed,
            total,
            cancelled and completed < total,
        )

    def _show_analysis_activity(self) -> None:
        total = len(self._pending_analysis_indices)
        path = self._analysis_current_path
        activity = self.query_one("#analysis-activity")
        activity.display = True
        self.query_one("#analysis-activity-title", Static).update(
            f"Analyzing in background  ·  track 1 of {total}  ·  0 complete"
        )
        if path is not None:
            self.query_one("#analysis-activity-file", Static).update(
                f"Current file: {path.name}  ·  {path.parent}"
            )
        self.query_one("#analysis-progress", ProgressBar).update(
            total=total,
            progress=0,
        )

    def _show_cancel_requested(self) -> None:
        total = len(self._pending_analysis_indices)
        path = self._analysis_current_path
        self.query_one("#analysis-activity-title", Static).update(
            f"Cancel requested  ·  {self._analysis_completed_count} of {total} complete"
        )
        if path is not None:
            self.query_one("#analysis-activity-file", Static).update(
                f"Finishing {path.name}  ·  {path.parent}"
            )

    def _advance_analysis_activity(
        self,
        completed: int,
        total: int,
        path: Path,
    ) -> None:
        self._analysis_completed_count = completed
        progress = self.query_one("#analysis-progress", ProgressBar)
        progress.update(total=total, progress=completed)

        if self._analysis_cancel_requested.is_set():
            self._analysis_current_path = path
            self.query_one("#analysis-activity-title", Static).update(
                f"Stopping analysis  ·  {completed} of {total} complete"
            )
            self.query_one("#analysis-activity-file", Static).update(
                f"Finished {path.name}  ·  completed results will be kept"
            )
            return

        if completed < len(self._pending_analysis_indices):
            next_index = self._pending_analysis_indices[completed]
            next_path = self.entries[next_index].path
            self._analysis_current_path = next_path
            self.query_one("#analysis-activity-title", Static).update(
                f"Analyzing in background  ·  track {completed + 1} of {total}"
                f"  ·  {completed} complete"
            )
            self.query_one("#analysis-activity-file", Static).update(
                f"Current file: {next_path.name}  ·  {next_path.parent}"
            )
            return

        self._analysis_current_path = None
        self.query_one("#analysis-activity-title", Static).update(
            f"Finalizing results  ·  {completed} of {total} complete"
        )
        self.query_one("#analysis-activity-file", Static).update(f"Finished {path.name}")

    def _hide_analysis_activity(self) -> None:
        self.query_one("#analysis-activity").display = False
        self._analysis_completed_count = 0
        self._analysis_current_path = None

    def _analysis_failed(self, message: str) -> None:
        completed = self._analysis_completed_count
        total = len(self._pending_analysis_indices)
        remaining = total - completed
        self._pending_analysis_indices = ()
        self._analysis_cancel_requested.clear()
        self._hide_analysis_activity()
        self.sub_title = (
            "Review analyzed tracks" if self.phase == "review" else "Choose tracks to analyze"
        )
        self.refresh_bindings()
        if completed:
            self._update_status(
                f"Analysis stopped after {completed} of {total} tracks"
                f"  ·  {remaining} remain selected"
            )
        else:
            self._update_status("Analysis did not start; nothing was written")
        self.push_screen(ErrorScreen("Could not analyze selection", message))

    def _analysis_item_complete(
        self,
        index: int,
        batch: AnalysisBatch,
        completed: int,
        total: int,
    ) -> None:
        entry = self.entries[index]
        plan = next(
            (item for item in batch.planned if item.path == entry.path),
            None,
        )
        failure = next(
            (item for item in batch.failures if item.path == entry.path),
            None,
        )
        if plan is not None:
            plan = stage_default_file_genre(plan)
            entry.plan = plan
            entry.plan_cached = False
            entry.analysis_error = None
            self.review_indices.add(index)
            if plan.readable_changes:
                self.write_selected.add(index)
            self._persist(index)
            self._analysis_success_count += 1
        else:
            if failure is None:
                failure = AnalysisFailure(
                    path=entry.path,
                    error_type="RuntimeError",
                    message="Analyzer returned no result for this track",
                )
            entry.plan = None
            entry.analysis_error = failure
            self.review_indices.add(index)
            self.write_selected.discard(index)
            self._analysis_failure_count += 1

        self.analysis_selected.discard(index)
        self._advance_analysis_activity(completed, total, entry.path)
        preferred_index = self._current_index()
        self._rebuild_table(preferred_index=preferred_index)
        self.refresh_bindings()

    def _analysis_finished(
        self,
        completed: int,
        total: int,
        cancelled: bool,
    ) -> None:
        remaining = total - completed
        self._pending_analysis_indices = ()
        self._analysis_cancel_requested.clear()
        self._hide_analysis_activity()
        self.refresh_bindings()

        if self.phase == "choose" and self.review_indices and not self._analysis_navigation_changed:
            self._show_review()
        else:
            self._rebuild_table()

        if cancelled and remaining > 0:
            if completed:
                self._show_review()
                self._update_status(
                    f"Cancelled after {completed} of {total} tracks; {remaining} remain selected"
                )
            else:
                self._show_library()
                self._update_status("Analysis cancelled; visible tracks remain selected")
            return

        summary = (
            f"Analysis complete  ·  {self._analysis_success_count} analyzed"
            f"  ·  {self._analysis_failure_count} failed"
        )
        if not self.busy:
            self._update_status(summary)
        self.notify(summary, title="Background analysis complete", timeout=6)

    def action_library(self) -> None:
        if self.busy:
            return
        if self.phase == "choose":
            return
        if self.analysis_running:
            self._analysis_navigation_changed = True
        self.analysis_selected = {
            index for index, entry in enumerate(self.entries) if entry.needs_analysis
        }
        self._show_library()

    def action_edit_genre(self) -> None:
        if self.busy:
            return
        index = self._current_review_index()
        if index is None:
            return
        item = self.entries[index].plan
        assert item is not None
        self.push_screen(
            GenreEditScreen(item),
            lambda result: self._genre_edited(index, result),
        )

    def _current_review_index(self) -> int | None:
        if self.phase != "review":
            self.notify("Analyze a selection before editing genre suggestions.")
            return None
        index = self._current_index()
        if index is None or self.entries[index].plan is None:
            self.notify("This track has no analysis result.", severity="warning")
            return None
        return index

    def _genre_edited(self, index: int, result: str | None) -> None:
        if result is None:
            return
        item = self.entries[index].plan
        if item is None:
            return
        genres = tuple(value.strip() for value in result.split(",") if value.strip())
        self.entries[index].plan = stage_file_genre(item, genres)
        if self.entries[index].plan.readable_changes:
            self.write_selected.add(index)
        self._persist(index)
        self._refresh_row(index)
        staged = ", ".join(genres) or "None"
        self._update_status(f"Staged standard file genre: {staged}")

    def action_save(self) -> None:
        if self.busy:
            return
        if self.phase != "review":
            self.notify("Analyze a selection before saving a write plan.")
            return
        planned = self._selected_items()
        if not planned:
            self.notify("Include at least one changed track first.", severity="warning")
            return
        failures = self._review_failures()
        try:
            path = save_plan(planned, failures=failures)
        except OSError as error:
            self.push_screen(ErrorScreen("Could not save plan", str(error)))
            return
        self.notify(f"Saved {path.name}", title="Plan saved", timeout=5)
        self._update_status(f"Saved {path.name}")

    def action_write(self) -> None:
        if self.busy:
            return
        if self.phase != "review":
            self.notify("Analyze a selection before writing metadata.")
            return
        failures = self._review_failures()
        if failures:
            self.notify(
                "Writing is disabled because one or more reviewed tracks failed analysis.",
                severity="error",
            )
            return
        planned = self._selected_items()
        if not planned:
            self.notify("Include at least one changed track first.", severity="warning")
            return
        self.busy = True
        self._pending_write = planned
        self._update_status("Checking every source and staged change…")
        self._preflight_for_confirmation(planned)

    @work(thread=True, exclusive=True, group="write", exit_on_error=False)
    def _preflight_for_confirmation(
        self,
        planned: tuple[PlannedWrite, ...],
    ) -> None:
        try:
            prepared = preflight_plan(planned)
        except Exception as error:
            self.call_from_thread(
                self._write_failed,
                "Preflight failed",
                str(error),
            )
            return
        self.call_from_thread(self._confirm_preflight, prepared)

    def _confirm_preflight(self, _prepared: Sequence[object]) -> None:
        self.busy = False
        track_count = len(self._pending_write)
        standard_count = sum(item.standard_genre_change is not None for item in self._pending_write)
        evidence_count = sum(len(item.evidence) for item in self._pending_write)
        self.push_screen(
            ConfirmWriteScreen(
                track_count=track_count,
                standard_genre_count=standard_count,
                evidence_count=evidence_count,
            ),
            self._write_confirmation,
        )

    def _write_confirmation(self, confirmed: bool | None) -> None:
        if not confirmed:
            self._pending_write = ()
            self._update_status("Write cancelled; nothing changed")
            return
        self.busy = True
        self._update_status("Running final preflight…")
        self._apply_pending_write()

    @work(thread=True, exclusive=True, group="write", exit_on_error=False)
    def _apply_pending_write(self) -> None:
        try:
            prepared = preflight_plan(self._pending_write)
            completed = apply_prepared(
                prepared,
                on_progress=self._write_progress_from_worker,
            )
        except PartialWriteError as error:
            written = self._pending_write[: error.completed]
            cleanup_error = self._discard_written(written)
            message = str(error)
            if cleanup_error is not None:
                message += f"\n\nLocal workbench cleanup also failed: {cleanup_error}"
            self.call_from_thread(
                self._partial_write_failed,
                "Write stopped",
                message,
                written,
            )
            return
        except Exception as error:
            self.call_from_thread(
                self._write_failed,
                "Write failed",
                str(error),
            )
            return
        cleanup_error = self._discard_written(self._pending_write)
        self.call_from_thread(self._write_complete, completed, cleanup_error)

    def _write_complete(
        self,
        completed: int,
        cleanup_error: str | None,
    ) -> None:
        written = self._pending_write
        self._accept_written(written)
        self._written_count += completed
        self.busy = False
        self._pending_write = ()
        message = f"Done. {completed} file{'s' if completed != 1 else ''} written and verified."
        if cleanup_error is not None:
            message += (
                " The audio write succeeded, but SetTag could not clear its "
                f"local workbench entry: {cleanup_error}"
            )
            self.notify(
                "The audio write succeeded, but local workbench cleanup failed.",
                severity="warning",
                timeout=8,
            )
        if self.review_indices:
            self._show_review()
        else:
            self._show_library()
        self._update_status(message)
        self.notify(message, title="Write complete", timeout=6)

    def _partial_write_failed(
        self,
        title: str,
        message: str,
        written: Sequence[PlannedWrite],
    ) -> None:
        self._accept_written(written)
        self._written_count += len(written)
        if self.review_indices:
            self._show_review()
        else:
            self._show_library()
        self._write_failed(title, message)

    def _accept_written(self, items: Sequence[PlannedWrite]) -> None:
        by_path = {entry.path: index for index, entry in enumerate(self.entries)}
        for item in items:
            index = by_path[item.path]
            entry = self.entries[index]
            if entry.metadata is not None:
                analyzed_at_values = item.desired["SETTAG_ANALYZED_AT"]
                analyzed_at = analyzed_at_values[0] if analyzed_at_values else None
                standard_genre = (
                    item.target_file_genre
                    if item.target_file_genre is not None
                    else item.file_genre
                )
                entry.metadata = replace(
                    entry.metadata,
                    genre_state=replace(
                        entry.metadata.genre_state,
                        standard=standard_genre,
                        settag=tuple(prediction.label for prediction in item.selected),
                    ),
                    owned=item.desired,
                    stored_predictions=item.evidence,
                    status="current",
                    analyzed_at=analyzed_at,
                    cached_plan=None,
                    cache_status=None,
                    cache_reason=None,
                )
            entry.plan = None
            entry.plan_cached = False
            entry.analysis_error = None
            self.analysis_selected.discard(index)
            self.write_selected.discard(index)
            self.review_indices.discard(index)

    def _discard_written(
        self,
        items: Sequence[PlannedWrite],
    ) -> str | None:
        if self.discard_plans is None or not items:
            return None
        try:
            self.discard_plans([item.path for item in items])
        except Exception as error:
            return f"{type(error).__name__}: {error}"
        return None

    def _write_progress_from_worker(
        self,
        completed: int,
        total: int,
        path: Path,
    ) -> None:
        self.call_from_thread(
            self._update_status,
            f"Writing {completed} of {total}: {path.name}",
        )

    def _write_failed(self, title: str, message: str) -> None:
        self.busy = False
        self._pending_write = ()
        self._update_status("Nothing else will be written")
        self.push_screen(ErrorScreen(title, message))

    def _selected_items(self) -> tuple[PlannedWrite, ...]:
        items: list[PlannedWrite] = []
        for index in sorted(self.write_selected):
            item = self.entries[index].plan
            if item is not None and item.readable_changes:
                items.append(item)
        return tuple(items)

    def _review_failures(self) -> tuple[AnalysisFailure, ...]:
        return tuple(
            failure
            for index in sorted(self.review_indices)
            if (failure := self.entries[index].analysis_error) is not None
        )

    def _persist(self, index: int) -> None:
        if self.persist_plan is None:
            return
        item = self.entries[index].plan
        if item is None:
            return
        try:
            self.persist_plan(item)
        except Exception as error:
            self.notify(
                "The result is still available in this session, but could not "
                f"be saved to the local workbench: {type(error).__name__}: {error}",
                severity="warning",
                timeout=8,
            )

    def action_quit(self) -> None:
        if self.busy:
            self.notify("A safety check or write is in progress.", severity="warning")
            return
        if self.analysis_running:
            self.notify(
                "Analysis is still running. Press Esc to stop after the current "
                "track before quitting.",
                severity="warning",
            )
            return
        if self._written_count:
            message = (
                f"Done. {self._written_count} "
                f"file{'s' if self._written_count != 1 else ''} "
                "written and verified."
            )
        else:
            message = "Nothing was written."
        self.exit(TuiOutcome(0, message))
