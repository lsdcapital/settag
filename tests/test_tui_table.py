"""Row rendering, tested without constructing an app.

These went through the Textual pilot harness before they took their inputs
explicitly, which made a wrong cell expensive to pin down.
"""

from pathlib import Path

from settag.plans import PlannedWrite
from settag.policy import Prediction
from settag.tags import OWNED_DESCRIPTIONS, GenreState
from settag.tui.entries import TrackEntry
from settag.tui.table import (
    TRACK_TABLE_COLUMNS,
    RowContext,
    entry_analysis,
    primary_review_predictions,
    row_cells,
    visible_row_cells,
)
from settag.workflow import AnalysisFailure, MetadataStatus, MetadataTrack

CONTEXT = RowContext(tasks=("genre",), review_top=5, score_cutoff=0.10)


def _track(
    path: Path,
    *,
    status: MetadataStatus = "not_analyzed",
    standard: tuple[str, ...] = (),
    predictions: tuple[Prediction, ...] = (),
    analyzed_at: str | None = None,
) -> MetadataTrack:
    owned: dict[str, list[str] | None] = dict.fromkeys(OWNED_DESCRIPTIONS)
    return MetadataTrack(
        path=path,
        genre_state=GenreState(standard=standard, settag=()),
        owned=owned,
        stored_predictions=predictions,
        status=status,
        analyzed_at=analyzed_at,
    )


def _entry(track: MetadataTrack) -> TrackEntry:
    return TrackEntry(path=track.path, metadata=track)


def test_row_shows_the_existing_genre_and_no_analysis_yet() -> None:
    entry = _entry(_track(Path("/music/track.wav"), standard=("Techno",)))

    cells = row_cells(entry, selected=False, context=CONTEXT)

    assert cells == ("", "track.wav", "Techno", "Never", "—", "—")


def test_row_marks_selection() -> None:
    entry = _entry(_track(Path("/music/track.wav")))

    assert row_cells(entry, selected=True, context=CONTEXT)[0] == "✓"
    assert row_cells(entry, selected=False, context=CONTEXT)[0] == ""


def test_row_reports_an_absent_genre_rather_than_an_empty_cell() -> None:
    entry = _entry(_track(Path("/music/track.wav")))

    assert row_cells(entry, selected=False, context=CONTEXT)[2] == "None"


def test_analysis_column_leads_with_the_error_that_matters() -> None:
    path = Path("/music/track.wav")
    failure = AnalysisFailure(path=path, error_type="RuntimeError", message="boom")
    analysis_failed = TrackEntry(path=path, metadata=_track(path), analysis_error=failure)
    metadata_failed = TrackEntry(path=path, metadata=None, metadata_error=failure)

    assert entry_analysis(analysis_failed, CONTEXT) == "Analysis error"
    assert entry_analysis(metadata_failed, CONTEXT) == "Metadata error"


def test_analysis_column_names_each_metadata_state_with_its_date() -> None:
    path = Path("/music/track.wav")
    states: tuple[tuple[MetadataStatus, str], ...] = (
        ("current", "Up to date"),
        ("stale", "Reanalyze"),
        ("invalid", "Incomplete"),
    )

    for status, label in states:
        entry = _entry(_track(path, status=status, analyzed_at="2026-07-25T10:00:00Z"))
        assert entry_analysis(entry, CONTEXT) == f"{label} · 2026-07-25"


def test_predictions_below_the_cutoff_are_not_suggested() -> None:
    path = Path("/music/track.wav")
    entry = _entry(_track(path, predictions=(Prediction("Electronic---House", 0.04),)))

    assert primary_review_predictions(entry, CONTEXT) == []
    assert row_cells(entry, selected=False, context=CONTEXT)[4] == "—"


def test_a_suggestion_shows_its_child_label_without_a_raw_score() -> None:
    path = Path("/music/track.wav")
    entry = _entry(_track(path, predictions=(Prediction("Electronic---Deep House", 0.72),)))

    cells = row_cells(entry, selected=False, context=CONTEXT)

    assert cells[4] == "Deep House"
    assert "0.720" not in cells


def test_a_task_not_in_play_contributes_no_suggestion() -> None:
    path = Path("/music/track.wav")
    entry = _entry(_track(path, predictions=(Prediction("Electronic---Deep House", 0.72),)))
    instrument_only = RowContext(tasks=("instrument",), review_top=5, score_cutoff=0.10)

    assert primary_review_predictions(entry, instrument_only) == []


def test_write_plan_column_names_a_timestamp_only_reanalysis_as_refresh() -> None:
    track = _track(Path("/music/track.wav"), standard=("Afro House",))
    plan = PlannedWrite(
        path=track.path,
        source_sha256="source",
        source_size=1,
        source_mtime_ns=1,
        file_genre=track.genre_state.standard,
        evidence=(),
        selected=(),
        desired=track.owned,
        metadata_format="id3",
        owned_changes=(
            "Analysis time: 2026-07-28 → 2026-07-29",
            "Task provenance: previous → current",
        ),
    )
    entry = TrackEntry(path=track.path, metadata=track, plan=plan)

    assert row_cells(entry, selected=True, context=CONTEXT)[5] == "Refresh"


def test_visible_cells_follow_the_columns_that_fit() -> None:
    entry = _entry(_track(Path("/music/track.wav"), standard=("Techno",)))
    narrow = tuple((column, 10) for column in TRACK_TABLE_COLUMNS[:3])

    cells = visible_row_cells(entry, selected=True, context=CONTEXT, layout=narrow)

    assert cells == ("✓", "track.wav", "Techno")
