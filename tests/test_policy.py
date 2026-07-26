import numpy as np
import pytest

from settag.policy import (
    AUDIO_SAMPLES,
    EVIDENCE_LIMIT,
    MIDDLE_PATCHES,
    PATCH_SECONDS,
    SPACED_PATCHES,
    Prediction,
    collect_evidence,
    parse_audio_sample,
    rank_predictions,
    sample_audio,
    select_predictions,
)

SAMPLE_RATE = 16_000
PATCH = SAMPLE_RATE * PATCH_SECONDS


def test_rank_predictions_is_descending_and_deterministic() -> None:
    ranked = rank_predictions(["z", "b", "a"], [0.9, 0.5, 0.5])

    assert ranked == [
        Prediction("z", 0.9),
        Prediction("a", 0.5),
        Prediction("b", 0.5),
    ]


def test_selection_never_forces_a_prediction_below_threshold() -> None:
    selected = select_predictions(
        [Prediction("Electronic---House", 0.09)],
        threshold=0.10,
        top=5,
    )

    assert selected == []


def test_selection_applies_threshold_then_top_limit() -> None:
    predictions = [
        Prediction("a", 0.9),
        Prediction("b", 0.8),
        Prediction("c", 0.7),
    ]

    assert select_predictions(predictions, threshold=0.75, top=1) == [Prediction("a", 0.9)]


def test_evidence_is_ranked_bounded_and_never_score_filtered() -> None:
    predictions = [
        Prediction(f"label-{index:02}", index / 100) for index in range(EVIDENCE_LIMIT + 5)
    ]

    evidence = collect_evidence(predictions)

    # Expressed relative to the limit so raising it does not require editing expectations:
    # the five lowest-scoring predictions fall away and the rest survive in rank order.
    highest = EVIDENCE_LIMIT + 4
    assert len(evidence) == EVIDENCE_LIMIT
    assert evidence[0] == Prediction(f"label-{highest:02}", highest / 100)
    assert evidence[-1] == Prediction("label-05", 0.05)


def _audio(seconds: float) -> np.ndarray:
    return np.arange(int(SAMPLE_RATE * seconds), dtype=np.float32)


def test_full_sample_returns_the_track_unchanged() -> None:
    audio = _audio(480)

    assert sample_audio(audio, strategy="full", sample_rate=SAMPLE_RATE) is audio


@pytest.mark.parametrize(
    ("strategy", "patches"),
    [("middle", MIDDLE_PATCHES), ("spaced", SPACED_PATCHES)],
)
def test_sampling_returns_a_whole_number_of_patches(strategy, patches: int) -> None:
    """Cutting on patch boundaries is what keeps a patch from straddling a join."""
    sampled = sample_audio(_audio(480), strategy=strategy, sample_rate=SAMPLE_RATE)

    assert len(sampled) == PATCH * patches
    assert len(sampled) % PATCH == 0


def test_middle_sample_is_centred() -> None:
    audio = _audio(480)

    sampled = sample_audio(audio, strategy="middle", sample_rate=SAMPLE_RATE)

    start = (len(audio) - PATCH * MIDDLE_PATCHES) // 2
    assert np.array_equal(sampled, audio[start : start + PATCH * MIDDLE_PATCHES])


def test_spaced_sample_reaches_both_ends_of_the_track() -> None:
    audio = _audio(480)

    sampled = sample_audio(audio, strategy="spaced", sample_rate=SAMPLE_RATE)

    assert np.array_equal(sampled[:PATCH], audio[:PATCH])
    assert np.array_equal(sampled[-PATCH:], audio[-PATCH:])


@pytest.mark.parametrize("strategy", ["middle", "spaced"])
def test_short_tracks_are_returned_whole(strategy) -> None:
    """A track smaller than the window is already cheap; padding would invent audio."""
    audio = _audio(45)

    assert sample_audio(audio, strategy=strategy, sample_rate=SAMPLE_RATE) is audio


def test_parse_audio_sample_accepts_every_documented_strategy() -> None:
    assert [parse_audio_sample(name) for name in AUDIO_SAMPLES] == list(AUDIO_SAMPLES)
    assert parse_audio_sample("  middle  ") == "middle"


def test_parse_audio_sample_rejects_an_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="unknown audio sample 'half'"):
        parse_audio_sample("half")
