from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

_spec = importlib.util.spec_from_file_location(
    "muq_playlist_eval", Path(__file__).parents[1] / "scripts/muq_playlist_eval.py"
)
assert _spec is not None
assert _spec.loader is not None
evaluation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluation)


def test_random_baseline_averages_ties_instead_of_using_track_order():
    result = evaluation.metrics(np.zeros(10), [True, True] + [False] * 8, 0)
    assert result["later_choices_in_top5"] == pytest.approx(1)
    assert result["recall_at5"] == pytest.approx(0.5)
    assert result["next_hit_at5"] == pytest.approx(0.5)
    assert result["next_mrr"] == pytest.approx(sum(1 / rank for rank in range(1, 11)) / 10)
    reordered = evaluation.metrics(np.zeros(10), [False] * 8 + [True, True], 9)
    assert result == reordered


def test_ideal_ranking_and_top_five_boundary_tie():
    ideal = evaluation.metrics([10, 9, 8, 7, 6, 5], [True, True, False, False, False, False], 0)
    assert ideal["ndcg_at5"] == 1
    assert ideal["next_mrr"] == 1
    boundary = evaluation.metrics([9, 8, 7, 6, 5, 5], [False] * 4 + [True, False], 4)
    assert boundary["later_choices_in_top5"] == pytest.approx(0.5)
    assert boundary["next_hit_at5"] == pytest.approx(0.5)


def test_held_out_membership_cannot_influence_history_baseline():
    training = {"held": ["a", "target"], "other": ["a", "b"]}
    before = evaluation.playlist_scores(["b", "target"], ["a"], training, "held")
    training["held"] = ["a", "b", "target", "target"]
    after = evaluation.playlist_scores(["b", "target"], ["a"], training, "held")
    np.testing.assert_array_equal(before, after)
    assert before[0] > before[1]
    assert before[1] == 0


def test_missing_audio_does_not_stitch_context_across_gap():
    sequence = ["a", "missing", "b", "c", "d", "e", "f"]
    rows = list(evaluation.queries(sequence, set(sequence) - {"missing"}))
    assert [row["cut"] for row in rows] == [5, 6]
    assert rows[0]["context"] == ["b", "c", "d"]
    assert rows[0]["next"] == "e"
    assert rows[0]["positives"] == {"e", "f"}


def test_missing_next_can_leave_later_recovery_without_claiming_exact_next():
    rows = list(evaluation.queries(["a", "b", "c", "missing", "d"], {"a", "b", "c", "d"}))
    assert len(rows) == 1
    assert rows[0]["next"] is None
    assert rows[0]["positives"] == {"d"}


def test_rank_fusion_treats_ties_equally():
    np.testing.assert_array_equal(evaluation.midranks([4, 4, 2, 1]), [1.5, 1.5, 3, 4])


def test_complete_evaluation_uses_same_unseen_candidates_for_every_model(tmp_path):
    tracks = [
        {"id": str(i), "sha256": "a" * 64, "bpm": 120 + i, "title": str(i), "artist": "Example"}
        for i in range(12)
    ]
    sequences = {
        "a": list(range(8)),
        "b": list(range(4, 12)),
        "c": list(range(0, 12, 2)),
        "d": list(range(1, 12, 2)),
        "reference1": [0, 1, 2],
        "reference2": [9, 10, 11],
    }
    (tmp_path / "manifest.json").write_text(json.dumps({"tracks": tracks}))
    (tmp_path / "playlists.json").write_text(
        json.dumps(
            {
                "held_out": ["a", "b", "c", "d"],
                "playlists": {
                    name: [{"id": str(i), "position": p + 1} for p, i in enumerate(sequence)]
                    for name, sequence in sequences.items()
                },
            }
        )
    )
    # Orthogonal track embeddings provide no similarity signal for unseen candidates.
    rows = [
        {"id": str(i), "source_sha256": "a" * 64, "vector": vector.tolist()}
        for i, vector in enumerate(np.eye(12))
    ]
    for model in ("muq", "effnet"):
        (tmp_path / f"{model}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    evaluation.run(tmp_path)
    result = json.loads((tmp_path / "playlist-results.json").read_text())
    assert result["queries"] == 16
    assert result["exact_next_queries"] == 16
    assert result["macro"]["muq"] == result["macro"]["random"]
    for row in result["queries_detail"]:
        prefix = {str(i) for i in sequences[row["playlist"]][: row["after_position"]]}
        assert all(not prefix.intersection(top) for top in row["top5"].values())
    assert (tmp_path / "candidate-examples.md").is_file()
