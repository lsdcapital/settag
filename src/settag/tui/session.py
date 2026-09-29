"""The interactive app's session state, independent of Textual.

Which tracks are selected for enrichment, which are in review, which are checked
for writing, and which are still queued in a running analysis all live here, and
every change to them goes through one of the methods below. The widgets only
render this state; the flows only decide when to change it. Keeping the rules in
one place is what stops a background result, a write, and an undo from each
resetting rows their own way.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from settag.freshness import EnrichmentState
from settag.journal import WriteRecord
from settag.plans import PlannedWrite, stage_file_genre
from settag.tags import OwnedValues, task_evidence_from_owned
from settag.tasks import AnalysisTask
from settag.tui.entries import TrackEntry, latest_analyzed_at
from settag.workflow import AnalysisFailure, MetadataStatus


class WriteToggle(Enum):
    """What pressing Space on a review row did."""

    TOGGLED = "toggled"
    DISMISSED = "dismissed"
    AWAITING_RESULT = "awaiting_result"
    NOT_WRITABLE = "not_writable"


@dataclass
class ReviewSession:
    analysis_tasks: tuple[AnalysisTask, ...]
    entries: list[TrackEntry] = field(default_factory=list)
    analysis_selected: set[int] = field(default_factory=set)
    review_indices: set[int] = field(default_factory=set)
    write_selected: set[int] = field(default_factory=set)
    #: Every track queued in the running analysis, in the order it runs them.
    pending_analysis: tuple[int, ...] = ()
    #: How many of ``pending_analysis`` have produced a result so far.
    analysis_completed: int = 0

    # Loading

    def load(self, entries: Iterable[TrackEntry]) -> None:
        """Start from freshly read metadata and any plans restored from the workbench."""
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
            index for index in self.review_indices if self.entries[index].needs_write_review
        }

    # Analysis

    @property
    def analysis_running(self) -> bool:
        return bool(self.pending_analysis)

    def begin_analysis(self, indices: Sequence[int]) -> None:
        self.pending_analysis = tuple(indices)
        self.analysis_completed = 0

    def end_analysis(self) -> None:
        self.pending_analysis = ()
        self.analysis_completed = 0

    def awaiting_new_result(self, index: int) -> bool:
        """Whether the running analysis has yet to replace this track's plan.

        A track with a partial cached result sits in review and in the analysis
        selection at once. While it is re-enriched, an edit or a write made on the
        old plan would be silently replaced by the new result, or would delete that
        result's workbench row once the write finished. Such tracks are held until
        their new result arrives.
        """
        return index in self.pending_analysis[self.analysis_completed :]

    def accept_result(
        self,
        index: int,
        plan: PlannedWrite | None,
        failure: AnalysisFailure | None,
        *,
        completed: int,
    ) -> None:
        """Show one finished track in review, as a plan to check or a failure to see."""
        entry = self.entries[index]
        if plan is not None:
            entry.plan = plan
            entry.plan_cached = False
            entry.analysis_error = None
            self.review_indices.add(index)
            if plan.needs_write_review:
                self.write_selected.add(index)
            else:
                self.write_selected.discard(index)
        else:
            entry.plan = None
            entry.analysis_error = failure or AnalysisFailure(
                path=entry.path,
                error_type="RuntimeError",
                message="Analyzer returned no result for this track",
            )
            self.review_indices.add(index)
            self.write_selected.discard(index)
        self.analysis_selected.discard(index)
        self.analysis_completed = completed

    # Selection

    def toggle_analysis(self, index: int) -> None:
        _toggle(self.analysis_selected, index)

    def toggle_write(self, index: int) -> WriteToggle:
        entry = self.entries[index]
        if entry.analysis_error is not None:
            self.dismiss_failure(index)
            return WriteToggle.DISMISSED
        if not entry.needs_write_review:
            return WriteToggle.NOT_WRITABLE
        if self.awaiting_new_result(index):
            return WriteToggle.AWAITING_RESULT
        _toggle(self.write_selected, index)
        return WriteToggle.TOGGLED

    def toggle_all_analysis(self, visible: Iterable[int]) -> None:
        eligible = {index for index in visible if self.entries[index].can_analyze}
        _toggle_all(self.analysis_selected, eligible)

    def toggle_all_write(self, visible: Iterable[int]) -> None:
        eligible = {
            index
            for index in visible
            if self.entries[index].needs_write_review and not self.awaiting_new_result(index)
        }
        _toggle_all(self.write_selected, eligible)

    def dismiss_failure(self, index: int) -> None:
        """Take a failed track out of review so it no longer blocks writing the rest.

        The error stays on the track, so the library still shows which file failed and
        why; a file that fails every time can be found and dealt with there.
        """
        assert self.entries[index].analysis_error is not None
        self.review_indices.discard(index)

    def return_to_library(self) -> None:
        # Keep the user's own choices: a track they unchecked stays unchecked. Only tracks
        # that no longer need enrichment drop out; A selects everything again.
        self.analysis_selected = {
            index for index in self.analysis_selected if self.entries[index].needs_analysis
        }

    def stage_genre(self, index: int, genres: tuple[str, ...]) -> PlannedWrite | None:
        item = self.entries[index].plan
        if item is None:
            return None
        updated = stage_file_genre(item, genres)
        self.entries[index].plan = updated
        if updated.needs_write_review:
            self.write_selected.add(index)
        else:
            self.write_selected.discard(index)
        return updated

    # Writing

    def writable_plans(self) -> tuple[PlannedWrite, ...]:
        """Checked plans that can be written now, in library order."""
        items: list[PlannedWrite] = []
        for index in sorted(self.write_selected):
            item = self.entries[index].plan
            if item is not None and item.needs_write_review and not self.awaiting_new_result(index):
                items.append(item)
        return tuple(items)

    def held_for_reanalysis(self) -> list[int]:
        """Checked tracks left out of a write because their new result has not arrived."""
        return [index for index in sorted(self.write_selected) if self.awaiting_new_result(index)]

    def review_failures(self) -> tuple[AnalysisFailure, ...]:
        return tuple(
            failure
            for index in sorted(self.review_indices)
            if (failure := self.entries[index].analysis_error) is not None
        )

    def accept_written(self, items: Sequence[PlannedWrite]) -> None:
        """Show written tracks as the library entries their files now are."""
        by_path = self._indices_by_path()
        for item in items:
            index = by_path[item.path]
            standard_genre = (
                item.target_file_genre if item.target_file_genre is not None else item.file_genre
            )
            self.refresh_entry_metadata(
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
            self._clear(index)

    def accept_reverted(self, records: Sequence[WriteRecord]) -> int:
        """Show restored files as their previous state. Returns review plans cleared."""
        by_path = self._indices_by_path()
        cleared = 0
        for record in records:
            index = by_path.get(record.path)
            if index is None:
                continue
            entry = self.entries[index]
            if entry.plan is not None and entry.needs_write_review:
                cleared += 1
            standard_genre = (
                record.standard_before
                if record.standard_after is not None or entry.metadata is None
                else entry.metadata.genre_state.standard
            )
            self.refresh_entry_metadata(
                index,
                owned=dict(record.owned_before),
                standard_genre=standard_genre,
                status=_restored_status(record.owned_before),
            )
            self._clear(index)
        return cleared

    def refresh_entry_metadata(
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
        stored_genre = task_evidence_from_owned(owned).get("genre", ())
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

    def _clear(self, index: int) -> None:
        entry = self.entries[index]
        entry.plan = None
        entry.plan_cached = False
        entry.analysis_error = None
        self.analysis_selected.discard(index)
        self.write_selected.discard(index)
        self.review_indices.discard(index)

    def _indices_by_path(self) -> dict[Path, int]:
        return {entry.path: index for index, entry in enumerate(self.entries)}


def _toggle(selection: set[int], index: int) -> None:
    if index in selection:
        selection.remove(index)
    else:
        selection.add(index)


def _toggle_all(selection: set[int], eligible: set[int]) -> None:
    if eligible and eligible.issubset(selection):
        selection.difference_update(eligible)
    else:
        selection.update(eligible)


def _restored_status(owned: OwnedValues) -> MetadataStatus:
    """Describe a track after its SetTag metadata was rolled back.

    A restored bundle cannot be shown as up to date without re-inspecting it
    against the current model and config, so anything still carrying SetTag
    metadata is reported as needing reanalysis rather than over-claimed.
    """
    if all(values is None for values in owned.values()):
        return "not_analyzed"
    return "stale"
