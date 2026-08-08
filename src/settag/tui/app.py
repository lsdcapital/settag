"""The SetTag application: phases, selection, and the background work."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from threading import Event

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    ProgressBar,
    Static,
)

from settag.journal import (
    BatchRecorder,
    JournalBatch,
    JournalError,
    WriteJournal,
    WriteRecord,
)
from settag.plans import (
    PlannedWrite,
    stage_default_file_genre,
    stage_file_genre,
    suggested_file_genre,
)
from settag.policy import Prediction
from settag.tags import OwnedValues, read_task_provenance, task_evidence_from_owned
from settag.tasks import AnalysisTask, ordered_tasks
from settag.tui.entries import (
    STATUS_LABELS,
    TASK_LABELS,
    AnalysisLoader,
    AppPhase,
    LibraryFilter,
    MetadataLoader,
    PlanDiscarder,
    PlanPersister,
    TrackEntry,
    TuiOutcome,
    latest_analyzed_at,
    suggested_label,
)
from settag.tui.screens import (
    ConfirmUndoScreen,
    ConfirmWriteScreen,
    ErrorScreen,
    GenreEditScreen,
    UndoScreen,
)
from settag.tui.style import APP_CSS
from settag.tui.table import (
    ResponsiveTrackTable,
    RowContext,
    TrackTableColumn,
    _track_table_layout,
    visible_row_cells,
)
from settag.workflow import (
    AnalysisBatch,
    AnalysisFailure,
    MetadataBatch,
    MetadataStatus,
    MetadataTrack,
    PartialWriteError,
    PreparedWrite,
    UndoPreflight,
    apply_prepared,
    apply_undo,
    preflight_plan,
    preflight_undo,
    save_plan,
    summarize_writes,
)

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


def _display_path(path: Path) -> str:
    """Keep paths recognizable without repeating the full home directory."""
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~" if relative == Path(".") else str(Path("~") / relative)


CHOOSE_ACTIONS = frozenset(
    {
        "toggle_track",
        "toggle_all",
        "toggle_details",
        "cycle_filter",
        "review",
        "analyze",
        "hygiene",
        "undo",
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
        "hygiene",
        "undo",
        "quit",
    }
)


def _restored_status(owned: OwnedValues) -> MetadataStatus:
    """Describe a track after its SetTag metadata was rolled back.

    A restored bundle cannot be shown as up to date without re-inspecting it
    against the current model and config, so anything still carrying SetTag
    metadata is reported as needing reanalysis rather than over-claimed.
    """
    if all(values is None for values in owned.values()):
        return "not_analyzed"
    return "stale"


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
        Binding("h", "hygiene", "Hygiene"),
        Binding("u", "undo", "Undo"),
        Binding("q", "quit", "Quit"),
    ]

    CSS = APP_CSS

    def __init__(
        self,
        *,
        source: Path,
        analysis_loader: AnalysisLoader,
        metadata_loader: MetadataLoader | None = None,
        initial_metadata: MetadataBatch | None = None,
        persist_plan: PlanPersister | None = None,
        discard_plans: PlanDiscarder | None = None,
        journal: WriteJournal | None = None,
        review_top: int = 5,
        score_cutoff: float = 0.10,
        analysis_tasks: Sequence[AnalysisTask] = ("genre",),
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
        self.journal = journal
        self.review_top = review_top
        self.score_cutoff = score_cutoff
        self.analysis_tasks = ordered_tasks(analysis_tasks)
        if not self.analysis_tasks:
            raise ValueError("SetTagApp requires at least one analysis task")
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
        self._pending_undo: tuple[WriteRecord, ...] = ()
        self._pending_undo_batch: str | None = None
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
                    yield Static(
                        "Track details",
                        markup=False,
                        classes="section-title",
                    )
                    with VerticalScroll(id="inspector-scroll"):
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

    @on(ResponsiveTrackTable.Resized)
    def track_table_resized(self) -> None:
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
            index for index in self.review_indices if self.entries[index].has_changes
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
            selected=tuple(self._row_context.select_for_review(track.cached_plan.evidence)),
        )

    @property
    def _row_context(self) -> RowContext:
        return RowContext(
            tasks=self.analysis_tasks,
            review_top=self.review_top,
            score_cutoff=self.score_cutoff,
        )

    def _visible_cells(self, index: int) -> tuple[str, ...]:
        """Supply one row's app state to the renderer in tui.table."""
        selected = (
            index in self.analysis_selected
            if self.phase == "choose"
            else index in self.write_selected
        )
        return visible_row_cells(
            self.entries[index],
            selected=selected,
            context=self._row_context,
            layout=self._table_layout,
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
            return [index for index in indices if self.entries[index].is_missing_standard_genre]
        return [index for index in indices if self.entries[index].is_current_unplanned]

    def _rebuild_table(
        self,
        preferred_index: int | None = None,
        *,
        refresh_surrounding: bool = True,
        preserve_view: bool = False,
    ) -> None:
        """Redraw every row.

        ``preserve_view`` is for redraws the user did not ask for, such as a
        background analysis finishing a track. ``clear`` resets the scroll
        offset and ``move_cursor`` then scrolls the cursor back into view, so an
        unrequested rebuild otherwise yanks a scrolled library back and steals
        focus from wherever it was.
        """
        if preferred_index is None:
            preferred_index = self._current_index()
        self.visible_indices = self._filtered_indices()
        table = self.query_one("#tracks", DataTable)
        scroll_y = table.scroll_y
        table.clear()
        for index in self.visible_indices:
            table.add_row(*self._visible_cells(index), key=str(index))

        if refresh_surrounding:
            self._update_context()
            self._update_status()
        if not self.visible_indices:
            if refresh_surrounding:
                self.query_one("#inspector", Static).update("No tracks match this view.")
            return

        if preferred_index is None:
            cursor_row = 0
        else:
            try:
                cursor_row = self.visible_indices.index(preferred_index)
            except ValueError:
                cursor_row = 0
        if not preserve_view:
            table.focus()
        table.move_cursor(row=cursor_row, scroll=not preserve_view)
        if preserve_view:
            table.scroll_to(y=scroll_y, animate=False)
        if refresh_surrounding:
            self._update_inspector(self.visible_indices[cursor_row])

    def _update_context(self) -> None:
        task_text = ", ".join(TASK_LABELS[task] for task in self.analysis_tasks)
        if self.phase == "choose":
            # ui-count: rows in the library view, not a property of any batch
            needs = sum(entry.needs_analysis for entry in self.entries)
            # ui-count: tracks whose tags could not be read into this view
            errors = sum(entry.metadata_error is not None for entry in self.entries)
            ready = len(self.review_indices)
            ready_text = f"  ·  {ready} ready to review" if ready else ""
            text = (
                f"{self.source}  ·  {len(self.entries)} tracks"
                f"  ·  {needs} need analysis"
                f"{ready_text}"
                f"  ·  {errors} metadata error{'s' if errors != 1 else ''}"
                f"  ·  Tasks: {task_text}"
                f"  ·  Filter: {FILTER_LABELS[self.library_filter]}"
            )
        else:
            # ui-count: analysis failures in this session's review set
            failures = sum(
                self.entries[index].analysis_error is not None for index in self.review_indices
            )
            text = (
                f"{self.source}  ·  {len(self.review_indices)} reviewed"
                f"  ·  {failures} analysis error{'s' if failures != 1 else ''}"
                f"  ·  Tasks: {task_text}"
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
        try:
            inspector = self.query_one("#inspector", Static)
            inspector_scroll = self.query_one("#inspector-scroll", VerticalScroll)
        except NoMatches:
            # A queued row-highlight can arrive while the app screen is unmounting.
            return
        inspector.update("\n".join(lines))
        inspector_scroll.scroll_home(animate=False)

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
        evidence_owned = (
            entry.plan.desired
            if entry.plan is not None
            else cached_plan.desired
            if cached_plan is not None and metadata.cache_status == "stale"
            else metadata.owned
        )
        candidate_title = (
            "Local candidates (stale)"
            if metadata.cache_status == "stale"
            else ("Local candidates (ready)" if entry.plan is not None else "Stored candidates")
        )
        lines.extend(
            [
                "Current file metadata",
                f"  Standard genre: {genre}",
                f"  SetTag status: {cache_status or STATUS_LABELS[metadata.status]}",
                f"  Last analyzed: {self._full_analyzed_at(entry)}",
                "",
                f"{candidate_title} · {self._candidate_policy()}",
            ]
        )
        lines.extend(
            self._task_candidate_sections(
                evidence_owned,
                fallback_genre=metadata.stored_predictions,
            )
        )

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
            return latest_analyzed_at(entry.plan.desired, self.analysis_tasks) or "Never"
        if entry.metadata is not None and entry.metadata.cached_plan is not None:
            return (
                latest_analyzed_at(entry.metadata.cached_plan.desired, self.analysis_tasks)
                or "Never"
            )
        if entry.metadata is not None:
            return entry.metadata.analyzed_at or "Never"
        return "Never"

    def _review_inspector(self, entry: TrackEntry, index: int) -> list[str]:
        lines = [entry.path.name, _display_path(entry.path.parent), ""]
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
        model_child = suggested_label(item.selected)
        rollup_line = (
            f"  Suggestion: {model_child} → {suggestion}"
            if suggestion and model_child and suggestion != model_child
            else None
        )
        lines.extend(
            [
                (
                    "Write plan · Included"
                    if index in self.write_selected
                    else "Write plan · Excluded"
                ),
                f"  {item.evidence_write_label}",
                f"  Standard genre: {genre_line.strip()}",
                *([rollup_line] if rollup_line else []),
                "",
                f"Candidates · {self._candidate_policy()}",
            ]
        )
        lines.extend(self._task_candidate_sections(item.desired))
        return lines

    def _task_candidate_sections(
        self,
        owned: OwnedValues,
        *,
        fallback_genre: Sequence[Prediction] = (),
    ) -> list[str]:
        evidence_by_task = task_evidence_from_owned(owned)
        provenance = read_task_provenance(owned)
        lines: list[str] = []
        for task in self.analysis_tasks:
            evidence = evidence_by_task.get(task, ())
            if not evidence and task == "genre":
                evidence = fallback_genre
            if evidence:
                count = len(evidence)
                noun = "score" if count == 1 else "scores"
                lines.append(f"{TASK_LABELS[task]} · {count} {noun}")
                lines.append(
                    self._candidate_line(
                        self._row_context.select_for_review(evidence),
                    )
                )
            elif task in provenance:
                lines.append(f"{TASK_LABELS[task]} · No ranked evidence")
            else:
                lines.append(f"{TASK_LABELS[task]} · Not analyzed")
        return lines

    def _candidate_policy(self) -> str:
        cutoff = f"{self.score_cutoff:.3f}".removesuffix("0")
        return f"cutoff ≥ {cutoff} · top {self.review_top}"

    def _candidate_line(
        self,
        selected: Sequence[Prediction],
    ) -> str:
        if selected:
            return "  " + " · ".join(
                f"{suggested_label((prediction,)) or prediction.label} {prediction.score:.3f}"
                for prediction in selected
            )
        return "  No candidate met the cutoff"

    def _refresh_row(self, index: int, *, update_inspector: bool = True) -> None:
        if index not in self.visible_indices:
            return
        row = self.visible_indices.index(index)
        table = self.query_one("#tracks", DataTable)
        for column, value in enumerate(self._visible_cells(index)):
            table.update_cell_at(
                Coordinate(row, column),
                value,
            )
        if update_inspector:
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
            # ui-count: how much of the current filtered view is selected
            selected = sum(index in self.analysis_selected for index in self.visible_indices)
            review_hint = (
                f"  ·  V review {len(self.review_indices)} ready" if self.review_indices else ""
            )
            base = (
                f"{selected} selected in this view"
                f"  ·  Filter: {FILTER_LABELS[self.library_filter]}"
                f"{review_hint}"
                "  ·  Space toggle  ·  Enter/R analyze selected  ·  H hygiene"
            )
        else:
            selected = len(self.write_selected)
            # ui-count: staged edits among the currently checked rows, before any preflight
            genre_edits = sum(
                self.entries[index].has_standard_genre_change for index in self.write_selected
            )
            base = (
                f"{selected} will be written"
                f"  ·  {genre_edits} standard genre edits"
                "  ·  Space toggle  ·  Enter/W review write  ·  H hygiene"
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
            eligible = {index for index in self.visible_indices if self.entries[index].has_changes}
            selection = self.write_selected

        if eligible and eligible.issubset(selection):
            selection.difference_update(eligible)
        else:
            selection.update(eligible)
        self._rebuild_table()

    def action_toggle_details(self) -> None:
        visible = not self.has_class("details-open")
        self.set_class(visible, "details-open")
        table = self.query_one("#tracks", DataTable)
        inspector_scroll = self.query_one("#inspector-scroll", VerticalScroll)
        index = self._current_index()
        if visible:
            if index is not None:
                self._update_inspector(index)
        else:
            table.focus()
        self.call_after_refresh(self._sync_table_columns)
        if visible:
            self.call_after_refresh(inspector_scroll.focus)

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
        self._refresh_after_analysis(index)
        self.refresh_bindings()

    def _refresh_after_analysis(self, index: int) -> None:
        """Show one finished track without moving the view under the user.

        Tracks complete while the user is free to scroll, filter, and read. Only
        the finished row's own cells change unless the current view admits or
        drops a track, so redrawing the whole table is usually unnecessary — and
        a redraw costs the scroll position. The inspector follows the cursor, not
        the track that happened to finish.
        """
        if self._filtered_indices() == self.visible_indices:
            self._refresh_row(index, update_inspector=index == self._current_index())
            self._update_context()
            return
        self._rebuild_table(preferred_index=self._current_index(), preserve_view=True)

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
        updated = stage_file_genre(item, genres)
        self.entries[index].plan = updated
        if updated.readable_changes:
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

    def _confirm_preflight(self, prepared: Sequence[PreparedWrite]) -> None:
        self.busy = False
        self.push_screen(
            ConfirmWriteScreen(summarize_writes(prepared)),
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
        recorder = BatchRecorder(self.journal) if self.journal is not None else None
        try:
            prepared = preflight_plan(self._pending_write)
            completed = apply_prepared(
                prepared,
                on_progress=self._write_progress_from_worker,
                on_write=recorder,
            )
        except PartialWriteError as error:
            written = self._pending_write[: error.completed]
            cleanup_error = self._discard_written(written)
            message = str(error)
            if cleanup_error is not None:
                message += f"\n\nLocal workbench cleanup also failed: {cleanup_error}"
            journal_error = recorder.error if recorder is not None else None
            if journal_error is not None:
                message += f"\n\n{journal_error}"
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
        self.call_from_thread(
            self._write_complete,
            completed,
            cleanup_error,
            recorder.error if recorder is not None else None,
        )

    def _write_complete(
        self,
        completed: int,
        cleanup_error: str | None,
        journal_error: str | None = None,
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
        if journal_error is not None:
            message += f" {journal_error}"
            self.notify(journal_error, severity="warning", timeout=8)
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

    def action_undo(self) -> None:
        if self.busy:
            return
        if self.analysis_running:
            self.notify(
                "Analysis is still running. Press Esc to stop it before undoing a write.",
                severity="warning",
            )
            return
        if self.journal is None:
            self.notify(
                "Undo is unavailable because no write journal is configured.",
                severity="warning",
            )
            return
        self.busy = True
        self._update_status("Reading the write journal…")
        self._load_undo_batches()

    @work(thread=True, exclusive=True, group="undo", exit_on_error=False)
    def _load_undo_batches(self) -> None:
        assert self.journal is not None
        try:
            batches = self.journal.recent(limit=20)
        except JournalError as error:
            self.call_from_thread(
                self._undo_failed,
                "Could not read the write journal",
                str(error),
            )
            return
        self.call_from_thread(self._choose_undo_batch, batches)

    def _choose_undo_batch(self, batches: Sequence[JournalBatch]) -> None:
        self.busy = False
        if not batches:
            self._update_status("There is nothing to undo yet")
            self.notify("No SetTag writes have been journaled yet.")
            return
        self.push_screen(UndoScreen(batches), self._undo_batch_chosen)

    def _undo_batch_chosen(self, batch_id: str | None) -> None:
        if not batch_id:
            self._update_status("Undo cancelled; nothing changed")
            return
        self.busy = True
        self._update_status("Checking every file in that write…")
        self._preflight_undo_batch(batch_id)

    @work(thread=True, exclusive=True, group="undo", exit_on_error=False)
    def _preflight_undo_batch(self, batch_id: str) -> None:
        assert self.journal is not None
        try:
            batch = self.journal.batch(batch_id)
        except JournalError as error:
            self.call_from_thread(
                self._undo_failed,
                "Could not read the write journal",
                str(error),
            )
            return
        if batch is None:
            self.call_from_thread(
                self._undo_failed,
                "Write batch is missing",
                f"The write journal no longer holds a batch named {batch_id}.",
            )
            return
        preflight = preflight_undo(batch.entries)
        self.call_from_thread(self._confirm_undo, batch, preflight)

    def _confirm_undo(self, batch: JournalBatch, preflight: UndoPreflight) -> None:
        self.busy = False
        self._pending_undo = preflight.restorable
        self._pending_undo_batch = batch.batch_id
        if not preflight.restorable:
            blockers = "\n".join(
                f"{blocked.entry.path.name}: {blocked.reason}" for blocked in preflight.blocked
            )
            self._pending_undo = ()
            self._pending_undo_batch = None
            self._update_status("Nothing could be restored from that write")
            self.push_screen(
                ErrorScreen(
                    "Nothing can be restored",
                    f"No file from that write can be safely restored.\n\n{blockers}",
                )
            )
            return
        self.push_screen(
            ConfirmUndoScreen(batch=batch, preflight=preflight),
            self._undo_confirmation,
        )

    def _undo_confirmation(self, confirmed: bool | None) -> None:
        if not confirmed:
            self._pending_undo = ()
            self._pending_undo_batch = None
            self._update_status("Undo cancelled; nothing changed")
            return
        self.busy = True
        self._update_status("Restoring the previous metadata…")
        self._apply_pending_undo()

    @work(thread=True, exclusive=True, group="undo", exit_on_error=False)
    def _apply_pending_undo(self) -> None:
        entries = self._pending_undo
        try:
            restored = apply_undo(entries, on_progress=self._undo_progress_from_worker)
        except PartialWriteError as error:
            self.call_from_thread(
                self._undo_partly_failed,
                str(error),
                entries[: error.completed],
            )
            return
        except Exception as error:
            self.call_from_thread(self._undo_failed, "Undo failed", str(error))
            return
        if self.journal is not None and self._pending_undo_batch is not None:
            try:
                self.journal.mark_reverted(self._pending_undo_batch)
            except JournalError as error:
                # The files are already restored, so this is not a failure of the undo. Say so
                # rather than swallowing it: the batch will still look undoable in the list,
                # and a DJ who is not told why would reasonably think the undo did not work.
                self.call_from_thread(
                    self.notify,
                    f"Files restored, but the journal could not be updated: {error}",
                    severity="warning",
                )
        self.call_from_thread(self._undo_complete, restored, entries)

    def _undo_progress_from_worker(self, completed: int, total: int, path: Path) -> None:
        self.call_from_thread(
            self._update_status,
            f"Restoring {completed} of {total}: {path.name}",
        )

    def _undo_complete(self, restored: int, entries: Sequence[WriteRecord]) -> None:
        self._accept_reverted(entries)
        self.busy = False
        self._pending_undo = ()
        self._pending_undo_batch = None
        self._show_library()
        message = f"Restored {restored} file{'s' if restored != 1 else ''} to their previous tags."
        self._update_status(message)
        self.notify(message, title="Undo complete", timeout=6)

    def _undo_partly_failed(self, message: str, restored: Sequence[WriteRecord]) -> None:
        self._accept_reverted(restored)
        self._show_library()
        self._undo_failed("Undo stopped", message)

    def _undo_failed(self, title: str, message: str) -> None:
        self.busy = False
        self._pending_undo = ()
        self._pending_undo_batch = None
        self._update_status("Nothing else will be restored")
        self.push_screen(ErrorScreen(title, message))

    def _accept_reverted(self, entries: Sequence[WriteRecord]) -> None:
        by_path = {entry.path: index for index, entry in enumerate(self.entries)}
        for record in entries:
            index = by_path.get(record.path)
            if index is None:
                continue
            entry = self.entries[index]
            standard_genre = (
                record.standard_before
                if record.standard_after is not None or entry.metadata is None
                else entry.metadata.genre_state.standard
            )
            self._refresh_entry_metadata(
                index,
                owned=dict(record.owned_before),
                standard_genre=standard_genre,
                status=_restored_status(record.owned_before),
            )
            entry.plan = None
            entry.plan_cached = False
            entry.analysis_error = None
            self.analysis_selected.discard(index)
            self.write_selected.discard(index)
            self.review_indices.discard(index)

    def _accept_written(self, items: Sequence[PlannedWrite]) -> None:
        by_path = {entry.path: index for index, entry in enumerate(self.entries)}
        for item in items:
            index = by_path[item.path]
            standard_genre = (
                item.target_file_genre if item.target_file_genre is not None else item.file_genre
            )
            self._refresh_entry_metadata(
                index,
                owned=item.desired,
                standard_genre=standard_genre,
                status="current",
            )
            entry = self.entries[index]
            entry.plan = None
            entry.plan_cached = False
            entry.analysis_error = None
            self.analysis_selected.discard(index)
            self.write_selected.discard(index)
            self.review_indices.discard(index)

    def _refresh_entry_metadata(
        self,
        index: int,
        *,
        owned: OwnedValues,
        standard_genre: tuple[str, ...],
        status: MetadataStatus,
    ) -> None:
        """Point one row at the metadata a file now holds.

        Shared by the write and undo paths so a row never describes a state the
        file is no longer in.
        """
        entry = self.entries[index]
        if entry.metadata is None:
            return
        stored_by_task = task_evidence_from_owned(owned)
        stored_genre = stored_by_task.get("genre", ())
        entry.metadata = replace(
            entry.metadata,
            genre_state=replace(
                entry.metadata.genre_state,
                standard=standard_genre,
                settag=tuple(prediction.label for prediction in stored_genre),
            ),
            owned=owned,
            stored_predictions=stored_genre,
            status=status,
            analyzed_at=latest_analyzed_at(owned, self.analysis_tasks),
            cached_plan=None,
            cache_status=None,
            cache_reason=None,
        )

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

    def action_hygiene(self) -> None:
        if self.busy:
            return
        if self.analysis_running:
            self.notify(
                "Stop analysis before switching to metadata hygiene.",
                severity="warning",
            )
            return
        self.exit(TuiOutcome(0, "Opening metadata hygiene.", next_action="hygiene"))

    async def action_quit(self) -> None:
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
