from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

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

from settag.plans import PlannedWrite, stage_file_genre
from settag.workflow import (
    AnalysisBatch,
    PartialWriteError,
    ProgressCallback,
    apply_prepared,
    preflight_plan,
    save_plan,
)

BatchLoader = Callable[[ProgressCallback], AnalysisBatch]


@dataclass(frozen=True)
class TuiOutcome:
    status: int
    message: str


def suggested_file_genre(item: PlannedWrite) -> str | None:
    """Return the direct child label without performing taxonomy mapping."""
    if not item.selected:
        return None
    return item.selected[0].label.rsplit("---", 1)[-1].strip() or None


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
            yield Label("Edit standard file genre", id="dialog-title")
            yield Static(
                "This changes the conventional genre tag only for this track.\n"
                "Leave the field empty to clear it.",
                markup=False,
                id="dialog-help",
            )
            yield Input(
                value=", ".join(current),
                placeholder=suggestion or "Genre",
                id="genre-input",
            )
            if suggestion:
                yield Static(
                    f"Model suggestion: {suggestion}",
                    markup=False,
                    id="dialog-suggestion",
                )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Stage change", variant="primary", id="stage")

    def on_mount(self) -> None:
        self.query_one("#genre-input", Input).focus()

    @on(Input.Submitted)
    def submit_genre(self) -> None:
        self._submit()

    @on(Button.Pressed, "#stage")
    def stage_genre(self) -> None:
        self._submit()

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
        Binding("y", "confirm", "Write"),
        Binding("n,escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        *,
        track_count: int,
        standard_genre_count: int,
        field_change_count: int,
    ) -> None:
        super().__init__()
        self.track_count = track_count
        self.standard_genre_count = standard_genre_count
        self.field_change_count = field_change_count

    def compose(self) -> ComposeResult:
        noun = "track" if self.track_count == 1 else "tracks"
        with Vertical(id="confirm-dialog"):
            yield Label("Write staged metadata?", id="dialog-title")
            yield Static(
                f"{self.track_count} {noun}\n"
                f"{self.field_change_count} SetTag field changes\n"
                f"{self.standard_genre_count} standard genre edits",
                markup=False,
                id="confirm-summary",
            )
            yield Static(
                "Sources passed preflight. They will be checked again before writing.",
                markup=False,
                id="dialog-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Write and verify", variant="primary", id="confirm")

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
    """The single interactive SetTag interface.

    The palette begins with an OKLCH crimson seed from the project design
    system. Textual currently accepts sRGB terminal colors, so the stylesheet
    uses its closest practical terminal equivalents.
    """

    TITLE = "SetTag"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("space", "toggle_track", "Include"),
        Binding("a", "select_all", "All"),
        Binding("n", "select_none", "None"),
        Binding("g", "use_suggestion", "Use suggestion"),
        Binding("e", "edit_genre", "Edit genre"),
        Binding("s", "save", "Save plan"),
        Binding("w", "write", "Write"),
        Binding("q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        background: #111111;
        color: #f3f1f1;
    }

    Header {
        background: #272323;
        color: #f3f1f1;
    }

    Footer {
        background: #272323;
        color: #d8d3d3;
    }

    Footer > .footer--highlight,
    Footer > .footer--key {
        background: #b63f38;
        color: #ffffff;
    }

    #loading {
        align: center middle;
        height: 1fr;
        padding: 2 6;
    }

    #loading-title {
        text-style: bold;
        color: #ffffff;
        margin-bottom: 1;
    }

    #loading-path {
        color: #bdb6b6;
        margin-bottom: 1;
    }

    #analysis-progress {
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
        color: #bdb6b6;
        background: #181717;
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
        width: 1fr;
        min-width: 34;
        padding: 0 2 1 1;
        background: #181717;
    }

    .section-title {
        height: 2;
        padding: 0 1;
        text-style: bold;
        color: #ffffff;
    }

    DataTable {
        height: 1fr;
        background: #111111;
        color: #e9e5e5;
        scrollbar-color: #645d5d;
        scrollbar-color-hover: #807575;
        scrollbar-color-active: #b63f38;
    }

    DataTable > .datatable--header {
        background: #272323;
        color: #ffffff;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #63302d;
        color: #ffffff;
    }

    #inspector {
        height: 1fr;
        padding: 0 1;
        color: #e4dfdf;
        overflow-y: auto;
    }

    #status {
        height: 3;
        padding: 1 2;
        background: #272323;
        color: #d8d3d3;
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
        background: #272323;
        border: solid #817575;
    }

    #dialog-title {
        text-style: bold;
        color: #ffffff;
        margin-bottom: 1;
    }

    #dialog-help,
    #dialog-suggestion {
        color: #c8c0c0;
        margin-bottom: 1;
    }

    #genre-input {
        margin-bottom: 1;
        border: tall #817575;
    }

    #genre-input:focus {
        border: tall #c84d45;
    }

    #confirm-summary,
    #error-message {
        margin-bottom: 1;
        color: #f3f1f1;
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

    Button.-primary {
        background: #b63f38;
        color: #ffffff;
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
        loader: BatchLoader | None = None,
        initial_batch: AnalysisBatch | None = None,
    ) -> None:
        super().__init__()
        if loader is None and initial_batch is None:
            raise ValueError("SetTagApp requires a batch loader or initial batch")
        self.source = source.expanduser().resolve()
        self.loader = loader
        self.initial_batch = initial_batch
        self.batch: AnalysisBatch | None = None
        self.items: list[PlannedWrite] = []
        self.selected: set[int] = set()
        self.busy = False
        self._pending_write: tuple[PlannedWrite, ...] = ()
        self.sub_title = str(self.source)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="loading"):
            yield Static("Analyzing your music", markup=False, id="loading-title")
            yield Static(str(self.source), markup=False, id="loading-path")
            yield Static("Preparing the model…", markup=False, id="loading-status")
            yield ProgressBar(total=100, show_eta=False, id="analysis-progress")
        with Vertical(id="main"):
            yield Static("", markup=False, id="context")
            with Horizontal(id="workspace"):
                with Vertical(id="tracks-pane"):
                    yield Static("Tracks", markup=False, classes="section-title")
                    yield DataTable(
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
        table = self.query_one("#tracks", DataTable)
        table.add_columns("", "Track", "File genre", "Suggested", "Score", "Changes")
        if self.initial_batch is not None:
            self._show_batch(self.initial_batch)
        else:
            self._load_analysis()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 100, "narrow")

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _load_analysis(self) -> None:
        assert self.loader is not None
        try:
            batch = self.loader(self._progress_from_worker)
        except Exception as error:
            self.call_from_thread(
                self._show_fatal_error,
                f"{type(error).__name__}: {error}",
            )
            return
        self.call_from_thread(self._show_batch, batch)

    def _progress_from_worker(self, completed: int, total: int, path: Path) -> None:
        self.call_from_thread(self._update_progress, completed, total, path.name)

    def _update_progress(self, completed: int, total: int, name: str) -> None:
        self.query_one("#loading-status", Static).update(
            f"{completed} of {total} · {name}"
        )
        self.query_one("#analysis-progress", ProgressBar).update(
            total=total,
            progress=completed,
        )

    def _show_fatal_error(self, message: str) -> None:
        self.exit(TuiOutcome(2, f"SetTag could not start: {message}"))

    def _show_batch(self, batch: AnalysisBatch) -> None:
        self.batch = batch
        self.items = list(batch.planned)
        self.selected = {
            index
            for index, item in enumerate(self.items)
            if bool(item.readable_changes)
        }

        table = self.query_one("#tracks", DataTable)
        table.clear()
        for index, item in enumerate(self.items):
            table.add_row(*self._row_cells(index, item), key=str(index))

        self.query_one("#loading").display = False
        self.query_one("#main").display = True
        failures = len(batch.failures)
        context = (
            f"{self.source}  ·  {len(self.items)} analyzed"
            f"  ·  {failures} error{'s' if failures != 1 else ''}"
        )
        self.query_one("#context", Static).update(context)
        self._update_status()

        if self.items:
            table.focus()
            table.move_cursor(row=0)
            self._update_inspector(0)
        else:
            self.query_one("#inspector", Static).update(
                "No tracks completed analysis."
            )

    def _row_cells(
        self,
        index: int,
        item: PlannedWrite,
    ) -> tuple[str, str, str, str, str, str]:
        primary = item.selected[0] if item.selected else None
        before = ", ".join(item.file_genre) or "None"
        if item.target_file_genre is not None:
            after = ", ".join(item.target_file_genre) or "None"
            file_genre = f"{before} → {after}"
        else:
            file_genre = before
        return (
            "[x]" if index in self.selected else "[ ]",
            item.path.name,
            file_genre,
            suggested_file_genre(item) or "—",
            f"{primary.score:.3f}" if primary else "—",
            str(len(item.readable_changes)),
        )

    @on(DataTable.RowHighlighted, "#tracks")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._update_inspector(event.cursor_row)

    @on(DataTable.RowSelected, "#tracks")
    def row_selected(self) -> None:
        self.action_edit_genre()

    def _current_index(self) -> int | None:
        if not self.items:
            return None
        row = self.query_one("#tracks", DataTable).cursor_row
        return row if 0 <= row < len(self.items) else None

    def _update_inspector(self, index: int) -> None:
        if not 0 <= index < len(self.items):
            return
        item = self.items[index]
        current = ", ".join(item.file_genre) or "None"
        if item.target_file_genre is None:
            genre_line = f"  {current} (unchanged)"
        else:
            target = ", ".join(item.target_file_genre) or "None"
            genre_line = f"  {current} → {target} (staged)"

        lines = [
            item.path.name,
            str(item.path.parent),
            "",
            "Standard file genre",
            genre_line,
            "",
            "Ranked model evidence",
        ]
        if item.selected:
            width = max(len(prediction.label) for prediction in item.selected)
            lines.extend(
                f"  {rank:>2}. {prediction.label:<{width}}  {prediction.score:.3f}"
                for rank, prediction in enumerate(item.selected, start=1)
            )
        else:
            lines.append("  No labels met the threshold.")

        lines.extend(["", f"Staged metadata changes ({len(item.readable_changes)})"])
        if item.readable_changes:
            lines.extend(f"  • {change}" for change in item.readable_changes)
        else:
            lines.append("  None")
        lines.extend(
            [
                "",
                "SetTag evidence and scores are written together.",
                "A standard genre changes only when explicitly staged here.",
            ]
        )
        self.query_one("#inspector", Static).update("\n".join(lines))

    def _refresh_row(self, index: int) -> None:
        table = self.query_one("#tracks", DataTable)
        for column, value in enumerate(self._row_cells(index, self.items[index])):
            table.update_cell_at(
                Coordinate(index, column),
                value,
                update_width=True,
            )
        self._update_inspector(index)
        self._update_status()

    def _update_status(self, message: str | None = None) -> None:
        selected = len(self.selected)
        genre_edits = sum(
            self.items[index].standard_genre_change is not None
            for index in self.selected
        )
        base = (
            f"{selected} included  ·  {genre_edits} standard genre edits"
            "  ·  Space include/exclude  ·  W preflight and write"
        )
        self.query_one("#status", Static).update(
            f"{message}  ·  {base}" if message else base
        )

    def action_toggle_track(self) -> None:
        if self.busy:
            return
        index = self._current_index()
        if index is None:
            return
        if index in self.selected:
            self.selected.remove(index)
        elif self.items[index].readable_changes:
            self.selected.add(index)
        self._refresh_row(index)

    def action_select_all(self) -> None:
        if self.busy:
            return
        self.selected = {
            index
            for index, item in enumerate(self.items)
            if bool(item.readable_changes)
        }
        self._refresh_all_rows()

    def action_select_none(self) -> None:
        if self.busy:
            return
        self.selected.clear()
        self._refresh_all_rows()

    def _refresh_all_rows(self) -> None:
        for index in range(len(self.items)):
            self._refresh_row(index)

    def action_use_suggestion(self) -> None:
        if self.busy:
            return
        index = self._current_index()
        if index is None:
            return
        suggestion = suggested_file_genre(self.items[index])
        if suggestion is None:
            self.notify(
                "No selected model label is available for this track.",
                severity="warning",
            )
            return
        self.items[index] = stage_file_genre(
            self.items[index],
            (suggestion,),
        )
        self.selected.add(index)
        self._refresh_row(index)
        self._update_status(f"Staged file genre “{suggestion}”")

    def action_edit_genre(self) -> None:
        if self.busy:
            return
        index = self._current_index()
        if index is None:
            return
        self.push_screen(
            GenreEditScreen(self.items[index]),
            lambda result: self._genre_edited(index, result),
        )

    def _genre_edited(self, index: int, result: str | None) -> None:
        if result is None:
            return
        genres = tuple(
            value.strip()
            for value in result.split(",")
            if value.strip()
        )
        self.items[index] = stage_file_genre(self.items[index], genres)
        if self.items[index].readable_changes:
            self.selected.add(index)
        self._refresh_row(index)
        self._update_status("Standard file genre edit staged")

    def action_save(self) -> None:
        if self.busy:
            return
        planned = self._selected_items()
        if not planned:
            self.notify("Include at least one changed track first.", severity="warning")
            return
        failures = self.batch.failures if self.batch is not None else ()
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
        if self.batch is not None and self.batch.failures:
            self.notify(
                "Writing is disabled because one or more tracks failed analysis.",
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

    def _confirm_preflight(self, prepared: Sequence[object]) -> None:
        self.busy = False
        track_count = len(self._pending_write)
        standard_count = sum(
            item.standard_genre_change is not None for item in self._pending_write
        )
        field_count = sum(len(item.owned_changes) for item in self._pending_write)
        self.push_screen(
            ConfirmWriteScreen(
                track_count=track_count,
                standard_genre_count=standard_count,
                field_change_count=field_count,
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
            self.call_from_thread(
                self._write_failed,
                "Write stopped",
                str(error),
            )
            return
        except Exception as error:
            self.call_from_thread(
                self._write_failed,
                "Write failed",
                str(error),
            )
            return
        self.call_from_thread(
            self.exit,
            TuiOutcome(
                0,
                f"Done. {completed} file{'s' if completed != 1 else ''} "
                "written and verified.",
            ),
        )

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
        return tuple(
            self.items[index]
            for index in sorted(self.selected)
            if self.items[index].readable_changes
        )

    def action_quit(self) -> None:
        if self.busy:
            self.notify("A safety check or write is in progress.", severity="warning")
            return
        self.exit(TuiOutcome(0, "Nothing was written."))
