"""The write flow: confirming and applying a reviewed batch of tag writes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from textual import work

from settag.freshness import EnrichmentState
from settag.journal import BatchRecorder
from settag.plans import PlannedWrite
from settag.tui.core import SetTagAppCore
from settag.tui.screens import ConfirmWriteScreen, ErrorScreen
from settag.workflow import (
    AnalysisFailure,
    PartialWriteError,
    PreparedWrite,
    apply_prepared,
    preflight_plan,
    save_plan,
    summarize_writes,
)


class WriteFlow(SetTagAppCore):
    """Previews, confirms, and applies a reviewed batch of tag writes."""

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
        self._save_plan(planned, failures)

    @work(thread=True, exclusive=True, group="save", exit_on_error=False)
    def _save_plan(
        self,
        planned: tuple[PlannedWrite, ...],
        failures: tuple[AnalysisFailure, ...],
    ) -> None:
        # One small file, but written off the event loop like every other write
        # in the app: a slow network volume must not freeze the UI for a keypress.
        try:
            path = save_plan(planned, failures=failures)
        except OSError as error:
            self.call_from_thread(self.push_screen, ErrorScreen("Could not save plan", str(error)))
            return
        self.call_from_thread(self._plan_saved, path)

    def _plan_saved(self, path: Path) -> None:
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
                error.completed,
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
        completed: int,
    ) -> None:
        self._accept_written(written)
        self._written_count += completed
        if self.review_indices:
            self._show_review()
        else:
            self._show_library()
        self._write_failed(title, message)

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
            if entry.metadata is not None and item.enrichment is not None:
                entry.metadata = replace(
                    entry.metadata,
                    enrichment=EnrichmentState(
                        item.enrichment, item.desired.get("SETTAG_BEATPORT")
                    ),
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
            if item is not None and item.needs_write_review:
                items.append(item)
        return tuple(items)

    def _review_failures(self) -> tuple[AnalysisFailure, ...]:
        return tuple(
            failure
            for index in sorted(self.review_indices)
            if (failure := self.entries[index].analysis_error) is not None
        )
