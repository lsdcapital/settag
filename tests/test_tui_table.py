"""Row rendering, tested without constructing an app.

These went through the Textual pilot harness before they took their inputs
explicitly, which made a wrong cell expensive to pin down.
"""

import time
from dataclasses import replace
from pathlib import Path

import pytest
from rich.text import Text

from settag.freshness import record_values
from settag.plans import PlannedWrite
from settag.policy import Prediction
from settag.tags import OWNED_DESCRIPTIONS, GenreState
from settag.tui.entries import TrackEntry
from settag.tui.table import (
    GENRE_MATCH_STYLE,
    GENRE_REVIEW_STYLE,
    TRACK_TABLE_COLUMN_BY_KEY,
    TRACK_TABLE_COLUMNS,
    RowContext,
    entry_analysis,
    genre_check,
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
    if status == "current":
        owned["SETTAG_ENRICHMENT"] = record_values(
            audio_complete=True, catalog={"status": "no_match", "checked_at": time.time()}
        )
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

    assert cells == ("", "track.wav", "Techno", "Never", "Not analyzed", "—")


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


def test_enrichment_column_does_not_mislabel_audio_date_as_enrichment_date() -> None:
    path = Path("/music/track.wav")
    states: tuple[tuple[MetadataStatus, str], ...] = (
        ("current", "Current"),
        ("stale", "Reanalyze"),
        ("invalid", "Incomplete"),
    )

    for status, label in states:
        entry = _entry(_track(path, status=status, analyzed_at="2026-07-25T10:00:00Z"))
        assert entry_analysis(entry, CONTEXT) == label


def test_predictions_below_the_cutoff_are_not_suggested() -> None:
    path = Path("/music/track.wav")
    entry = _entry(
        _track(path, status="current", predictions=(Prediction("Electronic---House", 0.04),))
    )

    assert primary_review_predictions(entry, CONTEXT) == []
    assert row_cells(entry, selected=False, context=CONTEXT)[4] == "No suggestion"


def test_a_missing_genre_shows_the_mapped_file_suggestion() -> None:
    path = Path("/music/track.wav")
    entry = _entry(
        _track(path, status="current", predictions=(Prediction("Electronic---Deep House", 0.72),))
    )

    cells = row_cells(entry, selected=False, context=CONTEXT)

    assert cells[4] == "Deep House"
    assert genre_check(entry, CONTEXT).model_genre == "Deep House"
    assert "0.720" not in cells


def test_a_task_not_in_play_contributes_no_suggestion() -> None:
    path = Path("/music/track.wav")
    entry = _entry(
        _track(path, status="current", predictions=(Prediction("Electronic---Deep House", 0.72),))
    )
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


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (("House",), "Differs → Progressive House"),
        (("Progressive House",), "Matches model"),
        ((" progressive house ",), "Matches model"),
        (("Techno",), "Differs → Progressive House"),
        ((), "Missing → Progressive House"),
        (("Progressive House", "Techno"), "Includes suggestion"),
    ],
)
def test_genre_agreement_is_independent_of_current_analysis(
    current: tuple[str, ...], expected: str
) -> None:
    entry = _entry(
        _track(
            Path("/music/track.wav"),
            status="current",
            standard=current,
            predictions=(Prediction("Electronic---Progressive House", 0.72),),
        )
    )
    check = genre_check(entry, CONTEXT)
    assert check.summary == expected
    assert check.model_genre == "Progressive House"
    assert check.suggested_genre == "Progressive House"
    assert entry_analysis(entry, CONTEXT) == "Current"


def test_stale_analysis_does_not_claim_a_genre_match() -> None:
    entry = _entry(
        _track(
            Path("/music/track.wav"),
            status="stale",
            standard=("House",),
            predictions=(Prediction("Electronic---House", 0.72),),
        )
    )
    assert genre_check(entry, CONTEXT).summary == "Reanalyze"


def test_non_genre_tasks_are_never_compared_with_the_file_genre() -> None:
    entry = _entry(_track(Path("/music/track.wav"), status="current"))
    context = RowContext(tasks=("instrument",), review_top=5, score_cutoff=0.10)
    assert genre_check(entry, context).summary == "Not assessed"


def test_a_staged_genre_is_not_reported_as_already_matching_the_file() -> None:
    metadata = _track(Path("/music/track.wav"))
    prediction = Prediction("Electronic---Progressive House", 0.72)
    plan = PlannedWrite(
        path=metadata.path,
        source_sha256="source",
        source_size=1,
        source_mtime_ns=1,
        file_genre=(),
        evidence=(prediction,),
        selected=(prediction,),
        desired=metadata.owned,
        metadata_format="id3",
        owned_changes=(),
        target_file_genre=("Progressive House",),
    )
    entry = TrackEntry(path=metadata.path, metadata=metadata, plan=plan)
    assert row_cells(entry, selected=False, context=CONTEXT)[2] == "None"
    assert genre_check(entry, CONTEXT).summary == "Missing → Progressive House"

    # The check changes to a match only after the stored metadata reflects the write.
    written = replace(
        metadata,
        status="current",
        stored_predictions=(prediction,),
        genre_state=GenreState(standard=("Progressive House",), settag=()),
    )
    assert genre_check(_entry(written), CONTEXT).summary == "Matches model"


@pytest.mark.parametrize(
    ("current", "text", "style"),
    [
        (("House",), "Progressive House", GENRE_REVIEW_STYLE),
        (("Progressive House",), "✓ Progressive House", GENRE_MATCH_STYLE),
        (("Progressive House", "Techno"), "✓ Progressive House", GENRE_MATCH_STYLE),
        (("Techno",), "Progressive House", GENRE_REVIEW_STYLE),
        ((), "Progressive House", GENRE_REVIEW_STYLE),
    ],
)
def test_suggestion_cells_show_mapped_genres_with_review_and_match_cues(
    current: tuple[str, ...], text: str, style: str
) -> None:
    entry = _entry(
        _track(
            Path("/music/track.wav"),
            status="current",
            standard=current,
            predictions=(Prediction("Electronic---Progressive House", 0.72),),
        )
    )
    cell = visible_row_cells(
        entry,
        selected=False,
        context=CONTEXT,
        layout=((TRACK_TABLE_COLUMN_BY_KEY["suggested_genre"], 20),),
    )[0]
    assert isinstance(cell, Text)
    assert cell.plain == text
    assert cell.style == style
