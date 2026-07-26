"""What the app displays: one row's state, and the callables it is given.

Shared by the app and the table renderer, so neither owns the other's types.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from settag.plans import PlannedWrite
from settag.policy import MIN_GENRE_SECONDS, Prediction
from settag.tags import OwnedValues, read_task_provenance
from settag.tasks import AnalysisTask
from settag.workflow import (
    AnalysisBatch,
    AnalysisFailure,
    CancelCallback,
    MetadataBatch,
    MetadataTrack,
    ProgressCallback,
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

STATUS_LABELS = {
    "not_analyzed": "Never analyzed",
    "current": "Up to date",
    "stale": "Reanalyze (model/config changed)",
    "invalid": "Incomplete metadata",
    "sample": f"Sample (shorter than the {MIN_GENRE_SECONDS}s the genre model reads)",
}

TASK_LABELS: dict[AnalysisTask, str] = {
    "genre": "Genre",
    "mood-theme": "Mood/theme",
    "instrument": "Instrument",
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
        if self.metadata is None or self.metadata_error is not None:
            return False
        # A sample is shorter than the genre model's window. Excluding it here
        # rather than at analysis time means it can never be selected, so the
        # analyzer is never handed a track it is guaranteed to reject.
        return not self.metadata.is_sample

    @property
    def needs_analysis(self) -> bool:
        return self.metadata is not None and self.plan is None and self.metadata.needs_analysis

    @property
    def has_changes(self) -> bool:
        return self.plan is not None and bool(self.plan.readable_changes)

    @property
    def has_standard_genre_change(self) -> bool:
        return self.plan is not None and self.plan.standard_genre_change is not None

    @property
    def is_missing_standard_genre(self) -> bool:
        return self.metadata is not None and not self.metadata.genre_state.standard

    @property
    def is_current_unplanned(self) -> bool:
        return self.metadata is not None and self.metadata.status == "current" and self.plan is None


def suggested_label(predictions: Sequence[Prediction]) -> str | None:
    """Return the direct child label without performing taxonomy mapping."""
    if not predictions:
        return None
    return predictions[0].label.rsplit("---", 1)[-1].strip() or None


def latest_analyzed_at(owned: OwnedValues, tasks: Sequence[AnalysisTask]) -> str | None:
    """Newest analysis time across the tasks in play, or the legacy single field."""
    provenance = read_task_provenance(owned)
    values: list[str] = []
    for task in tasks:
        entry = provenance.get(task)
        analyzed_at = entry.get("analyzed_at") if entry is not None else None
        if isinstance(analyzed_at, str) and analyzed_at:
            values.append(analyzed_at)
    if values:
        return max(values)
    legacy = owned.get("SETTAG_ANALYZED_AT")
    return legacy[0] if legacy and len(legacy) == 1 else None
