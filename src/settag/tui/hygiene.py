"""The independent metadata-hygiene review app.

THESIS: hygiene is a field-level inspection bench, never an automatic broom.
OWN-WORLD: Booth Compass surfaces, one Ember cursor, dense metadata rows.
STORY: see the suspicious value, understand the rule, choose, verify, clean.
FIRST VIEWPORT: findings dominate; exact evidence stays one keystroke away.
FORM: an Operate-mode extension of SetTag's incumbent track-table workspace.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

from settag.hygiene import (
    HygieneBatch,
    HygieneFailure,
    HygieneFinding,
    HygienePlan,
    HygieneSummary,
    HygieneTrack,
    PartialHygieneWriteError,
    PreparedHygiene,
    apply_hygiene,
    inspect_hygiene_paths,
    plan_hygiene_track,
    preflight_hygiene,
    summarize_hygiene,
)
from settag.journal import BatchRecorder, WriteJournal
from settag.tui.entries import TuiOutcome
from settag.tui.screens import ConfirmWriteScreen, ErrorScreen
from settag.tui.style import APP_CSS


@dataclass(frozen=True)
class HygieneReviewRow:
    path: Path
    track: HygieneTrack | None = None
    finding: HygieneFinding | None = None
    failure: HygieneFailure | None = None

    @property
    def is_selectable(self) -> bool:
        return self.track is not None and self.finding is not None


class HygieneApp(App[TuiOutcome]):
    """Review suspicious text fields without loading an analysis model."""

    TITLE = "SetTag Hygiene"
    ENABLE_COMMAND_PALETTE = False
    CSS = APP_CSS
    BINDINGS = [
        Binding("space", "toggle_finding", "Toggle"),
        Binding("a", "toggle_all", "All/None"),
        Binding("i", "toggle_details", "Details"),
        Binding("w", "write", "Clean"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        source: Path,
        paths: Sequence[Path],
        batch: HygieneBatch | None = None,
        journal: WriteJournal,
    ) -> None:
        super().__init__()
        self.source = source.expanduser().resolve()
        self.paths = tuple(paths)
        self.batch = batch
        self.journal = journal
        self.rows: list[HygieneReviewRow] = []
        self.selected: set[int] = set()
        self.busy = batch is None
        self._pending: tuple[HygienePlan, ...] = ()
        self._pending_prepared: tuple[PreparedHygiene, ...] = ()
        if batch is not None:
            self._rebuild_rows(select_all=True)
        self.sub_title = "Scanning metadata" if batch is None else "Review suspicious metadata"

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="hygiene-main"):
            yield Static("", markup=False, id="context")
            with Horizontal(id="workspace"):
                with Vertical(id="tracks-pane"):
                    yield Static(
                        "Hygiene review · checked tags will be cleaned",
                        classes="section-title",
                    )
                    yield DataTable(id="hygiene-table", cursor_type="row")
                with Vertical(id="inspector-pane"):
                    yield Static("Finding details", classes="section-title")
                    with VerticalScroll(id="inspector-scroll", can_focus=True):
                        yield Static("", markup=False, id="inspector")
            yield Static("", markup=False, id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#hygiene-table", DataTable)
        table.add_column("", key="selected", width=1)
        table.add_column("Track", key="track", width=26)
        table.add_column("Tag", key="tag", width=18)
        table.add_column("Current value", key="current", width=34)
        table.add_column("Why flagged", key="reason", width=24)
        self._update_layout(self.size.width)
        self._rebuild_table()
        if self.batch is None:
            self._load_hygiene()

    def on_resize(self, event: events.Resize) -> None:
        self._update_layout(event.size.width)

    def _update_layout(self, width: int) -> None:
        self.set_class(width < 90, "narrow")

    def _rebuild_rows(self, *, select_all: bool) -> None:
        assert self.batch is not None
        finding_rows = [
            HygieneReviewRow(path=track.path, track=track, finding=finding)
            for track in self.batch.tracks
            for finding in track.findings
        ]
        failure_rows = [
            HygieneReviewRow(path=failure.path, failure=failure) for failure in self.batch.failures
        ]
        self.rows = [*finding_rows, *failure_rows]
        self.selected = (
            {index for index, row in enumerate(self.rows) if row.is_selectable}
            if select_all
            else set()
        )

    def _rebuild_table(self, *, message: str | None = None) -> None:
        table = self.query_one("#hygiene-table", DataTable)
        table.clear()
        if self.batch is None:
            self._update_context()
            self._update_status(message or "Preparing metadata scan…")
            self.query_one("#inspector", Static).update(
                "Scanning comment-like and generated text fields…"
            )
            return
        for index, row in enumerate(self.rows):
            if row.finding is not None:
                table.add_row(
                    "✓" if index in self.selected else "",
                    row.path.name,
                    row.finding.label,
                    row.finding.current_text,
                    row.finding.reason_text,
                    key=str(index),
                )
            else:
                assert row.failure is not None
                table.add_row(
                    "!",
                    row.path.name,
                    "Inspection error",
                    "Could not inspect",
                    row.failure.description,
                    key=str(index),
                )
        self._update_context()
        self._update_status(message)
        if self.rows:
            table.focus()
            table.move_cursor(row=0)
            self._update_inspector(0)
        else:
            self.query_one("#inspector", Static).update(
                "All scanned files are clean. No suspicious comments, URLs, duplicate "
                "values, empty values, or generated encoder markers were found."
            )

    def _update_context(self) -> None:
        if self.batch is None:
            self.query_one("#context", Static).update(
                f"{self.source}\nScanning metadata without loading an analysis model"
            )
            return
        self.query_one("#context", Static).update(
            f"{self.source}\n"
            f"{self.batch.affected_track_count} affected of {len(self.batch.tracks)} scanned"
            f"  ·  {self.batch.finding_count} suggestions"
            f"  ·  {len(self.batch.failures)} errors"
        )

    def _update_status(self, message: str | None = None) -> None:
        if self.busy:
            if message is not None:
                self.query_one("#status", Static).update(message)
            return
        base = (
            f"{len(self.selected)} checked"
            "  ·  Space toggle  ·  A all/none  ·  I details  ·  W review cleanup"
        )
        self.query_one("#status", Static).update(f"{message}  ·  {base}" if message else base)

    def _current_row(self) -> int | None:
        table = self.query_one("#hygiene-table", DataTable)
        if not self.rows or table.cursor_row < 0 or table.cursor_row >= len(self.rows):
            return None
        return table.cursor_row

    @work(thread=True, exclusive=True, group="hygiene-scan", exit_on_error=False)
    def _load_hygiene(self) -> None:
        batch = inspect_hygiene_paths(self.paths, on_progress=self._scan_progress_from_worker)
        self.call_from_thread(self._scan_complete, batch)

    def _scan_progress_from_worker(self, completed: int, total: int, path: Path) -> None:
        self.call_from_thread(
            self._update_status,
            f"Inspecting {completed} of {total}: {path.name}",
        )

    def _scan_complete(self, batch: HygieneBatch) -> None:
        self.batch = batch
        self.busy = False
        self.sub_title = "Review suspicious metadata"
        self._rebuild_rows(select_all=True)
        self._rebuild_table()

    @on(DataTable.RowHighlighted, "#hygiene-table")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is None:
            return
        self._update_inspector(int(str(event.row_key.value)))

    def _update_inspector(self, index: int) -> None:
        row = self.rows[index]
        if row.failure is not None:
            self.query_one("#inspector", Static).update(
                "\n".join(
                    (
                        row.path.name,
                        str(row.path.parent),
                        "",
                        "Inspection error",
                        row.failure.description,
                        "",
                        "This file was not classified as clean and cannot enter a cleanup write.",
                    )
                )
            )
            return
        track = row.track
        finding = row.finding
        assert track is not None
        assert finding is not None
        after = "\n".join(finding.after) if finding.after else "Remove this tag"
        current = "\n".join(value or "(empty)" for value in finding.before) or "(empty tag)"
        self.query_one("#inspector", Static).update(
            "\n".join(
                (
                    track.path.name,
                    str(track.path.parent),
                    "",
                    f"Tag: {finding.label}",
                    f"Container: {track.metadata_format}",
                    f"Reason: {finding.reason_text}",
                    "",
                    "Current value",
                    current,
                    "",
                    "After cleanup",
                    after,
                    "",
                    (
                        "Only this checked field-level suggestion is staged. "
                        "Other metadata is preserved."
                    ),
                )
            )
        )

    def action_toggle_finding(self) -> None:
        if self.busy:
            return
        index = self._current_row()
        if index is None:
            return
        if not self.rows[index].is_selectable:
            self.notify(
                "This file could not be inspected and cannot be selected.", severity="warning"
            )
            return
        if index in self.selected:
            self.selected.remove(index)
        else:
            self.selected.add(index)
        self._refresh_row(index)

    def _refresh_row(self, index: int) -> None:
        table = self.query_one("#hygiene-table", DataTable)
        table.update_cell(str(index), "selected", "✓" if index in self.selected else "")
        self._update_status()

    def action_toggle_all(self) -> None:
        if self.busy or not self.rows:
            return
        eligible = {index for index, row in enumerate(self.rows) if row.is_selectable}
        if not eligible:
            return
        self.selected = set() if eligible.issubset(self.selected) else eligible
        self._rebuild_table()

    def action_toggle_details(self) -> None:
        visible = not self.has_class("details-open")
        self.set_class(visible, "details-open")
        if visible:
            index = self._current_row()
            if index is not None:
                self._update_inspector(index)
            self.call_after_refresh(self.query_one("#inspector-scroll", VerticalScroll).focus)
        else:
            self.query_one("#hygiene-table", DataTable).focus()

    def _selected_plans(self) -> tuple[HygienePlan, ...]:
        grouped: dict[Path, list[HygieneFinding]] = defaultdict(list)
        tracks: dict[Path, HygieneTrack] = {}
        for index in sorted(self.selected):
            row = self.rows[index]
            track = row.track
            finding = row.finding
            assert track is not None
            assert finding is not None
            tracks[track.path] = track
            grouped[track.path].append(finding)
        return tuple(
            plan_hygiene_track(tracks[path], findings) for path, findings in grouped.items()
        )

    def action_write(self) -> None:
        if self.busy:
            return
        plans = self._selected_plans()
        if not plans:
            self.notify("Check at least one hygiene suggestion before cleaning.")
            return
        self.busy = True
        self._pending = plans
        self._update_status("Checking files and staged tag values…")
        self._preflight_for_confirmation(plans)

    @work(thread=True, exclusive=True, group="hygiene-preflight", exit_on_error=False)
    def _preflight_for_confirmation(self, plans: tuple[HygienePlan, ...]) -> None:
        try:
            prepared = preflight_hygiene(plans)
            summary = summarize_hygiene(plans)
        except Exception as error:
            self.call_from_thread(self._failed, "Hygiene check failed", str(error))
            return
        self.call_from_thread(self._show_confirmation, prepared, summary)

    def _show_confirmation(
        self,
        prepared: tuple[PreparedHygiene, ...],
        summary: HygieneSummary,
    ) -> None:
        self._pending_prepared = prepared
        self.push_screen(ConfirmWriteScreen(summary), self._confirmed)

    def _confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.busy = False
            self._pending = ()
            self._pending_prepared = ()
            self._update_status("Cleanup cancelled; nothing changed")
            return
        self._update_status("Rechecking files before cleanup…")
        self._apply_pending()

    @work(thread=True, exclusive=True, group="hygiene-write", exit_on_error=False)
    def _apply_pending(self) -> None:
        recorder = BatchRecorder(self.journal)
        try:
            prepared = preflight_hygiene(self._pending)
            written = apply_hygiene(
                prepared,
                on_progress=self._progress_from_worker,
                on_write=recorder,
            )
            refreshed = inspect_hygiene_paths(self.paths)
        except PartialHygieneWriteError as error:
            refreshed = inspect_hygiene_paths(self.paths)
            self.call_from_thread(
                self._partly_failed,
                str(error),
                refreshed,
                recorder.error,
            )
            return
        except Exception as error:
            self.call_from_thread(self._failed, "Cleanup failed", str(error))
            return
        self.call_from_thread(self._complete, written, refreshed, recorder)

    def _progress_from_worker(self, completed: int, total: int, path: Path) -> None:
        self.call_from_thread(
            self._update_status,
            f"Cleaning {completed} of {total}: {path.name}",
        )

    def _complete(
        self,
        written: int,
        refreshed: HygieneBatch,
        recorder: BatchRecorder,
    ) -> None:
        self.batch = refreshed
        self._rebuild_rows(select_all=False)
        self.busy = False
        self._pending = ()
        self._pending_prepared = ()
        message = f"Cleaned and verified {written} file{'s' if written != 1 else ''}."
        if recorder.error is not None:
            self.notify(recorder.error, severity="warning", timeout=8)
        elif recorder.recorded:
            message += f" Undo with: settag undo {recorder.batch_id}"
        self._rebuild_table(message=message)
        self.notify(message, title="Hygiene complete", timeout=7)

    def _partly_failed(
        self,
        message: str,
        refreshed: HygieneBatch,
        journal_error: str | None,
    ) -> None:
        self.batch = refreshed
        self._rebuild_rows(select_all=False)
        self.busy = False
        self._pending = ()
        self._pending_prepared = ()
        self._rebuild_table()
        if journal_error is not None:
            self.notify(journal_error, severity="warning", timeout=8)
        self.push_screen(ErrorScreen("Cleanup stopped", message))

    def _failed(self, title: str, message: str) -> None:
        self.busy = False
        self._pending = ()
        self._pending_prepared = ()
        self._update_status("Nothing was changed")
        self.push_screen(ErrorScreen(title, message))

    async def action_quit(self) -> None:
        if self.busy:
            self.notify("A safety check or cleanup write is in progress.", severity="warning")
            return
        self.exit(TuiOutcome(0, "Hygiene review closed."))
