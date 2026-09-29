"""Validate comparison integrity without installing MuQ or downloading weights."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "muq_experiment", Path(__file__).parents[1] / "scripts/muq_experiment.py"
)
assert _spec is not None
assert _spec.loader is not None
experiment = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(experiment)


@pytest.mark.parametrize("value", [[0, 0], [float("nan"), 1], [float("inf"), 1]])
def test_directionless_evidence_cannot_enter_retrieval(value):
    with pytest.raises(ValueError, match="Nonfinite or directionless"):
        experiment.unit(value)


@pytest.mark.parametrize("problem", ["missing", "duplicate", "source"])
def test_report_refuses_incomparable_runs(tmp_path, problem):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"tracks": [{"id": "a", "sha256": "expected"}]})
    )
    row = {"id": "a", "source_sha256": "expected", "vector": [1, 0]}
    rows = [row]
    if problem == "missing":
        rows = []
    elif problem == "duplicate":
        rows = [row, row]
    else:
        row["source_sha256"] = "different"
    (tmp_path / "effnet.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(RuntimeError, match=r"coverage|source mismatch"):
        experiment.report(SimpleNamespace(run=tmp_path))
    assert not (tmp_path / "results.json").exists()


def test_known_neighbours_and_blinded_playlists(tmp_path):
    vectors = [[1, 0], [0.99, 0.1], [0.8, 0.6], [0, 1], [-1, 0], [0, -1], [0.7, -0.7]]
    tracks, rows = [], []
    for i, vector in enumerate(vectors):
        tracks.append(
            {
                "id": str(i),
                "sha256": "a" * 64,
                "folder": str(i),
                "title": str(i),
                "artist": "Example",
                "seconds": 120,
                "path": f"/music/{i}.mp3",
            }
        )
        rows.append(
            {
                "id": str(i),
                "source_sha256": "a" * 64,
                "vector": experiment.unit(vector),
                "decode_seconds": 1,
                "inference_seconds": 2,
                "peak_process_rss_bytes": 100,
            }
        )
    (tmp_path / "manifest.json").write_text(json.dumps({"tracks": tracks}))
    for name in ("effnet", "muq"):
        (tmp_path / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    experiment.report(SimpleNamespace(run=tmp_path))
    result = json.loads((tmp_path / "results.json").read_text())
    assert result["mean_top5_overlap"] == 1
    neighbours = result["models"]["muq"]["neighbours"][0]["top5"]
    assert neighbours[:3] == ["1", "2", "6"]
    assert "0" not in neighbours
    playlist = (tmp_path / "listening/01-A.m3u8").read_text()
    assert playlist.count("#EXTINF:") == 6
    assert playlist.splitlines()[2] == "/music/0.mp3"
    review = (tmp_path / "listening/review.md").read_text()
    assert "effnet" not in review
    assert "muq" not in review
