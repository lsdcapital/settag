from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import numpy as np

# Ranked evidence retained per task. This must cover a whole taxonomy, not a shortlist:
# a consumer computing a signed composite (high-arousal labels minus low-arousal ones)
# needs both ends, and truncating to a shortlist silently drops the low end because those
# labels rank last by construction. Measured at 20 on a 56-class mood taxonomy, `fast` and
# `slow` were never written at all and `calm`/`soft`/`powerful`/`heavy` landed on 2-4% of
# tracks, so any composite using them was computed against zeros.
#
# 60 covers the mood (56) and instrument (40) taxonomies completely and keeps the 519-class
# genre taxonomy bounded, where a ranked shortlist is what its consumer wants. Cost is a few
# KB per file. Changing this changes the evidence configuration digest, so previously
# analysed tracks are correctly reported as stale.
EVIDENCE_LIMIT = 60


@dataclass(frozen=True)
class Prediction:
    label: str
    score: float

    def to_dict(self) -> dict[str, object]:
        return {"label": self.label, "score": self.score}


def rank_predictions(
    labels: Iterable[str],
    scores: Iterable[float],
) -> list[Prediction]:
    predictions = [
        Prediction(label=label, score=float(score))
        for label, score in zip(labels, scores, strict=True)
    ]
    return sorted(predictions, key=lambda item: (-item.score, item.label))


def collect_evidence(
    predictions: Iterable[Prediction],
    *,
    limit: int = EVIDENCE_LIMIT,
) -> list[Prediction]:
    """Return a bounded, deterministic evidence set without a score cutoff."""
    if limit < 1:
        raise ValueError("evidence limit must be at least 1")
    return sorted(
        predictions,
        key=lambda item: (-item.score, item.label),
    )[:limit]


def select_predictions(
    predictions: Iterable[Prediction],
    *,
    threshold: float,
    top: int,
) -> list[Prediction]:
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be between 0 and 1")
    if top < 1:
        raise ValueError("top must be at least 1")

    return [item for item in predictions if item.score >= threshold][:top]


# How much of a track the genre model reads. MAEST embeds one 30s patch at a time and
# its graph is fixed at batch 1, so its cost is strictly linear in patch count and it is
# 92.7% of a run (15.54s against EffNet's 1.23s on a 482s track). Analyzing fewer patches
# is the only lever that moves the total.
#
# Measured over 14 library tracks against the full-track answer: `middle` is 2.22x faster
# and preserved the rolled-up conventional genre on 14/14, `spaced` is 1.62x and preserved
# it on 13/14. Both kept a Spearman rank correlation above 0.98 across all 519 labels, so
# the evidence keeps its shape; what churns is the densely packed 0.1-0.25 tail, where the
# model is not confident anyway. Fewer patches also means less averaging, so scores come
# out more peaked than a full-track run.
AudioSample = Literal["full", "middle", "spaced"]
AUDIO_SAMPLES: tuple[AudioSample, ...] = ("full", "middle", "spaced")

PATCH_SECONDS = 30
MIDDLE_PATCHES = 4
SPACED_PATCHES = 6


def parse_audio_sample(value: str) -> AudioSample:
    requested = value.strip()
    if requested not in AUDIO_SAMPLES:
        choices = ", ".join(AUDIO_SAMPLES)
        raise ValueError(f"unknown audio sample {value!r}; choose from {choices}")
    return requested


def sample_audio(
    audio: np.ndarray,
    *,
    strategy: AudioSample,
    sample_rate: int,
) -> np.ndarray:
    """Return the portion of ``audio`` the genre model should read.

    Cutting on exact ``PATCH_SECONDS`` boundaries means a sampled chunk is a whole
    number of MAEST patches, so concatenated chunks never produce a patch that
    straddles a join. Audio shorter than the requested window is returned as-is
    rather than padded: a short track is cheap already.
    """
    if strategy == "full":
        return audio
    patch = sample_rate * PATCH_SECONDS
    count = MIDDLE_PATCHES if strategy == "middle" else SPACED_PATCHES
    if len(audio) <= patch * count:
        return audio

    if strategy == "middle":
        start = (len(audio) - patch * count) // 2
        return audio[start : start + patch * count]

    offsets = np.linspace(0, len(audio) - patch, count).astype(int)
    return np.concatenate([audio[offset : offset + patch] for offset in offsets])
