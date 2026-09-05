"""The analysis flow: running background analysis and reviewing its results."""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.widgets import ProgressBar, Static

from settag.plans import PlannedWrite, stage_default_file_genre
from settag.tui.core import SetTagAppCore
from settag.tui.screens import ErrorScreen
from settag.workflow import AnalysisBatch, AnalysisFailure


class AnalysisFlow(SetTagAppCore):
    """Runs background analysis and persists and reports its results."""

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
        # ui-count: rows selected in this view for this analysis run
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
            plan = next((item for item in batch.planned if item.path == path), None)
            staged = stage_default_file_genre(plan) if plan is not None else None
            # Persisted here, on the worker thread, so a slow or locked workbench
            # holds this thread rather than the event loop. The main thread only
            # learns the outcome, alongside the plan it is about to display.
            persist_error = self._persist_item(staged) if staged is not None else None
            self.call_from_thread(
                self._analysis_item_complete,
                index,
                batch,
                completed,
                total,
                staged,
                persist_error,
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
        # ui-count: background tracks queued for this analysis run
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
        # ui-count: background tracks queued for this analysis run
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

        # ui-count: background tracks queued for this analysis run
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
        # ui-count: background tracks queued for this analysis run
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
        plan: PlannedWrite | None,
        persist_error: str | None,
    ) -> None:
        entry = self.entries[index]
        failure = next(
            (item for item in batch.failures if item.path == entry.path),
            None,
        )
        if plan is not None:
            entry.plan = plan
            entry.plan_cached = False
            entry.analysis_error = None
            self.review_indices.add(index)
            if plan.readable_changes:
                self.write_selected.add(index)
            if persist_error is not None:
                self._report_persist_failure(persist_error)
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
            self._rebuild_table(preserve_view=True)

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

    def _persist(self, index: int) -> None:
        """Persist one entry's plan from the main thread, after a user edit."""
        item = self.entries[index].plan
        if item is None:
            return
        error = self._persist_item(item)
        if error is not None:
            self._report_persist_failure(error)

    def _persist_item(self, item: PlannedWrite) -> str | None:
        """Save one plan to the workbench; safe on any thread. Returns the failure, if any."""
        if self.persist_plan is None:
            return None
        try:
            self.persist_plan(item)
        except Exception as error:
            return f"{type(error).__name__}: {error}"
        return None

    def _report_persist_failure(self, error: str) -> None:
        self.notify(
            "The result is still available in this session, but could not "
            f"be saved to the local workbench: {error}",
            severity="warning",
            timeout=8,
        )
