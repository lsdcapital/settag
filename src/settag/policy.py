from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

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
