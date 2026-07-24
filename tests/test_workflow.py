import wave
from pathlib import Path

from settag.policy import Prediction
from settag.records import config_record
from settag.tags import (
    OWNED_DESCRIPTIONS,
    apply_owned_tags,
    build_owned_values,
    build_task_owned_values,
)
from settag.workflow import inspect_paths


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 80)


def _owned_values(
    *,
    model_id: str = "model/v1",
    config_sha256: str = "config/current",
) -> dict[str, list[str] | None]:
    return build_owned_values(
        [Prediction("Electronic---House", 0.72)],
        model_id=model_id,
        analyzed_at="2026-07-23T12:00:00Z",
        config_sha256=config_sha256,
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

    apply_owned_tags(current, _owned_values())
    apply_owned_tags(stale, _owned_values(config_sha256="config/older"))
    invalid_values = _owned_values()
    invalid_values["SETTAG_VERSION"] = None
    apply_owned_tags(invalid, invalid_values)

    progress: list[tuple[int, int, Path]] = []
    batch = inspect_paths(
        (missing, current, stale, invalid),
        expected_model_id="model/v1",
        expected_config_sha256="config/current",
        on_progress=lambda completed, total, path: progress.append(
            (completed, total, path)
        ),
    )
    statuses = {track.path.name: track.status for track in batch.tracks}

    assert batch.failures == ()
    assert statuses == {
        "missing.wav": "not_analyzed",
        "current.wav": "current",
        "stale.wav": "stale",
        "invalid.wav": "invalid",
    }
    assert next(
        track for track in batch.tracks if track.path == current
    ).stored_predictions == (Prediction("Electronic---House", 0.72),)
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
        {field: None for field in OWNED_DESCRIPTIONS},
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
    apply_owned_tags(path, desired)

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
        {field: None for field in OWNED_DESCRIPTIONS},
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
    apply_owned_tags(path, desired)

    track = inspect_paths(
        (path,),
        expected_model_ids={"instrument": "model/instrument/v1"},
        expected_config_sha256=str(config["sha256"]),
    ).tracks[0]

    assert track.status == "invalid"
