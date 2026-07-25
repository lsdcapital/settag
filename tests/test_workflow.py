import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from settag.journal import BatchRecorder, WriteJournal
from settag.plans import PlannedWrite, stage_file_genre
from settag.policy import Prediction
from settag.records import config_record
from settag.tags import (
    OWNED_DESCRIPTIONS,
    apply_metadata_tags,
    build_task_owned_values,
    read_genre_state,
    read_owned_values,
)
from settag.tasks import AnalysisTask
from settag.workflow import (
    apply_prepared,
    apply_undo,
    inspect_paths,
    planned_write_for_track,
    preflight_plan,
    preflight_undo,
    prepare_track,
    summarize_writes,
)


class FakeAnalyzer:
    spec = SimpleNamespace(id="model/v1")

    def analyze(self, path: Path) -> list[Prediction]:
        return [Prediction("Electronic---House", 0.72)]


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 80)


def _plan(path: Path) -> PlannedWrite:
    return planned_write_for_track(
        prepare_track(path, analyzer=FakeAnalyzer(), top=5, threshold=0.10)
    )


def _owned_values(
    *,
    model_id: str = "model/v1",
    config_sha256: str = "config/current",
) -> dict[str, list[str] | None]:
    return build_task_owned_values(
        dict.fromkeys(OWNED_DESCRIPTIONS),
        {"genre": [Prediction("Electronic---House", 0.72)]},
        {
            "genre": {
                "model": {"schema": "settag.models/v1", "id": model_id, "files": {}},
                "analyzed_at": "2026-07-23T12:00:00Z",
                "config": {"sha256": config_sha256},
            }
        },
    )


def test_metadata_inspection_classifies_current_stale_missing_and_invalid(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.wav"
    current = tmp_path / "current.wav"
    stale = tmp_path / "stale.wav"
    invalid = tmp_path / "invalid.wav"
    for path in (missing, current, stale, invalid):
        _silent_wav(path)

    apply_metadata_tags(current, _owned_values())
    apply_metadata_tags(stale, _owned_values(config_sha256="config/older"))
    invalid_values = _owned_values()
    invalid_values["SETTAG_VERSION"] = None
    apply_metadata_tags(invalid, invalid_values)

    progress: list[tuple[int, int, Path]] = []
    batch = inspect_paths(
        (missing, current, stale, invalid),
        expected_model_ids={"genre": "model/v1"},
        expected_config_sha256="config/current",
        on_progress=lambda completed, total, path: progress.append((completed, total, path)),
    )
    statuses = {track.path.name: track.status for track in batch.tracks}

    assert batch.failures == ()
    assert statuses == {
        "missing.wav": "not_analyzed",
        "current.wav": "current",
        "stale.wav": "stale",
        "invalid.wav": "invalid",
    }
    assert next(track for track in batch.tracks if track.path == current).stored_predictions == (
        Prediction("Electronic---House", 0.72),
    )
    assert progress == [
        (1, 4, missing),
        (2, 4, current),
        (3, 4, stale),
        (4, 4, invalid),
    ]


def test_metadata_inspection_is_task_aware_for_effnet_only_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "instrument.wav"
    _silent_wav(path)
    recorded_config = config_record(
        top=5,
        threshold=0.10,
        tasks=("genre", "mood-theme", "instrument"),
    )
    instrument_config = config_record(
        top=5,
        threshold=0.10,
        tasks=("instrument",),
    )
    desired = build_task_owned_values(
        dict.fromkeys(OWNED_DESCRIPTIONS),
        {"instrument": [Prediction("synthesizer", 0.81)]},
        {
            "instrument": {
                "model": {
                    "schema": "settag.models/v1",
                    "id": "model/instrument/v1",
                    "files": {},
                },
                "analyzed_at": "2026-07-24T12:00:00Z",
                "config": recorded_config,
            }
        },
    )
    apply_metadata_tags(path, desired)

    current = inspect_paths(
        (path,),
        expected_model_ids={"instrument": "model/instrument/v1"},
        expected_config_sha256=str(instrument_config["sha256"]),
        expected_config=instrument_config,
    ).tracks[0]
    missing_genre = inspect_paths(
        (path,),
        expected_model_ids={
            "genre": "model/genre/v1",
            "instrument": "model/instrument/v1",
        },
        expected_config_sha256=str(instrument_config["sha256"]),
        expected_config=instrument_config,
    ).tracks[0]

    assert current.status == "current"
    assert current.analyzed_at == "2026-07-24T12:00:00Z"
    assert current.stored_predictions == ()
    assert missing_genre.status == "stale"


def test_metadata_inspection_rejects_invalid_selected_task_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "instrument.wav"
    _silent_wav(path)
    config = config_record(
        top=5,
        threshold=0.10,
        tasks=("instrument",),
    )
    desired = build_task_owned_values(
        dict.fromkeys(OWNED_DESCRIPTIONS),
        {"instrument": [Prediction("synthesizer", 0.81)]},
        {
            "instrument": {
                "model": {
                    "schema": "settag.models/v1",
                    "id": "model/instrument/v1",
                    "files": {},
                },
                "analyzed_at": "2026-07-24T12:00:00Z",
                "config": config,
            }
        },
    )
    desired["SETTAG_INSTRUMENT_SCORES"] = ["not-json"]
    apply_metadata_tags(path, desired)

    track = inspect_paths(
        (path,),
        expected_model_ids={"instrument": "model/instrument/v1"},
        expected_config_sha256=str(config["sha256"]),
    ).tracks[0]

    assert track.status == "invalid"


def test_undo_restores_the_metadata_a_write_replaced(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    owned_before = read_owned_values(path)
    genre_before = read_genre_state(path).standard

    journal = WriteJournal(tmp_path / "journal.sqlite3")
    recorder = BatchRecorder(journal)
    plan = stage_file_genre(_plan(path), ("House",))
    apply_prepared(preflight_plan([plan]), on_write=recorder)
    assert read_genre_state(path).standard == ("House",)

    batch = journal.batch(recorder.batch_id)
    assert batch is not None
    preflight = preflight_undo(batch.entries)
    restored = apply_undo(preflight.restorable)

    assert preflight.blocked == ()
    assert restored == 1
    assert read_owned_values(path) == owned_before
    assert read_genre_state(path).standard == genre_before


def test_undo_restores_the_older_settag_bundle_a_rewrite_replaced(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    apply_metadata_tags(path, _owned_values(model_id="model/older"))
    owned_before = read_owned_values(path)

    journal = WriteJournal(tmp_path / "journal.sqlite3")
    recorder = BatchRecorder(journal)
    apply_prepared(preflight_plan([_plan(path)]), on_write=recorder)
    assert read_owned_values(path)["SETTAG_MODEL"] == ["model/v1"]

    batch = journal.batch(recorder.batch_id)
    assert batch is not None
    apply_undo(preflight_undo(batch.entries).restorable)

    assert read_owned_values(path) == owned_before


def test_a_write_is_journaled_once_per_written_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-up-to-date track is skipped, so it earns no journal entry.

    The analysis timestamp is pinned because it has one-second resolution: left
    real, whether the second analysis counts as a change depends on which side
    of a second boundary the two calls land on.
    """
    monkeypatch.setattr("settag.workflow.utc_now", lambda: "2026-07-25T10:00:00Z")
    written = tmp_path / "written.wav"
    unchanged = tmp_path / "unchanged.wav"
    for path in (written, unchanged):
        _silent_wav(path)

    journal = WriteJournal(tmp_path / "journal.sqlite3")
    first = BatchRecorder(journal)
    apply_prepared(preflight_plan([_plan(unchanged)]), on_write=first)

    second = BatchRecorder(journal)
    plans = [_plan(written), _plan(unchanged)]
    apply_prepared(preflight_plan(plans), on_write=second)

    batch = journal.batch(second.batch_id)
    assert batch is not None
    assert [entry.path for entry in batch.entries] == [written]


def test_a_failing_recorder_never_fails_the_write(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)

    def explode(_entry: object) -> None:
        raise RuntimeError("journal is unavailable")

    written = apply_prepared(preflight_plan([_plan(path)]), on_write=explode)

    assert written == 1
    assert read_owned_values(path)["SETTAG_GENRE"] == ["Electronic---House"]


def test_undo_skips_a_file_that_changed_after_the_write(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    recorder = BatchRecorder(journal)
    apply_prepared(preflight_plan([_plan(path)]), on_write=recorder)

    apply_metadata_tags(path, _owned_values(model_id="model/elsewhere"))

    batch = journal.batch(recorder.batch_id)
    assert batch is not None
    preflight = preflight_undo(batch.entries)

    assert preflight.restorable == ()
    assert [blocked.reason for blocked in preflight.blocked] == [
        "file changed after SetTag wrote it"
    ]


def test_force_restores_a_file_that_changed_after_the_write(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    owned_before = read_owned_values(path)
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    recorder = BatchRecorder(journal)
    apply_prepared(preflight_plan([_plan(path)]), on_write=recorder)

    apply_metadata_tags(path, _owned_values(model_id="model/elsewhere"))

    batch = journal.batch(recorder.batch_id)
    assert batch is not None
    preflight = preflight_undo(batch.entries, force=True)
    apply_undo(preflight.restorable)

    assert preflight.blocked == ()
    assert read_owned_values(path) == owned_before


def test_undo_reports_a_missing_file_instead_of_failing(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    recorder = BatchRecorder(journal)
    apply_prepared(preflight_plan([_plan(path)]), on_write=recorder)
    path.unlink()

    batch = journal.batch(recorder.batch_id)
    assert batch is not None
    preflight = preflight_undo(batch.entries, force=True)

    assert preflight.restorable == ()
    assert [blocked.reason for blocked in preflight.blocked] == ["file is missing"]


class FakeMultiTaskAnalyzer:
    model_manifests: dict[AnalysisTask, dict[str, object]] = {
        task: {"schema": "settag.models/v1", "id": f"model/{task}/v1", "files": {}}
        for task in ("genre", "instrument")
    }

    def analyze_tasks(self, path: Path) -> dict[AnalysisTask, list[Prediction]]:
        return {
            "genre": [Prediction("Electronic---House", 0.72)],
            "instrument": [
                Prediction("synthesizer", 0.81),
                Prediction("drum machine", 0.44),
            ],
        }


def test_summary_counts_evidence_from_every_task_not_just_genre(tmp_path: Path) -> None:
    """Regression: the CLI counted genre evidence while the TUI counted all tasks.

    One batch reported two different numbers depending on which UI asked.
    """
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = planned_write_for_track(
        prepare_track(path, analyzer=FakeMultiTaskAnalyzer(), top=5, threshold=0.10)
    )

    summary = summarize_writes(preflight_plan([plan]))

    assert len(plan.evidence) == 1
    assert summary.evidence_scores == 3


def test_summary_counts_writes_bundles_and_staged_genre_edits(tmp_path: Path) -> None:
    changed = tmp_path / "changed.wav"
    staged = tmp_path / "staged.wav"
    for path in (changed, staged):
        _silent_wav(path)

    prepared = preflight_plan([_plan(changed), stage_file_genre(_plan(staged), ("House",))])
    summary = summarize_writes(prepared)

    assert summary.track_count == 2
    assert summary.write_count == 2
    assert summary.bundle_changes == 2
    assert summary.standard_genre_edits == 1
    assert summary.empty_file_genres == 2


def test_undo_preflight_counts_match_its_contents(tmp_path: Path) -> None:
    present = tmp_path / "present.wav"
    removed = tmp_path / "removed.wav"
    for path in (present, removed):
        _silent_wav(path)

    journal = WriteJournal(tmp_path / "journal.sqlite3")
    recorder = BatchRecorder(journal)
    plans = [stage_file_genre(_plan(present), ("House",)), _plan(removed)]
    apply_prepared(preflight_plan(plans), on_write=recorder)
    removed.unlink()

    batch = journal.batch(recorder.batch_id)
    assert batch is not None
    preflight = preflight_undo(batch.entries)

    assert preflight.restore_count == 1
    assert preflight.blocked_count == 1
    assert preflight.standard_genre_edits == 1
