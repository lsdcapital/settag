import sqlite3
from pathlib import Path

import pytest

from settag.journal import (
    BatchRecorder,
    JournalError,
    WriteJournal,
    WriteRecord,
    new_batch_id,
)
from settag.tags import OWNED_DESCRIPTIONS


def _empty_owned() -> dict[str, list[str] | None]:
    return dict.fromkeys(OWNED_DESCRIPTIONS)


def _owned(genre: str = "Electronic---House") -> dict[str, list[str] | None]:
    owned = _empty_owned()
    owned["SETTAG_GENRE"] = [genre]
    owned["SETTAG_VERSION"] = ["0.1.0"]
    return owned


def _record(
    path: Path,
    *,
    written_at: str = "2026-07-25T10:00:00Z",
    standard_after: tuple[str, ...] | None = ("House",),
) -> WriteRecord:
    return WriteRecord(
        path=path,
        metadata_format="id3",
        owned_before=_empty_owned(),
        owned_after=_owned(),
        standard_before=(),
        standard_after=standard_after,
        sha256_before="a" * 64,
        size_after=1234,
        mtime_ns_after=5678,
        written_at=written_at,
    )


def test_records_round_trip_through_the_database(tmp_path: Path) -> None:
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    entry = _record(tmp_path / "track.wav")

    journal.record("batch-1", entry)
    batch = journal.batch("batch-1")

    assert batch is not None
    assert batch.batch_id == "batch-1"
    assert batch.started_at == entry.written_at
    assert batch.reverted_at is None
    assert batch.entries == (entry,)


def test_batch_groups_every_file_from_one_apply(tmp_path: Path) -> None:
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    first = _record(tmp_path / "a.wav")
    second = _record(tmp_path / "b.wav", standard_after=None)

    journal.record("batch-1", first)
    journal.record("batch-1", second)
    batch = journal.batch("batch-1")

    assert batch is not None
    assert batch.track_count == 2
    assert batch.standard_genre_count == 1
    assert batch.summary == "2 tracks, 1 file genre edit"


def test_recent_returns_newest_batches_first_and_latest_matches(tmp_path: Path) -> None:
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    journal.record("older", _record(tmp_path / "a.wav", written_at="2026-07-24T10:00:00Z"))
    journal.record("newer", _record(tmp_path / "b.wav", written_at="2026-07-25T10:00:00Z"))

    recent = journal.recent()
    latest = journal.latest()

    assert [batch.batch_id for batch in recent] == ["newer", "older"]
    assert latest is not None
    assert latest.batch_id == "newer"


def test_mark_reverted_is_visible_in_the_summary(tmp_path: Path) -> None:
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    journal.record("batch-1", _record(tmp_path / "track.wav"))

    journal.mark_reverted("batch-1")
    batch = journal.batch("batch-1")

    assert batch is not None
    assert batch.reverted_at is not None
    assert "already reverted" in batch.summary


def test_prune_drops_batches_past_the_retention_window(tmp_path: Path) -> None:
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    journal.record("old", _record(tmp_path / "a.wav", written_at="2020-01-01T00:00:00Z"))

    # Only one batch is recorded here: starting a second batch would enforce retention on
    # its own, which is covered by the test below.
    removed = journal.prune(days=90)

    assert removed == 1
    assert journal.batch("old") is None


def test_starting_a_batch_enforces_the_retention_window(tmp_path: Path) -> None:
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    journal.record("old", _record(tmp_path / "a.wav", written_at="2020-01-01T00:00:00Z"))
    journal.record("new", _record(tmp_path / "b.wav", written_at="2026-07-25T10:00:00Z"))

    assert journal.batch("old") is None
    assert journal.batch("new") is not None


def test_retention_runs_once_per_batch_not_once_per_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    calls = 0
    original = WriteJournal.prune

    def counted(self: WriteJournal, days: int = 90, *, keep: str | None = None) -> int:
        nonlocal calls
        calls += 1
        return original(self, days, keep=keep)

    monkeypatch.setattr(WriteJournal, "prune", counted)
    for index in range(3):
        journal.record("batch", _record(tmp_path / f"{index}.wav"))

    assert calls == 1


def test_a_failing_prune_never_loses_the_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention is housekeeping: it must not turn a committed write into an error."""
    journal = WriteJournal(tmp_path / "journal.sqlite3")

    def explode(self: WriteJournal, days: int = 90, *, keep: str | None = None) -> int:
        raise sqlite3.OperationalError("disk is unhappy")

    monkeypatch.setattr(WriteJournal, "prune", explode)
    journal.record("batch", _record(tmp_path / "a.wav"))

    assert journal.batch("batch") is not None


def test_a_newer_database_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(JournalError, match="newer than"):
        WriteJournal(path).recent()


def test_unknown_batch_and_empty_journal_report_nothing(tmp_path: Path) -> None:
    journal = WriteJournal(tmp_path / "journal.sqlite3")

    assert journal.batch("nope") is None
    assert journal.latest() is None
    assert journal.recent() == ()


def test_readable_changes_describe_both_halves_of_the_write(tmp_path: Path) -> None:
    entry = _record(tmp_path / "track.wav")

    lines = entry.readable_changes

    assert "Genre labels: 0 → 1" in lines
    assert "File genre: None → House" in lines


def test_readable_changes_omit_an_untouched_file_genre(tmp_path: Path) -> None:
    entry = _record(tmp_path / "track.wav", standard_after=None)

    assert not any(line.startswith("File genre") for line in entry.readable_changes)


def test_recorder_absorbs_storage_failures_and_reports_them(tmp_path: Path) -> None:
    unusable = tmp_path / "not-a-directory"
    unusable.write_text("", encoding="utf-8")
    recorder = BatchRecorder(WriteJournal(unusable / "journal.sqlite3"))

    recorder(_record(tmp_path / "track.wav"))

    assert recorder.recorded == 0
    assert recorder.error is not None
    assert "cannot be undone" in recorder.error


def test_recorder_reports_no_error_when_every_write_is_journaled(tmp_path: Path) -> None:
    recorder = BatchRecorder(WriteJournal(tmp_path / "journal.sqlite3"))

    recorder(_record(tmp_path / "track.wav"))

    assert recorder.recorded == 1
    assert recorder.error is None


def test_batch_ids_are_unique(tmp_path: Path) -> None:
    assert new_batch_id() != new_batch_id()


def test_recorder_absorbs_a_failing_insert_and_reports_it(tmp_path: Path) -> None:
    """A storage error on the insert itself, not just on opening the database.

    This used to escape as a raw sqlite3 error, which the recorder did not catch
    and the write loop silently dropped, so a write that could not be undone
    was reported as a clean success.
    """
    path = tmp_path / "journal.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE write_entries (id INTEGER PRIMARY KEY, batch_id TEXT)")
        connection.execute("PRAGMA user_version = 1")
    recorder = BatchRecorder(WriteJournal(path))

    recorder(_record(tmp_path / "track.wav"))

    assert recorder.recorded == 0
    assert recorder.error is not None
    assert "cannot be undone" in recorder.error
    assert "no column named" in recorder.error
