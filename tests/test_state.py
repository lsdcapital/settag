import json
import shutil
import sqlite3
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from settag.plans import planned_write_record, stage_file_genre
from settag.policy import Prediction
from settag.records import config_record
from settag.state import WorkbenchStore
from settag.tags import apply_metadata_tags
from settag.tasks import AnalysisTask
from settag.workflow import planned_write_for_track, prepare_track


class FakeAnalyzer:
    spec = SimpleNamespace(id="model/v1")

    def analyze(self, path: Path) -> list[Prediction]:
        return [Prediction("Electronic---House", 0.72)]


class FakeInstrumentAnalyzer:
    backend_version = "test"
    model_manifests: dict[AnalysisTask, dict[str, object]] = {
        "instrument": {
            "schema": "settag.models/v1",
            "id": "model/instrument/v1",
            "files": {},
        }
    }

    def analyze_tasks(
        self,
        path: Path,
    ) -> dict[AnalysisTask, list[Prediction]]:
        return {"instrument": [Prediction("synthesizer", 0.81)]}


def _silent_wav(path: Path, *, seconds: float = 35.0) -> None:
    """Write a silent WAV.

    The default is long enough to clear the genre model's 30s window, so a
    fixture is a track rather than a sample. Pass a shorter value to build one.
    """
    rate = 8_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * int(rate * seconds))


def _plan(path: Path):
    track = prepare_track(
        path,
        analyzer=FakeAnalyzer(),
        top=5,
        threshold=0.10,
    )
    return planned_write_for_track(track)


def _instrument_plan(path: Path):
    track = prepare_track(
        path,
        analyzer=FakeInstrumentAnalyzer(),
        top=5,
        threshold=0.10,
    )
    return planned_write_for_track(track)


def _load_all(store: WorkbenchStore, plan):
    model = plan.desired["SETTAG_MODEL"]
    config = plan.desired["SETTAG_CONFIG_SHA256"]
    assert model is not None
    assert config is not None
    return store.load(
        [plan.path],
        expected_model_ids={"genre": model[0]},
        expected_config_sha256=config[0],
    )


def _load(store: WorkbenchStore, path: Path, plan):
    return _load_all(store, plan)[path.resolve()]


def test_workbench_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _plan(path)
    store = WorkbenchStore(tmp_path / "state.sqlite3")

    store.save(plan)

    entry = _load(store, path, plan)
    assert entry.status == "ready"
    assert entry.reason is None
    assert entry.plan == plan


def test_workbench_classifies_task_aware_effnet_plan(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _instrument_plan(path)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)
    config_sha256 = str(
        config_record(
            top=5,
            threshold=0.10,
            tasks=("instrument",),
        )["sha256"]
    )

    ready = store.load(
        [path],
        expected_model_ids={"instrument": "model/instrument/v1"},
        expected_config_sha256=config_sha256,
    )[path.resolve()]
    combined_config = config_record(
        top=5,
        threshold=0.10,
        tasks=("genre", "mood-theme", "instrument"),
    )
    ready_after_task_selection_change = store.load(
        [path],
        expected_model_ids={"instrument": "model/instrument/v1"},
        expected_config_sha256=str(combined_config["sha256"]),
        expected_config=combined_config,
    )[path.resolve()]
    changed_tasks = store.load(
        [path],
        expected_model_ids={
            "genre": "model/genre/v1",
            "instrument": "model/instrument/v1",
        },
        expected_config_sha256=config_sha256,
    )[path.resolve()]

    assert ready.status == "ready"
    assert ready_after_task_selection_change.status == "ready"
    assert changed_tasks.status == "stale"
    assert changed_tasks.reason == "analysis tasks changed"


def test_workbench_marks_changed_source_stale(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _plan(path)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)
    # Re-recorded at a different length, so the samples themselves differ.
    _silent_wav(path, seconds=36.0)

    entry = _load(store, path, plan)

    assert entry.status == "stale"
    assert entry.reason == "source file changed"


def test_workbench_survives_a_tag_write_to_the_source(tmp_path: Path) -> None:
    """A tag write must not invalidate an analysis of unchanged audio.

    This is the case that used to strand plans: SetTag's own output changes the
    file's size, mtime, and whole-file digest without touching a sample, so a
    cache keyed on those discarded work it should have kept.
    """
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _plan(path)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)

    before = path.stat()
    apply_metadata_tags(path, plan.desired)
    assert path.stat().st_size != before.st_size

    entry = _load(store, path, plan)

    assert entry.status == "ready"
    assert entry.reason is None


@pytest.mark.parametrize(
    ("model", "config", "reason"),
    [
        ("model/v2", None, "analysis model changed"),
        (None, "config/new", "evidence settings changed"),
    ],
)
def test_workbench_marks_model_or_config_change_stale(
    tmp_path: Path,
    model: str | None,
    config: str | None,
    reason: str,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _plan(path)
    stored_model = plan.desired["SETTAG_MODEL"]
    stored_config = plan.desired["SETTAG_CONFIG_SHA256"]
    assert stored_model is not None
    assert stored_config is not None
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)

    entry = store.load(
        [path],
        expected_model_ids={"genre": model or stored_model[0]},
        expected_config_sha256=config or stored_config[0],
    )[path.resolve()]

    assert entry.status == "stale"
    assert entry.reason == reason


def test_workbench_remains_ready_when_only_review_policy_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _plan(path)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)
    changed_policy = config_record(top=1, threshold=0.95)

    entry = store.load(
        [path],
        expected_model_ids={"genre": "model/v1"},
        expected_config_sha256=str(changed_policy["sha256"]),
    )[path.resolve()]

    assert entry.status == "ready"


def test_workbench_preserves_staged_standard_genre(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = stage_file_genre(_plan(path), ("House",))
    store = WorkbenchStore(tmp_path / "state.sqlite3")

    store.save(plan)

    assert _load(store, path, plan).plan.target_file_genre == ("House",)


def test_workbench_delete_removes_plan(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _plan(path)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)

    store.delete([path])

    model = plan.desired["SETTAG_MODEL"]
    config = plan.desired["SETTAG_CONFIG_SHA256"]
    assert model is not None
    assert config is not None
    assert (
        store.load(
            [path],
            expected_model_ids={"genre": model[0]},
            expected_config_sha256=config[0],
        )
        == {}
    )


def _replace_stored_json(store: WorkbenchStore, path: Path, record: object) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workbench_plans SET plan_json = ? WHERE path = ?",
            (json.dumps(record), str(path.resolve())),
        )


def _stored_paths(store: WorkbenchStore) -> list[str]:
    with sqlite3.connect(store.path) as connection:
        return [str(row[0]) for row in connection.execute("SELECT path FROM workbench_plans")]


def test_workbench_discards_corrupt_cached_record(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _plan(path)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)
    _replace_stored_json(store, path, {"schema": "broken"})

    assert _load_all(store, plan) == {}
    assert _stored_paths(store) == []


def test_workbench_discards_superseded_plan_schema(tmp_path: Path) -> None:
    """An upgrade that bumps ``PLAN_SCHEMA`` must not brick a warm cache."""
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _plan(path)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)
    superseded = planned_write_record(plan) | {"schema": "settag.plan/v3"}
    _replace_stored_json(store, path, superseded)

    assert _load_all(store, plan) == {}
    assert _stored_paths(store) == []


def test_workbench_keeps_readable_records_beside_a_discarded_one(tmp_path: Path) -> None:
    readable = tmp_path / "readable.wav"
    unreadable = tmp_path / "unreadable.wav"
    _silent_wav(readable)
    _silent_wav(unreadable)
    readable_plan = _plan(readable)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(readable_plan)
    store.save(_plan(unreadable))
    _replace_stored_json(store, unreadable, {"schema": "broken"})

    model = readable_plan.desired["SETTAG_MODEL"]
    config = readable_plan.desired["SETTAG_CONFIG_SHA256"]
    assert model is not None
    assert config is not None
    entries = store.load(
        [readable, unreadable],
        expected_model_ids={"genre": model[0]},
        expected_config_sha256=config[0],
    )

    assert set(entries) == {readable.resolve()}
    assert entries[readable.resolve()].status == "ready"
    assert _stored_paths(store) == [str(readable.resolve())]


def test_workbench_follows_a_renamed_source(tmp_path: Path) -> None:
    """A rename must not strand an analysis.

    Renaming is routine for a DJ library, and the plan is keyed by path, so
    before the audio digest existed this reported the track as missing while it
    sat untouched in the same directory.
    """
    original = tmp_path / "before.wav"
    _silent_wav(original)
    plan = _plan(original)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)

    renamed = tmp_path / "after.wav"
    original.rename(renamed)

    moved = store.relocate([renamed])

    assert moved == {original.resolve(): renamed.resolve()}
    assert _stored_paths(store) == [str(renamed.resolve())]

    model = plan.desired["SETTAG_MODEL"]
    config = plan.desired["SETTAG_CONFIG_SHA256"]
    assert model is not None
    assert config is not None
    entries = store.load(
        [renamed],
        expected_model_ids={"genre": model[0]},
        expected_config_sha256=config[0],
    )
    assert entries[renamed.resolve()].status == "ready"


def test_workbench_follows_a_source_renamed_and_retagged(tmp_path: Path) -> None:
    """Identity has to survive a rename and a tag write happening together."""
    original = tmp_path / "before.wav"
    _silent_wav(original)
    plan = _plan(original)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)

    apply_metadata_tags(original, plan.desired)
    renamed = tmp_path / "after.wav"
    original.rename(renamed)

    assert store.relocate([renamed]) == {original.resolve(): renamed.resolve()}


def test_workbench_leaves_an_ambiguous_move_alone(tmp_path: Path) -> None:
    """Two identical files are not evidence of where one plan should go.

    Guessing would attach an analysis to the wrong track, which is worse than
    reporting the file as missing and asking for a rescan.
    """
    original = tmp_path / "before.wav"
    _silent_wav(original)
    plan = _plan(original)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)

    first = tmp_path / "copy-one.wav"
    second = tmp_path / "copy-two.wav"
    original.rename(first)
    shutil.copy2(first, second)

    assert store.relocate([first, second]) == {}
    assert _stored_paths(store) == [str(original.resolve())]


def test_workbench_does_not_relocate_onto_a_different_track(tmp_path: Path) -> None:
    original = tmp_path / "before.wav"
    other = tmp_path / "other.wav"
    _silent_wav(original)
    _silent_wav(other, seconds=36.0)
    plan = _plan(original)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)
    original.unlink()

    assert store.relocate([other]) == {}
    assert _stored_paths(store) == [str(original.resolve())]


def test_workbench_relocates_a_schema_one_row(tmp_path: Path) -> None:
    """A cache written before the audio digest still follows a rename.

    Those rows are analyses the user already paid for, so the upgrade path has
    to work on them rather than only on freshly written ones.
    """
    original = tmp_path / "before.wav"
    _silent_wav(original)
    plan = _plan(original)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)

    # Reduce the row to what schema 1 stored: no identity columns, and a plan
    # body in the v4 format that predates the audio digest.
    record = json.loads(json.dumps(planned_write_record(plan)))
    record["source"].pop("audio_sha256")
    record["schema"] = "settag.plan/v4"
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workbench_plans SET plan_json = ?, audio_sha256 = NULL, source_size = NULL",
            (json.dumps(record),),
        )

    renamed = tmp_path / "after.wav"
    original.rename(renamed)

    assert store.relocate([renamed]) == {original.resolve(): renamed.resolve()}
