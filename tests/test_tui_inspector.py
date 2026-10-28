"""The details panel, rendered without an app."""

from dataclasses import replace
from pathlib import Path

from test_tui import _analysis_batch, _metadata_track, _silent_wav

from settag.policy import Prediction, library_cutoffs
from settag.state import AudioFailure
from settag.tui.entries import TrackEntry
from settag.tui.inspector import display_path, metadata_inspector, review_inspector
from settag.tui.table import RowContext, entry_analysis
from settag.workflow import AnalysisFailure

CONTEXT = RowContext(tasks=("genre",), review_top=5, score_cutoff=0.10)


def test_library_details_say_whether_the_track_is_selected(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    entry = TrackEntry(path=path, metadata=_metadata_track(path))

    selected = metadata_inspector(entry, selected_for_enrichment=True, context=CONTEXT)
    unselected = metadata_inspector(entry, selected_for_enrichment=False, context=CONTEXT)

    assert "Selected for enrichment." in selected
    assert "Not selected for enrichment." in unselected
    assert "Candidates · cutoff ≥ 0.10 · top 5" in selected
    assert path.name in selected


def test_review_details_offer_to_dismiss_a_failed_track(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    failure = AnalysisFailure(path, "DecodeError", "corrupt frame")
    entry = TrackEntry(path=path, analysis_error=failure)

    lines = review_inspector(entry, 0, checked=False, context=CONTEXT)

    assert lines[:2] == ["Enrichment failed", "  DecodeError: corrupt frame"]
    assert any("Press Space to dismiss" in line for line in lines)


def test_review_details_describe_the_planned_write(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _analysis_batch((path,)).planned[0]
    entry = TrackEntry(path=path, metadata=_metadata_track(path), plan=plan)

    lines = review_inspector(entry, 0, checked=True, context=CONTEXT)

    assert lines[-2] == path.name
    assert len(lines) > 3


def test_paths_under_home_are_shortened() -> None:
    assert display_path(Path.home() / "Music") == str(Path("~") / "Music")
    assert display_path(Path.home()) == "~"
    assert display_path(Path("/Volumes/USB")) == "/Volumes/USB"


def test_library_details_explain_a_file_that_keeps_failing(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    failure = AudioFailure("DecodeError: corrupt frame", 3, "2026-10-27T10:00:00Z")
    entry = TrackEntry(path=path, metadata=replace(_metadata_track(path), audio_failure=failure))

    lines = metadata_inspector(entry, selected_for_enrichment=False, context=CONTEXT)

    assert "Audio analysis failed the last 3 times" in lines
    assert "  DecodeError: corrupt frame" in lines
    assert entry_analysis(entry, CONTEXT) == "Keeps failing · 3 tries"


def test_mood_candidates_rank_against_the_library_once_it_is_large_enough(
    tmp_path: Path,
) -> None:
    rows = [
        [Prediction("Deep", 0.6 - n / 100), Prediction("Party", 0.05 + n / 1000)] for n in range(25)
    ]
    context = RowContext(
        tasks=("genre", "mood-theme"),
        review_top=5,
        score_cutoff=0.10,
        library_cutoffs={"mood-theme": library_cutoffs(rows)},
    )

    # Party never reaches 0.10, but it is second on this track and in the library's top 40%.
    assert [p.label for p in context.select_for_review(rows[-1], "mood-theme")] == ["Party"]
    # Genre keeps the score cutoff.
    assert [p.label for p in context.select_for_review(rows[-1], "genre")] == ["Deep"]
    assert context.ranks_against_library("mood-theme")
    assert not RowContext(("mood-theme",), 5, 0.10).ranks_against_library("mood-theme")
