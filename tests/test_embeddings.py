import json

import pytest

from settag.catalog import DISCOGS_EFFNET_INSTRUMENT
from settag.embeddings import embedding_record, pooled_embedding


def test_pools_patches_before_normalizing_and_retains_coordinate_identity() -> None:
    value = pooled_embedding([[3, 0], [0, 4]], DISCOGS_EFFNET_INSTRUMENT)
    assert value["vector"] == pytest.approx([0.6, 0.8])
    assert value["dimensions"] == 2
    assert value["patch_count"] == 2
    model = value["model"]
    assert isinstance(model, dict)
    assert model["sha256"] == DISCOGS_EFFNET_INSTRUMENT.file("embedding").sha256
    record = embedding_record(
        {"path": "/music/track.wav", "sha256": "a" * 64, "audio_sha256": "b" * 64},
        "2026-09-05T10:00:00Z",
        value,
    )
    assert json.loads(json.dumps(record, allow_nan=False))["schema"] == "audio-embedding/v1"


@pytest.mark.parametrize("values", [[], [1, 2], [[0, 0]], [[float("nan"), 1]], [[float("inf"), 1]]])
def test_invalid_or_directionless_embeddings_are_rejected(values) -> None:
    with pytest.raises(ValueError, match="Embedding"):
        pooled_embedding(values, DISCOGS_EFFNET_INSTRUMENT)
