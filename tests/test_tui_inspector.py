"""The details panel, rendered without an app."""

from pathlib import Path

from test_tui import _analysis_batch, _metadata_track, _silent_wav

from settag.tui.entries import TrackEntry
from settag.tui.inspector import display_path, metadata_inspector, review_inspector
from settag.tui.table import RowContext
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
