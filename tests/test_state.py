import json
import sqlite3
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from settag.plans import stage_file_genre
from settag.policy import Prediction
from settag.records import config_record
from settag.state import WorkbenchError, WorkbenchStore
from settag.workflow import planned_write_for_track, prepare_track


class FakeAnalyzer:
    spec = SimpleNamespace(id="model/v1")

    def analyze(self, path: Path) -> list[Prediction]:
        return [Prediction("Electronic---House", 0.72)]


class FakeInstrumentAnalyzer:
    backend_version = "test"
    model_manifests = {
        "instrument": {
            "schema": "settag.models/v1",
            "id": "model/instrument/v1",
            "files": {},
        }
    }

    def analyze_tasks(self, path: Path) -> dict[str, list[Prediction]]:
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
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
    )
    return planned_write_for_track(track)


def _instrument_plan(path: Path):
    track = prepare_track(
        path,
        analyzer=FakeInstrumentAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
    )
    return planned_write_for_track(track)


def _load(store: WorkbenchStore, path: Path, plan):
    model = plan.desired["SETTAG_MODEL"]
    config = plan.desired["SETTAG_CONFIG_SHA256"]
    assert model is not None
    assert config is not None
    return store.load(
        [path],
        expected_model_id=model[0],
        expected_config_sha256=config[0],
    )[path.resolve()]


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
        expected_model_id=model or stored_model[0],
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
        expected_model_id="model/v1",
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
    assert store.load(
        [path],
        expected_model_id=model[0],
        expected_config_sha256=config[0],
    ) == {}


def test_workbench_reports_corrupt_cached_record(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _plan(path)
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(plan)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workbench_plans SET plan_json = ? WHERE path = ?",
            (json.dumps({"schema": "broken"}), str(path.resolve())),
        )

    with pytest.raises(WorkbenchError, match="Invalid cached analysis"):
        _load(store, path, plan)
