"""Modal screens.

Each one asks a single question and dismisses with the answer. They hold no
application state and compute no counts: what they show is handed to them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static

from settag.journal import JournalBatch
from settag.plans import PlannedWrite, catalog_genres, suggested_file_genre
from settag.tui.entries import suggested_label
from settag.workflow import UndoPreflight


class ConfirmationSummary(Protocol):
    @property
    def confirmation_title(self) -> str: ...

    @property
    def confirmation_action(self) -> str: ...

    @property
    def confirmation_help(self) -> str: ...

    def confirmation_preview(self, *, limit: int = 3) -> str: ...


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
                model_child = suggested_label(self.item.selected)
                source = (
                    " (from verified Beatport matches)"
                    if catalog_genres(self.item.desired)
                    else f" (from model label {model_child})"
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

    def __init__(self, summary: ConfirmationSummary) -> None:
        super().__init__()
        self.summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.summary.confirmation_title, id="dialog-title")
            yield Static(
                self.summary.confirmation_preview(),
                markup=False,
                id="confirm-summary",
            )
            yield Static(
                self.summary.confirmation_help,
                markup=False,
                id="dialog-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Back to review", id="cancel")
                yield Button(
                    self.summary.confirmation_action,
                    variant="primary",
                    id="confirm",
                )

    def on_mount(self) -> None:
        self._update_layout(self.size.width, self.size.height)
        self.query_one("#confirm", Button).focus()

    def on_resize(self, event: events.Resize) -> None:
        self._update_layout(event.size.width, event.size.height)

    def _update_layout(self, width: int, height: int) -> None:
        narrow = width < 64
        compact = narrow or height < 36
        self.set_class(narrow, "narrow")
        preview = (
            self.summary.confirmation_preview(limit=1)
            if compact
            else self.summary.confirmation_preview()
        )
        self.query_one("#confirm-summary", Static).update(preview)

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


class UndoScreen(ModalScreen[str | None]):
    """Pick a previous write batch to revert."""

    # Enter is not bound here: DataTable has its own Enter binding, so the row
    # selection event below is what confirms a choice from the table.
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, batches: Sequence[JournalBatch]) -> None:
        super().__init__()
        self.batches = tuple(batches)

    def compose(self) -> ComposeResult:
        with Vertical(id="undo-dialog"):
            yield Label("Undo a previous write", id="dialog-title")
            table: DataTable[str] = DataTable(id="undo-table", cursor_type="row")
            table.add_columns("Written", "Files", "What changed")
            for batch in self.batches:
                table.add_row(
                    batch.started_at,
                    str(batch.track_count),
                    batch.summary,
                    key=batch.batch_id,
                )
            yield table
            yield Static(
                "Restores the tag values that write replaced. Tag values are "
                "restored, not the original bytes, so the file checksum will differ.",
                markup=False,
                id="dialog-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Revert selected", variant="primary", id="confirm")

    def on_mount(self) -> None:
        self.query_one("#undo-table", DataTable).focus()

    def _selected_batch(self) -> str | None:
        table: DataTable[str] = self.query_one("#undo-table", DataTable)
        if table.row_count == 0:
            return None
        row = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return None if row.value is None else str(row.value)

    @on(DataTable.RowSelected, "#undo-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        value = event.row_key.value
        self.dismiss(None if value is None else str(value))

    @on(Button.Pressed, "#confirm")
    def confirm_button(self) -> None:
        self.action_choose()

    @on(Button.Pressed, "#cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def action_choose(self) -> None:
        self.dismiss(self._selected_batch())

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmUndoScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("enter,y", "confirm", "Restore"),
        Binding("n,escape", "cancel", "Cancel"),
    ]

    def __init__(self, *, batch: JournalBatch, preflight: UndoPreflight) -> None:
        super().__init__()
        self.journal_batch = batch
        self.preflight = preflight

    @property
    def restore_count(self) -> int:
        return self.preflight.restore_count

    @property
    def blocked_count(self) -> int:
        return self.preflight.blocked_count

    def compose(self) -> ComposeResult:
        noun = "file" if self.restore_count == 1 else "files"
        skipped = (
            f"\n{self.blocked_count} skipped because the file changed since"
            if self.blocked_count
            else ""
        )
        reverted = (
            f"\nThis batch was already reverted {self.journal_batch.reverted_at}"
            if self.journal_batch.reverted_at is not None
            else ""
        )
        with Vertical(id="confirm-dialog"):
            yield Label("Restore the previous metadata?", id="dialog-title")
            yield Static(
                f"{self.restore_count} {noun} from the write of {self.journal_batch.started_at}"
                f"{skipped}{reverted}",
                markup=False,
                id="confirm-summary",
            )
            yield Static(
                "Only the SetTag metadata, staged genre edits, and hygiene fields "
                "shown in the write are restored. SetTag will verify each file afterwards.",
                markup=False,
                id="dialog-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    f"Restore {self.restore_count} {noun}",
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
