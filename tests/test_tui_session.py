"""The app's session rules, exercised without Textual.

Each test here pins one rule that the widgets used to enforce piecemeal: a
background result, a write, an undo, and the user's own toggles all go through
``ReviewSession``, so the rule holds no matter which screen triggered it.
"""

from dataclasses import replace
from pathlib import Path

from test_tui import _analysis_batch, _metadata_track, _silent_wav

from settag.journal import WriteRecord
from settag.tui.entries import TrackEntry
from settag.tui.session import ReviewSession, WriteToggle
from settag.workflow import AnalysisFailure


def _session(tmp_path: Path, names: tuple[str, ...] = ("a", "b", "c")) -> ReviewSession:
    session = ReviewSession(("genre",))
    entries = []
    for name in names:
        path = tmp_path / f"{name}.wav"
        _silent_wav(path)
        entries.append(TrackEntry(path=path, metadata=_metadata_track(path)))
    # Loaded out of order on purpose: the session sorts by path.
    session.load(reversed(entries))
    return session


def _plan(session: ReviewSession, index: int):
    return _analysis_batch((session.entries[index].path,)).planned[0]


def test_loading_selects_what_needs_enrichment_and_restores_ready_plans(tmp_path: Path) -> None:
    session = ReviewSession(("genre",))
    ready = tmp_path / "a.wav"
    fresh = tmp_path / "b.wav"
    for path in (ready, fresh):
        _silent_wav(path)
    plan = _analysis_batch((ready,)).planned[0]

    session.load(
        [
            TrackEntry(path=fresh, metadata=_metadata_track(fresh)),
            TrackEntry(path=ready, metadata=_metadata_track(ready), plan=plan),
        ]
    )

    assert [entry.path for entry in session.entries] == [ready, fresh]
    assert session.analysis_selected == {1}
    assert session.review_indices == {0}
    assert session.write_selected == {0}


def test_a_result_moves_a_track_from_the_library_selection_into_review(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.begin_analysis((0, 1))

    session.accept_result(0, _plan(session, 0), None, completed=1)

    assert session.analysis_selected == {1, 2}
    assert session.review_indices == {0}
    assert session.write_selected == {0}
    assert session.analysis_running


def test_a_missing_result_is_recorded_as_a_failure(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.begin_analysis((0,))

    session.accept_result(0, None, None, completed=1)

    assert session.entries[0].analysis_error is not None
    assert "no result" in session.entries[0].analysis_error.message
    assert session.review_indices == {0}
    assert session.write_selected == set()


def test_a_track_queued_for_reanalysis_is_held_until_its_result_arrives(tmp_path: Path) -> None:
    session = _session(tmp_path)
    old = _plan(session, 1)
    session.accept_result(1, old, None, completed=0)
    session.begin_analysis((0, 1))

    assert session.awaiting_new_result(1)
    assert session.toggle_write(1) is WriteToggle.AWAITING_RESULT
    assert session.writable_plans() == ()
    assert session.held_for_reanalysis() == [1]

    session.accept_result(0, _plan(session, 0), None, completed=1)
    assert session.awaiting_new_result(1)
    session.accept_result(1, _plan(session, 1), None, completed=2)

    assert not session.awaiting_new_result(1)
    assert session.held_for_reanalysis() == []
    assert len(session.writable_plans()) == 2


def test_dismissing_a_failure_unblocks_writing_but_keeps_the_error(tmp_path: Path) -> None:
    session = _session(tmp_path)
    failure = AnalysisFailure(session.entries[1].path, "DecodeError", "corrupt frame")
    session.accept_result(0, _plan(session, 0), None, completed=1)
    session.accept_result(1, None, failure, completed=2)
    assert session.review_failures() == (failure,)

    assert session.toggle_write(1) is WriteToggle.DISMISSED

    assert session.review_failures() == ()
    assert session.review_indices == {0}
    assert session.entries[1].analysis_error == failure


def test_toggle_all_skips_tracks_that_cannot_take_part(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.accept_result(0, _plan(session, 0), None, completed=1)
    session.accept_result(1, _plan(session, 1), None, completed=2)
    session.begin_analysis((1,))

    session.toggle_all_write([0, 1])
    assert session.write_selected == {1}
    session.toggle_all_write([0, 1])
    assert session.write_selected == {0, 1}


def test_returning_to_the_library_keeps_unchecked_tracks_unchecked(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.toggle_analysis(1)
    session.accept_result(0, _plan(session, 0), None, completed=1)

    session.return_to_library()

    assert session.analysis_selected == {2}


def test_staging_a_genre_checks_the_track_for_writing(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.accept_result(0, _plan(session, 0), None, completed=1)
    session.toggle_write(0)
    assert session.write_selected == set()

    updated = session.stage_genre(0, ("Techno",))

    assert updated is not None
    assert updated.target_file_genre == ("Techno",)
    assert session.write_selected == {0}
    assert session.stage_genre(1, ("House",)) is None


def test_a_written_track_becomes_a_current_library_entry(tmp_path: Path) -> None:
    session = _session(tmp_path)
    plan = _plan(session, 0)
    session.accept_result(0, plan, None, completed=1)

    session.accept_written([plan])

    entry = session.entries[0]
    assert entry.plan is None
    assert entry.metadata is not None
    assert entry.metadata.status == "current"
    assert 0 not in session.review_indices | session.write_selected | session.analysis_selected


def test_a_reverted_track_leaves_review_and_reports_the_cleared_plan(tmp_path: Path) -> None:
    session = _session(tmp_path)
    plan = _plan(session, 0)
    session.accept_result(0, plan, None, completed=1)
    record = WriteRecord(
        path=session.entries[0].path,
        metadata_format="id3",
        owned_before=dict.fromkeys(plan.desired),
        owned_after=dict(plan.desired),
        standard_before=(),
        standard_after=None,
        sha256_before="0" * 64,
        size_after=1,
        mtime_ns_after=1,
        written_at="2026-09-29T12:00:00Z",
    )
    unknown = replace(record, path=tmp_path / "elsewhere.wav")

    cleared = session.accept_reverted([record, unknown])

    assert cleared == 1
    assert session.entries[0].plan is None
    assert session.entries[0].metadata is not None
    assert session.entries[0].metadata.status == "not_analyzed"
    assert session.review_indices == set()
