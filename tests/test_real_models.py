"""Smoke tests against the real Essentia models.

Deselected by default (see ``addopts`` in pyproject.toml) because loading MAEST
takes seconds and CI has no model cache. Run them with::

    uv run pytest -m models

They skip rather than fail when the models are not downloaded, so the command
is safe to run anywhere; only a developer who has run ``settag models download``
exercises them.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np
import pytest

from settag.analyzer import EssentiaGenreAnalyzer
from settag.model_store import default_model_dir, missing_task_files
from settag.policy import MIN_GENRE_SECONDS
from settag.workflow import analyze_paths

MODEL_DIR = default_model_dir()

pytestmark = [
    pytest.mark.models,
    pytest.mark.skipif(
        bool(missing_task_files(MODEL_DIR, ("genre",))),
        reason=f"genre models are not installed in {MODEL_DIR}",
    ),
]


def _tone(path: Path, seconds: float, *, rate: int = 44100) -> None:
    """A 220 Hz sine: deterministic, decodes everywhere, and is not silence."""
    count = int(seconds * rate)
    samples = np.sin(2 * np.pi * 220 * np.arange(count) / rate) * 0.3
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((samples * 32767).astype("<i2").tobytes())


@pytest.fixture(scope="module")
def analyzer() -> EssentiaGenreAnalyzer:
    return EssentiaGenreAnalyzer(MODEL_DIR, sample="middle")


def test_a_full_length_track_yields_one_finite_score_per_label(
    tmp_path: Path, analyzer: EssentiaGenreAnalyzer
) -> None:
    track = tmp_path / "track.wav"
    _tone(track, MIN_GENRE_SECONDS + 5)

    predictions = analyzer.analyze(track)

    assert len(predictions) == len(analyzer.labels) == 519
    assert {item.label for item in predictions} == set(analyzer.labels)
    scores = [item.score for item in predictions]
    assert all(math.isfinite(score) and 0.0 <= score <= 1.0 for score in scores)
    assert scores == sorted(scores, reverse=True)


def test_the_model_refuses_a_clip_shorter_than_one_patch(
    tmp_path: Path, analyzer: EssentiaGenreAnalyzer
) -> None:
    """MIN_GENRE_SECONDS mirrors this refusal; if MAEST's patch size changes, this fails first."""
    clip = tmp_path / "clip.wav"
    _tone(clip, MIN_GENRE_SECONDS - 10)

    with pytest.raises(RuntimeError, match="too short"):
        analyzer.analyze(clip)


def test_the_workflow_names_the_short_clip_before_the_model_sees_it(
    tmp_path: Path, analyzer: EssentiaGenreAnalyzer
) -> None:
    clip = tmp_path / "clip.wav"
    _tone(clip, MIN_GENRE_SECONDS - 10)

    batch = analyze_paths((clip,), analyzer=analyzer, top=5, threshold=0.1)

    assert [failure.error_type for failure in batch.failures] == ["TrackTooShortError"]
    assert f"shorter than the {MIN_GENRE_SECONDS:g}s" in batch.failures[0].message
