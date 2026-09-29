"""Retrospective held-out playlist continuation; never writes to SetPath or audio."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def groups(scores):
    """Tie groups in descending score order; avoid track-ID-dependent evaluation."""
    values = np.round(np.asarray(scores, dtype=float), 12)
    return [np.flatnonzero(values == value) for value in np.unique(values)[::-1]]


def midranks(scores):
    result = np.empty(len(scores))
    start = 1
    for indices in groups(scores):
        result[indices] = start + (len(indices) - 1) / 2
        start += len(indices)
    return result


def metrics(scores, relevant, next_index, k=5):
    """Expected metrics under uniformly randomized ordering inside each tie group."""
    relevant = np.asarray(relevant, dtype=bool)
    if not relevant.any():
        raise ValueError("A query must have an available positive")
    hits, dcg, next_hit, reciprocal_rank = 0.0, 0.0, 0.0, 0.0
    start = 1
    for indices in groups(scores):
        ranks = np.arange(start, start + len(indices))
        visible = ranks[ranks <= k]
        density = float(relevant[indices].mean())
        hits += len(visible) * density
        dcg += density * float(np.sum(1 / np.log2(visible + 1)))
        if next_index is not None and next_index in indices:
            next_hit = len(visible) / len(indices)
            reciprocal_rank = float(np.mean(1 / ranks))
        start += len(indices)
    ideal = float(np.sum(1 / np.log2(np.arange(1, min(k, int(relevant.sum())) + 1) + 1)))
    return {
        "later_choices_in_top5": hits,
        "recall_at5": hits / int(relevant.sum()),
        "ndcg_at5": dcg / ideal,
        "next_hit_at5": next_hit if next_index is not None else None,
        "next_mrr": reciprocal_rank if next_index is not None else None,
    }


def queries(sequence, available):
    """Keep original positions: a missing track never creates a fictitious transition."""
    for cut in range(3, len(sequence)):
        context = sequence[cut - 3 : cut]
        if not all(track in available for track in context):
            continue
        prefix = set(sequence[:cut])
        positives = set(sequence[cut:]) & available - prefix
        if positives:
            yield {
                "cut": cut,
                "context": context,
                "prefix": prefix,
                "positives": positives,
                "next": sequence[cut] if sequence[cut] in available - prefix else None,
            }


def playlist_scores(candidates, context, sequences, held_out):
    training = [set(sequence) for name, sequence in sequences.items() if name != held_out]
    # Prioritize co-membership with recent tracks; playlist frequency breaks score ties.
    return np.array(
        [
            sum(
                candidate in playlist and seed in playlist
                for playlist in training
                for seed in context
            )
            * (len(training) + 1)
            + sum(candidate in playlist for playlist in training)
            for candidate in candidates
        ],
        dtype=float,
    )


def average(rows):
    return {
        field: float(np.mean([row[field] for row in rows if row[field] is not None]))
        for field in rows[0]
    }


def run(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    tracks = {track["id"]: track for track in manifest["tracks"]}
    ids = list(tracks)
    positions = {track: index for index, track in enumerate(ids)}
    playlists = json.loads((directory / "playlists.json").read_text())
    sequences = {
        name: [entry["id"] for entry in entries] for name, entries in playlists["playlists"].items()
    }
    arrays = {}
    for model in ("effnet", "muq"):
        rows = [
            json.loads(line) for line in (directory / f"{model}.jsonl").read_text().splitlines()
        ]
        by_id = {row["id"]: row for row in rows}
        if len(rows) != len(ids) or set(by_id) != set(ids):
            raise ValueError(f"Incomplete or duplicate {model} coverage")
        if any(by_id[tid]["source_sha256"] != tracks[tid]["sha256"] for tid in ids):
            raise ValueError("Source identity mismatch")
        arrays[model] = np.array([by_id[tid]["vector"] for tid in ids])
        if not np.isfinite(arrays[model]).all() or not np.allclose(
            np.linalg.norm(arrays[model], axis=1), 1
        ):
            raise ValueError("Invalid normalized embeddings")
    folds, details = [], []
    for held_out in playlists["held_out"]:
        sequence = sequences[held_out]
        fold_rows = []
        for query in queries(sequence, set(ids)):
            candidates = [tid for tid in ids if tid not in query["prefix"]]
            ci = [positions[tid] for tid in candidates]
            qi = [positions[tid] for tid in query["context"]]
            scores = {"random": np.zeros(len(candidates))}
            scores["playlist-only"] = playlist_scores(
                candidates, query["context"], sequences, held_out
            )
            bpms = [tracks[tid].get("bpm") for tid in query["context"]]
            known_bpms = [bpm for bpm in bpms if bpm and bpm > 0]
            tempo = np.median(known_bpms) if known_bpms else None
            scores["bpm-only"] = np.array(
                [
                    -abs(tracks[tid]["bpm"] - tempo)
                    if tempo is not None and tracks[tid].get("bpm")
                    else -1e9
                    for tid in candidates
                ]
            )
            for model in ("effnet", "muq"):
                scores[model] = arrays[model][ci] @ arrays[model][qi].mean(axis=0)
                scores[f"playlist+{model}"] = 1 / (60 + midranks(scores["playlist-only"])) + 1 / (
                    60 + midranks(scores[model])
                )
            relevant = [tid in query["positives"] for tid in candidates]
            next_index = candidates.index(query["next"]) if query["next"] else None
            measured = {
                name: metrics(value, relevant, next_index) for name, value in scores.items()
            }
            fold_rows.append(measured)
            details.append(
                {
                    "playlist": held_out,
                    "after_position": query["cut"],
                    "context": query["context"],
                    "actual_next": query["next"],
                    "remaining_choices": sorted(query["positives"]),
                    "candidates": len(candidates),
                    "metrics": measured,
                    "top5": {
                        name: [candidates[i] for i in np.argsort(-value, kind="stable")[:5]]
                        for name, value in scores.items()
                        if name != "random"
                    },
                    "note": "Tie-averaged metrics; displayed tie order is illustrative only.",
                }
            )
        if not fold_rows:
            raise ValueError(f"No evaluable queries for {held_out}")
        folds.append(
            {
                "playlist": held_out,
                "queries": len(fold_rows),
                "playlist_tracks": len(sequence),
                "available_tracks": sum(tid in tracks for tid in sequence),
                "metrics": {
                    name: average([row[name] for row in fold_rows]) for name in fold_rows[0]
                },
            }
        )
    result = {
        "scope": "Retrospective curated-playlist recovery; no historic availability claim",
        "candidate_tracks": len(ids),
        "held_out_playlists": len(folds),
        "queries": len(details),
        "exact_next_queries": sum(row["actual_next"] is not None for row in details),
        "macro": {
            name: average([fold["metrics"][name] for fold in folds]) for name in folds[0]["metrics"]
        },
        "folds": folds,
        "queries_detail": details,
        "limitations": [
            "Current membership selected the candidate pool, including held-out choices.",
            "The baseline excludes the held-out playlist. Raw audition histories are not used.",
            "Other playlists may have been curated later. This is not time-causal replay.",
            "Missing files are excluded; queries with a missing recent-context track are skipped.",
            "Non-selected tracks are unlabelled alternatives, not proven bad recommendations.",
            "Prefixes are correlated. Macro averages weight each of four sets equally.",
            "No tuning on held-out results. Rank-fusion constant is fixed at 60.",
            "Saved order may differ from performed order. Crowd response is not observed.",
        ],
    }
    with (directory / "playlist-results.json").open("x") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    lines = [
        "# Playlist-based MuQ comparison",
        "",
        f"{len(ids)} curated tracks; four held-out sets; {len(details)} continuation queries.",
        "Use the last three saved entries to rank tracks not already in the playlist prefix.",
        (
            "Success means retrieving tracks you selected later in the held-out set. "
            "It does not imply other suggestions are bad."
        ),
        "",
        "## Equal-weight average across the four sets",
        "",
        "| Method | Later choices in top 5 | Recall@5 | NDCG@5 | Exact next in top 5 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, values in result["macro"].items():
        lines.append(
            f"| {name} | {values['later_choices_in_top5']:.2f} | {values['recall_at5']:.1%} "
            f"| {values['ndcg_at5']:.3f} | {values['next_hit_at5']:.1%} |"
        )
    lines.extend(
        [
            "",
            "## NDCG@5 by held-out set",
            "",
            "| Set | Queries | Playlist-only | BPM | EffNet | MuQ | History+EffNet | History+MuQ |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for fold in folds:
        values = " | ".join(
            f"{fold['metrics'][name]['ndcg_at5']:.3f}"
            for name in (
                "playlist-only",
                "bpm-only",
                "effnet",
                "muq",
                "playlist+effnet",
                "playlist+muq",
            )
        )
        lines.append(f"| {fold['playlist']} | {fold['queries']} | {values} |")
    lines.extend(
        [
            "",
            (
                "NDCG rewards placing your later choices near the top; 1 is ideal. "
                "Recall measures the fraction of remaining choices retrieved. "
                "Exact-next recovery is a stricter secondary measure. "
                "Tied scores use expected metrics, not arbitrary ID ordering."
            ),
            "",
            (
                "The playlist-only baseline prioritizes co-membership with context tracks "
                "in the other five curated playlists, then frequency across those playlists. "
                "The BPM baseline ranks by absolute distance from the median context BPM. "
                "Audio models rank by mean cosine similarity to the same three tracks. "
                "Combined methods use equal reciprocal-rank fusion with constant 60."
            ),
            "",
            "## Limits",
            "",
        ]
    )
    lines.extend(f"- {limitation}" for limitation in result["limitations"])
    lines.extend(
        [
            "",
            (
                "[Suggestions in your playlist contexts](candidate-examples.md). "
                "[Per-query results](playlist-results.json). "
                "[Original inputs](playlists.json). No database, audio, or live ranking changes."
            ),
            "",
        ]
    )
    (directory / "playlist-report.md").write_text("\n".join(lines))
    examples = [
        "# Suggestions in your playlist contexts",
        "",
        "One example per set: the first evaluable prefix, selected independently of scores.",
        (
            "A star marks a track you selected later in this set. Other tracks are unlabelled "
            "alternatives, not known bad suggestions."
        ),
        "",
    ]
    for held_out in playlists["held_out"]:
        query = next(row for row in details if row["playlist"] == held_out)
        context = " → ".join(tracks[tid]["title"] for tid in query["context"])
        actual = (
            tracks[query["actual_next"]]["title"] if query["actual_next"] else "Audio unavailable"
        )
        examples.extend(
            [f"## {held_out}", "", f"Context: {context}", "", f"Your next choice: **{actual}**", ""]
        )
        for method in ("effnet", "muq", "bpm-only", "playlist+muq"):
            examples.extend([f"### {method}", ""])
            for rank, tid in enumerate(query["top5"][method], start=1):
                track = tracks[tid]
                mark = " ★" if tid in query["remaining_choices"] else ""
                examples.append(f"{rank}. {track['artist']} — {track['title']}{mark}")
            examples.append("")
    (directory / "candidate-examples.md").write_text("\n".join(examples))
    print(json.dumps({"queries": len(details), "macro": result["macro"], "folds": folds}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    run(parser.parse_args().run)
