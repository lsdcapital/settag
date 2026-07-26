import json
import sqlite3
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from settag.plans import planned_write_record, stage_file_genre
from settag.policy import Prediction
from settag.records import config_record
from settag.state import WorkbenchStore
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


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 80)


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
    path.write_bytes(path.read_bytes() + b"\0")

    entry = _load(store, path, plan)

    assert entry.status == "stale"
    assert entry.reason == "source file changed"


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
