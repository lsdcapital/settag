import wave
from pathlib import Path

from settag.policy import Prediction
from settag.tags import apply_owned_tags, build_owned_values
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
