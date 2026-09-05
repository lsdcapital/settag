"""Optional, producer-neutral audio evidence export; never part of music tags or plans."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from settag import __version__
from settag.catalog import ModelSpec

EMBEDDING_SCHEMA = "audio-embedding/v1"


def pooled_embedding(embeddings: Any, spec: ModelSpec) -> dict[str, object]:
    """Pool EffNet's patch-by-dimension matrix, retaining the exact feature-space identity."""
    matrix = np.asarray(embeddings, dtype=float)
    if matrix.ndim != 2 or not all(matrix.shape) or not np.isfinite(matrix).all():
        raise ValueError("Embedding output must be a non-empty finite patch-by-dimension matrix")
    pooled = matrix.mean(axis=0)
    norm = float(np.linalg.norm(pooled))
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError("Embedding mean has no finite nonzero direction")
    return {
        "model": {
            "id": spec.file("embedding").filename.removesuffix(".pb"),
            "sha256": spec.file("embedding").sha256,
            "output": spec.embedding_output,
        },
        "preprocessing": {
            "sample_rate": spec.sample_rate,
            "channels": "mono",
            "resample_quality": 4,
            "audio_sample": "full",
        },
        "pooling": "mean",
        "normalization": "l2",
        "dimensions": int(matrix.shape[1]),
        "patch_count": int(matrix.shape[0]),
        "vector": (pooled / norm).tolist(),
    }


def embedding_record(
    source: Mapping[str, object], analyzed_at: str, embedding: Mapping[str, object]
) -> dict[str, object]:
    return {
        "schema": EMBEDDING_SCHEMA,
        "producer": {"id": "settag", "version": __version__},
        "analyzed_at": analyzed_at,
        "source": {
            "path": source["path"],
            "sha256": source["sha256"],
            "audio_sha256": source["audio_sha256"],
            "audio_hash_algorithm": "settag.audio-sha256/v1",
        },
        "embedding": dict(embedding),
    }
