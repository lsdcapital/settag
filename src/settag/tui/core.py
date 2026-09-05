"""The core SetTagApp state, rendering, and selection actions.

The undo, write, and analysis flows are mixed in from their own modules;
this module holds only what they all share.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    ProgressBar,
    Static,
    Tree,
)

from settag.journal import (
    WriteJournal,
    WriteRecord,
)
from settag.plans import (
    PlannedWrite,
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
    GenreFilter,
    LibraryFilter,
    MetadataLoader,
    PlanDiscarder,
    PlanPersister,
    TrackEntry,
    TuiOutcome,
    latest_analyzed_at,
    suggested_label,
)
from settag.tui.review import NodeKey, ReviewTree
from settag.tui.screens import (
    GenreEditScreen,
)
from settag.tui.table import (
    GENRE_MATCH_STYLE,
    GENRE_REVIEW_STYLE,
    ResponsiveTrackTable,
    RowContext,
    TrackTableColumn,
    _track_table_layout,
    genre_check,
    visible_row_cells,
)
from settag.workflow import (
    MetadataBatch,
    MetadataStatus,
    MetadataTrack,
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
    "current": "Analysis current",
}


GENRE_FILTER_ORDER: tuple[GenreFilter, ...] = ("all", "needs_review", "missing_genre", "matches")
GENRE_FILTER_LABELS: dict[GenreFilter, str] = {
    "all": "All",
    "needs_review": "Needs review",
    "missing_genre": "Missing genre",
    "matches": "Matches",
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
        "cycle_genre_filter",
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


class SetTagAppCore(App[TuiOutcome]):
    """Metadata-first library browser and explicit analysis/write workflow.

    Holds the state, rendering, and selection actions shared by every flow.
    Textual class attributes (BINDINGS, CSS, TITLE) live on the concrete
    ``SetTagApp`` in ``app.py``, since Textual reads them from the app class
    actually instantiated.
    """

    if TYPE_CHECKING:
        # Implemented by settag.tui.analysis_flow.AnalysisFlow. Declared here,
        # type-only, so type checkers can resolve the call from _genre_edited below.
        def _persist(self, index: int) -> None: ...

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
        self.genre_filter: GenreFilter = "all"
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
        self._pending_undo_skipped = 0
        self._written_count = 0
        self._table_layout: tuple[tuple[TrackTableColumn, int], ...] = ()
        self._inspector_state: tuple[AppPhase, int, str] | None = None
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
            yield Static("", markup=False, id="library-filters")
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
                    yield Static(
                        "No tracks match these filters. Use F or G to change the view.",
                        markup=False,
                        id="library-empty",
                    )
                    yield ResponsiveTrackTable(
                        cursor_type="row",
                        zebra_stripes=True,
                        id="tracks",
                    )
                    yield ReviewTree()
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
        if self.phase != "choose":
            return
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
                preserve_view=True,
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
            # ui-count: the review selection restored from the local workbench
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

    def _visible_cells(self, index: int) -> tuple[str | Text, ...]:
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
        preferred_index = self._current_index()
        self.phase = "choose"
        self.query_one("#review-tree").display = False
        self.query_one("#library-filters").display = True
        self.query_one("#tracks").display = True
        self.sub_title = "Choose tracks to analyze"
        self.refresh_bindings()
        self.query_one("#tracks-pane .section-title", Static).update(
            "Library · choose tracks to analyze"
        )
        self._rebuild_table(preferred_index)
        self.call_after_refresh(self._sync_table_columns)

    def _show_review(self) -> None:
        preferred_index = self._current_index()
        self.phase = "review"
        self.query_one("#tracks").display = False
        self.query_one("#library-filters").display = False
        self.query_one("#library-empty").display = False
        self.query_one("#review-tree").display = True
        self.sub_title = "Review analyzed tracks"
        self.refresh_bindings()
        self.query_one("#tracks-pane .section-title", Static).update(
            "Review · tracks and proposed changes"
        )
        self._rebuild_table(preferred_index)

    def _filtered_indices(self) -> list[int]:
        if self.phase == "review":
            return sorted(self.review_indices)

        # ui-count: enumerate the view entries for filtering
        indices = list(range(len(self.entries)))
        if self.library_filter == "needs_analysis":
            indices = [index for index in indices if self.entries[index].needs_analysis]
        elif self.library_filter == "missing_genre":
            indices = [index for index in indices if self.entries[index].is_missing_standard_genre]
        elif self.library_filter == "current":
            indices = [index for index in indices if self.entries[index].is_current_unplanned]
        if self.genre_filter == "all":
            return indices
        if self.genre_filter == "missing_genre":
            return [index for index in indices if self.entries[index].is_missing_standard_genre]
        relations = {"match"} if self.genre_filter == "matches" else {"different", "missing"}
        return [
            index
            for index in indices
            if genre_check(self.entries[index], self._row_context).relation in relations
        ]

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
        if self.phase == "review":
            tree = self.query_one(ReviewTree)
            tree.sync(
                self.entries,
                self.visible_indices,
                self.write_selected,
                self._row_context,
                preferred_index=preferred_index,
                preserve_view=preserve_view,
            )
            if not preserve_view:
                tree.focus()
            if refresh_surrounding:
                self._update_context()
                self._update_status()
                index = (
                    preferred_index
                    if preferred_index in self.visible_indices
                    else next(iter(self.visible_indices), None)
                )
                if index is not None and not preserve_view:
                    self._update_inspector(index)
                elif index is None:
                    self._inspector_state = None
                    self.query_one("#inspector", Static).update("No tracks ready to review.")
            return
        table = self.query_one("#tracks", DataTable)
        scroll_y = table.scroll_y
        self.query_one("#library-empty").display = not self.visible_indices
        table.clear()
        for index in self.visible_indices:
            table.add_row(*self._visible_cells(index), key=str(index))

        if refresh_surrounding:
            self._update_context()
            self._update_status()
        if not self.visible_indices:
            if refresh_surrounding:
                self._inspector_state = None
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
        if self.phase == "choose":
            filters = Text(
                f"F Library: {FILTER_LABELS[self.library_filter]}"
                f"  ·  G Genre: {GENRE_FILTER_LABELS[self.genre_filter]}  ·  "
            )
            filters.append("✓ match", style=GENRE_MATCH_STYLE)
            filters.append("  ·  ")
            filters.append("Review", style=GENRE_REVIEW_STYLE)
            self.query_one("#library-filters", Static).update(filters)
        task_text = ", ".join(TASK_LABELS[task] for task in self.analysis_tasks)
        if self.phase == "choose":
            # ui-count: rows in the library view, not a property of any batch
            needs = sum(entry.needs_analysis for entry in self.entries)
            # ui-count: tracks whose tags could not be read into this view
            errors = sum(entry.metadata_error is not None for entry in self.entries)
            # ui-count: the review selection built up in this session
            ready = len(self.review_indices)
            ready_text = f"  ·  {ready} ready to review" if ready else ""
            text = (
                # ui-count: every row this view has loaded
                f"{len(self.entries)} tracks"
                f"  ·  {needs} need analysis"
                f"{ready_text}"
                f"  ·  {errors} metadata error{'s' if errors != 1 else ''}"
            )
        else:
            # ui-count: analysis failures in this session's review set
            failures = sum(
                self.entries[index].analysis_error is not None for index in self.review_indices
            )
            text = (
                # ui-count: the review selection for this session
                f"{len(self.review_indices)} ready to review"
                f"  ·  {failures} analysis error{'s' if failures != 1 else ''}"
            )
        self.query_one("#context", Static).update(
            f"{text}\nTasks: {task_text}  ·  {_display_path(self.source)}"
        )

    @on(DataTable.RowHighlighted, "#tracks")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        value = event.row_key.value
        if self.phase == "choose" and value is not None:
            self._update_inspector(int(value))

    @on(DataTable.RowSelected, "#tracks")
    def row_selected(self) -> None:
        if self.phase == "choose":
            self._open_details()

    @on(Tree.NodeHighlighted, "#review-tree")
    def review_node_highlighted(self, event: Tree.NodeHighlighted[NodeKey]) -> None:
        if self.phase == "review" and event.node.data is not None:
            self._update_inspector(event.node.data[0])

    @on(Tree.NodeSelected, "#review-tree")
    def review_node_selected(self, event: Tree.NodeSelected[NodeKey]) -> None:
        if self.phase != "review" or event.node.data is None:
            return
        if event.node.children:
            event.node.toggle()
        else:
            self._open_details()

    def _open_details(self) -> None:
        if not self.has_class("details-open"):
            self.action_toggle_details()
        else:
            self.query_one("#inspector-scroll", VerticalScroll).focus()

    def _current_index(self) -> int | None:
        if self.phase == "review":
            return self.query_one(ReviewTree).current_index
        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        # ui-count: rows currently visible under the active filter
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
        text = "\n".join(lines)
        state = (self.phase, index, text)
        if state == self._inspector_state:
            return
        self._inspector_state = state
        inspector.update(text)
        inspector_scroll.scroll_home(animate=False)

    def _metadata_inspector(self, entry: TrackEntry, index: int) -> list[str]:
        identity = ["", entry.path.name, _display_path(entry.path.parent)]
        lines: list[str] = []
        if entry.metadata_error is not None:
            return [
                *lines,
                "Metadata could not be read",
                f"  {entry.metadata_error.description}",
                "",
                "This track cannot be analyzed safely until its metadata is readable.",
                *identity,
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
                f"Standard genre: {genre}",
                *genre_check(entry, self._row_context).details,
                "",
                f"Analysis: {cache_status or STATUS_LABELS[metadata.status]}",
                f"Last analyzed: {self._full_analyzed_at(entry)}",
                "Analysis freshness does not indicate genre agreement.",
                *(
                    [f"Staged file genre: {', '.join(entry.plan.target_file_genre) or 'None'}"]
                    if entry.plan is not None and entry.plan.target_file_genre is not None
                    else []
                ),
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
                *identity,
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
        identity = ["", entry.path.name, _display_path(entry.path.parent)]
        lines: list[str] = []
        if entry.analysis_error is not None:
            return [
                *lines,
                "Analysis failed",
                f"  {entry.analysis_error.description}",
                "",
                "Return to the library with B to retry or choose another track.",
                *identity,
            ]
        if entry.plan is None:
            return ["No analysis result is available.", *identity]

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
                *genre_check(entry, self._row_context).details,
                "",
                f"Candidates · {self._candidate_policy()}",
            ]
        )
        lines.extend(self._task_candidate_sections(item.desired))
        lines.extend(identity)
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
                # ui-count: entries in this task's evidence list, shown only in this panel
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
        if self.phase == "review":
            self.query_one(ReviewTree).update_track(
                index,
                self.entries[index],
                index in self.write_selected,
                self._row_context,
            )
            if update_inspector:
                self._update_inspector(index)
            self._update_status()
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
                    # ui-count: background tracks queued for this analysis run
                    f"{len(self._pending_analysis_indices)} complete"
                )

            if self.phase == "choose":
                review_hint = (
                    # ui-count: the review selection built up in this session
                    f"  ·  V review {len(self.review_indices)} ready" if self.review_indices else ""
                )
                base = (
                    f"{progress}{review_hint}  ·  I details  ·  F filter  ·  Esc stop after current"
                )
            else:
                # ui-count: rows checked for writing in the current view
                selected = len(self.write_selected)
                base = (
                    f"{progress}"
                    f"  ·  {selected} completed track"
                    f"{'s' if selected != 1 else ''} ready to write"
                    "  ·  W review completed writes"
                    "  ·  Esc stop after current"
                )
            self.query_one("#status", Static).update(f"{message}  ·  {base}" if message else base)
            return

        if self.phase == "choose":
            # ui-count: how much of the current filtered view is selected
            selected = sum(index in self.analysis_selected for index in self.visible_indices)
            review_hint = (
                # ui-count: the review selection built up in this session
                f"  ·  V review {len(self.review_indices)} ready" if self.review_indices else ""
            )
            base = (
                f"R analyze selected  ·  Enter details  ·  {selected} selected in this view"
                f"{review_hint}"
            )
        else:
            # ui-count: rows checked for writing in the current view
            selected = len(self.write_selected)
            # ui-count: staged edits among the currently checked rows, before any preflight
            genre_edits = sum(
                self.entries[index].has_standard_genre_change for index in self.write_selected
            )
            base = (
                f"W review write  ·  {selected} will be written"
                f"  ·  {genre_edits} standard genre edits"
                "  ·  Space toggles track"
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
        self._rebuild_table(preserve_view=True)

    def action_toggle_details(self) -> None:
        visible = not self.has_class("details-open")
        self.set_class(visible, "details-open")
        track_view = (
            self.query_one(ReviewTree)
            if self.phase == "review"
            else self.query_one("#tracks", DataTable)
        )
        inspector_scroll = self.query_one("#inspector-scroll", VerticalScroll)
        index = self._current_index()
        if visible:
            if index is not None:
                self._update_inspector(index)
        else:
            track_view.focus()
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
        # ui-count: the fixed set of library filter options this view cycles through
        self.library_filter = FILTER_ORDER[(position + 1) % len(FILTER_ORDER)]
        self._rebuild_table()

    def action_cycle_genre_filter(self) -> None:
        if self.busy or self.phase != "choose":
            return
        position = GENRE_FILTER_ORDER.index(self.genre_filter)
        # ui-count: cycle the fixed genre filter options
        self.genre_filter = GENRE_FILTER_ORDER[(position + 1) % len(GENRE_FILTER_ORDER)]
        self._rebuild_table()

    def action_review(self) -> None:
        if self.busy or self.phase != "choose":
            return
        if not self.review_indices:
            self.notify("There are no analyzed tracks ready to review.")
            return
        if self.analysis_running:
            self._analysis_navigation_changed = True
        self._show_review()

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
