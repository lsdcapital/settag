"""The undo flow: reverting a previous SetTag write from the journal."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from textual import work

from settag.journal import JournalBatch, JournalError, WriteRecord
from settag.tags import OwnedValues
from settag.tui.core import SetTagAppCore
from settag.tui.screens import ConfirmUndoScreen, ErrorScreen, UndoScreen
from settag.workflow import (
    MetadataStatus,
    PartialWriteError,
    UndoPreflight,
    apply_undo,
    preflight_undo,
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


class UndoFlow(SetTagAppCore):
    """Reads the write journal, previews a batch, and restores it."""

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
        # A batch is marked reverted only when nothing in it is left for a forced retry.
        self._pending_undo_batch = batch.batch_id if preflight.restores_everything else None
        self._pending_undo_skipped = preflight.blocked_count
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
        skipped = self._pending_undo_skipped
        self._pending_undo_skipped = 0
        if skipped:
            message += (
                f" {skipped} skipped file{'s' if skipped != 1 else ''} still carr"
                f"{'y' if skipped != 1 else 'ies'} the write; it stays in the undo list."
            )
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
