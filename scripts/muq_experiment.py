"""Offline, read-only audio retrieval experiment; dependencies live in a separate venv.

Run select, effnet, muq, then report with the same --run directory. Experiment
records deliberately do not claim compatibility with audio-embedding/v1: MuQ's
chunk/tail/layer recipe needs a richer feature-space identity.
"""

# Backend imports stay lazy so EffNet does not require PyTorch and reports load neither.
# ruff: noqa: PLC0415

from __future__ import annotations

import argparse
import importlib.metadata
import json
import random
import resource
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from settag.hashing import sha256_audio, sha256_file


def write_json(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def unit(vector):
    vector = np.asarray(vector, dtype=np.float64)
    norm = np.linalg.norm(vector)
    if not np.isfinite(vector).all() or not np.isfinite(norm) or norm <= 0:
        raise ValueError("Nonfinite or directionless embedding")
    return (vector / norm).tolist()


def select(args):
    import mutagen

    groups = defaultdict(list)
    for path in sorted(args.library.rglob("*")):
        if path.suffix.lower() not in {".mp3", ".flac", ".aiff", ".aif", ".wav", ".m4a"}:
            continue
        group = path.relative_to(args.library).parts[0]
        if group.lower() in {"samples", "acapella"}:
            continue
        groups[group].append(path)
    rng = random.Random(20260909)
    for paths in groups.values():
        rng.shuffle(paths)
    tracks, seen, excluded = [], set(), []
    while len(tracks) < args.count and any(groups.values()):
        for group in sorted(groups):
            if not groups[group] or len(tracks) >= args.count:
                continue
            path = groups[group].pop()
            try:
                tags = mutagen.File(path, easy=True)
                seconds = tags.info.length
                if not 60 <= seconds <= 900:
                    excluded.append(
                        {"path": str(path), "reason": "duration outside 60-900 seconds"}
                    )
                    continue
                audio_hash = sha256_audio(path)
                if audio_hash in seen:
                    excluded.append({"path": str(path), "reason": "duplicate audio payload"})
                    continue
                seen.add(audio_hash)
                tracks.append(
                    {
                        "id": f"track-{len(tracks) + 1:03d}",
                        "path": str(path.resolve()),
                        "title": (tags.get("title") or [path.stem])[0],
                        "artist": (tags.get("artist") or [""])[0],
                        "genres": tags.get("genre", []),
                        "folder": group,
                        "seconds": seconds,
                        "sha256": sha256_file(path),
                        "audio_sha256": audio_hash,
                    }
                )
            except Exception as error:
                excluded.append({"path": str(path), "reason": str(error)})
    if len(tracks) != args.count:
        raise RuntimeError(f"Only {len(tracks)} eligible tracks")
    write_json(
        args.run / "manifest.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "selection": (
                "seed 20260909; shuffled within folders, round-robin across folders; "
                "60-900s; audio-payload deduplicated"
            ),
            "library": str(args.library),
            "tracks": tracks,
            "excluded": excluded,
            "folder_counts": dict(Counter(t["folder"] for t in tracks)),
        },
    )
    print(
        f"Selected {len(tracks)} tracks, {sum(t['seconds'] for t in tracks) / 3600:.2f} hours",
        flush=True,
    )


def infer(args):
    import essentia
    import essentia.standard as es

    tracks = json.loads((args.run / "manifest.json").read_text())["tracks"]
    output = args.run / f"{args.command}.jsonl"
    existing = (
        [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []
    )
    completed = {row["id"] for row in existing}
    sources = {track["id"]: track["sha256"] for track in tracks}
    if len(completed) != len(existing) or any(
        sources.get(row["id"]) != row["source_sha256"] for row in existing
    ):
        raise RuntimeError("Resume records do not match the manifest")
    started = time.perf_counter()
    essentia.log.infoActive = False
    essentia.log.warningActive = False
    if args.command == "effnet":
        from settag.catalog import DISCOGS_EFFNET_INSTRUMENT as spec
        from settag.embeddings import pooled_embedding

        model_path = spec.path(args.models, "embedding")
        assert sha256_file(model_path) == spec.file("embedding").sha256
        model = vars(es)["TensorflowPredictEffnetDiscogs"](
            graphFilename=str(model_path), output=spec.embedding_output
        )
        recipe: dict[str, Any] = {
            "model": "discogs-effnet-bs64-1",
            "weights_sha256": sha256_file(model_path),
            "sample_rate": 16000,
            "decoder": "essentia.MonoLoader",
            "resample_quality": 4,
            "channels": "mono",
            "audio_sample": "full",
            "pooling": "patch mean then L2",
            "output": spec.embedding_output,
            "device": "cpu",
        }
    else:
        # Experiment-only dependencies, installed into the run environment, not the project.
        import torch  # ty: ignore[unresolved-import]
        from huggingface_hub import snapshot_download  # ty: ignore[unresolved-import]
        from muq import MuQ  # ty: ignore[unresolved-import]

        torch.set_num_threads(4)
        torch.manual_seed(20260909)
        info = json.loads((args.run / "model-info.json").read_text())
        snapshot = Path(
            snapshot_download(
                "OpenMuQ/MuQ-large-msd-iter",
                revision=info["sha"],
                cache_dir=str(args.run / "hf-cache"),
                allow_patterns=["config.json", "*.safetensors"],
            )
        )
        device = args.device
        if device == "auto":
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        model = MuQ.from_pretrained(str(snapshot)).to(device).float().eval()
        recipe: dict[str, Any] = {
            "model": "OpenMuQ/MuQ-large-msd-iter",
            "revision": info["sha"],
            "files": {p.name: sha256_file(p) for p in sorted(snapshot.iterdir()) if p.is_file()},
            "sample_rate": 24000,
            "decoder": "essentia.MonoLoader",
            "resample_quality": 4,
            "channels": "mono",
            "audio_sample": "full",
            "chunk_seconds": 10,
            "overlap": 0,
            "tail": "un-padded remainder; if shorter than 1 second merge into previous chunk",
            "output": "last_hidden_state",
            "pooling": "all frames mean then L2",
            "dtype": "float32",
            "device": device,
        }
    recipe["packages"] = {
        name: importlib.metadata.version(name)
        for name in (
            ["numpy", "essentia-tensorflow"]
            if args.command == "effnet"
            else ["numpy", "essentia-tensorflow", "muq", "torch", "transformers", "huggingface-hub"]
        )
    }
    recipe_path = args.run / f"{args.command}-recipe.json"
    if recipe_path.exists():
        if json.loads(recipe_path.read_text()) != recipe:
            raise RuntimeError("Recipe changed: use a new run directory")
    else:
        write_json(recipe_path, recipe)
    print(
        f"Loaded {args.command}: {recipe['device']}, {time.perf_counter() - started:.1f}s",
        flush=True,
    )
    count = 0
    with output.open("a") as handle:
        for track in tracks:
            if track["id"] in completed:
                continue
            start = time.perf_counter()
            path = Path(track["path"])
            if sha256_file(path) != track["sha256"]:
                raise RuntimeError(f"Source changed: {path}")
            audio = vars(es)["MonoLoader"](
                filename=str(path), sampleRate=recipe["sample_rate"], resampleQuality=4
            )()
            decoded = time.perf_counter()
            if args.command == "effnet":
                embedding = pooled_embedding(model(audio), spec)
                vector, frames = embedding["vector"], embedding["patch_count"]
                chunks = None
            else:
                size = 240000
                boundaries = list(range(0, len(audio), size))
                if len(boundaries) > 1 and len(audio) - boundaries[-1] < 24000:
                    boundaries.pop()
                boundaries.append(len(audio))
                total, frames = np.zeros(1024, dtype=np.float64), 0
                with torch.inference_mode():
                    for begin, end in pairwise(boundaries):
                        wav = torch.from_numpy(audio[begin:end].copy()).unsqueeze(0).to(device)
                        features = (
                            model(wav, output_hidden_states=False)
                            .last_hidden_state[0]
                            .cpu()
                            .numpy()
                        )
                        if not np.isfinite(features).all():
                            raise ValueError(f"Nonfinite MuQ output: {track['id']} at {begin}")
                        total += features.sum(axis=0, dtype=np.float64)
                        frames += len(features)
                vector, chunks = unit(total / frames), len(boundaries) - 1
            inferred = time.perf_counter()
            if sha256_file(path) != track["sha256"]:
                raise RuntimeError(f"Source changed during inference: {path}")
            row = {
                "schema": "settag-retrieval-experiment/v1",
                "id": track["id"],
                "source_sha256": track["sha256"],
                "vector": vector,
                "frames": frames,
                "chunks": chunks,
                "decode_seconds": decoded - start,
                "inference_seconds": inferred - decoded,
                "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            }
            handle.write(json.dumps(row, allow_nan=False) + "\n")
            handle.flush()
            count += 1
            print(
                f"{args.command} {len(completed) + count}/{len(tracks)} "
                f"{track['id']} {inferred - start:.1f}s {track['title']}",
                flush=True,
            )
            if args.limit and count >= args.limit:
                break


def report(args):
    tracks = json.loads((args.run / "manifest.json").read_text())["tracks"]
    results, matrices = {}, {}
    for name in ("effnet", "muq"):
        rows = [json.loads(line) for line in (args.run / f"{name}.jsonl").read_text().splitlines()]
        by_id = {row["id"]: row for row in rows}
        if len(rows) != len(tracks) or set(by_id) != {t["id"] for t in tracks}:
            raise RuntimeError(f"Incomplete/duplicate {name} coverage")
        for track in tracks:
            if by_id[track["id"]]["source_sha256"] != track["sha256"]:
                raise RuntimeError(f"Embedding source mismatch: {track['id']}")
        matrix = np.array([by_id[t["id"]]["vector"] for t in tracks])
        if not np.isfinite(matrix).all() or not np.allclose(np.linalg.norm(matrix, axis=1), 1):
            raise ValueError("Invalid normalized embeddings")
        similarity = matrix @ matrix.T
        np.fill_diagonal(similarity, -np.inf)
        matrices[name] = np.argsort(-similarity, axis=1, kind="stable")[:, :5]
        results[name] = {
            "dimensions": matrix.shape[1],
            "tracks": len(rows),
            "inference_seconds": sum(row["inference_seconds"] for row in rows),
            "decode_seconds": sum(row["decode_seconds"] for row in rows),
            "peak_process_rss_bytes": max(row["peak_process_rss_bytes"] for row in rows),
            "distinct_tracks_retrieved": len(set(matrices[name].ravel().tolist())),
            "same_folder_fraction": float(
                np.mean(
                    [
                        tracks[i]["folder"] == tracks[j]["folder"]
                        for i, indices in enumerate(matrices[name])
                        for j in indices
                    ]
                )
            ),
            "neighbours": [
                {"id": t["id"], "top5": [tracks[j]["id"] for j in matrices[name][i]]}
                for i, t in enumerate(tracks)
            ],
        }
    overlap = [
        len(set(a) & set(b)) / 5 for a, b in zip(matrices["effnet"], matrices["muq"], strict=True)
    ]
    summary = {
        "models": results,
        "mean_top5_overlap": float(np.mean(overlap)),
        "queries_with_no_shared_neighbours": sum(value == 0 for value in overlap),
        "interpretation": (
            "Overlap and folder agreement are descriptive, not retrieval quality or DJ usefulness. "
            "Human judgments pending."
        ),
    }
    write_json(args.run / "results.json", summary)
    listening = args.run / "listening"
    listening.mkdir(exist_ok=True)
    rng = random.Random(913)
    assignments, ratings = [], []
    review = [
        "# Blinded listening comparison",
        "",
        "Each playlist contains the seed followed by five neighbours.",
        (
            "Listen for sonic resemblance and useful-next-track separately. "
            "Tempo/key compatibility is not enforced."
        ),
        (
            "Record preferences A, B, tie, or neither in ratings.json. "
            "Do not open blind-key.json until finished."
        ),
        "",
    ]
    # Spread queries across the full folder range, independent of retrieval results.
    first_by_folder = {}
    for index, track in enumerate(tracks):
        first_by_folder.setdefault(track["folder"], index)
    folder_seeds = list(first_by_folder.values())
    seed_indices = [
        folder_seeds[index]
        for index in np.linspace(0, len(folder_seeds) - 1, min(20, len(folder_seeds)), dtype=int)
    ]
    for query_number, i in enumerate(seed_indices, start=1):
        seed = tracks[i]
        names = ["effnet", "muq"]
        rng.shuffle(names)
        assignments.append({"seed": seed["id"], "A": names[0], "B": names[1]})
        review.extend([f"## {query_number}. {seed['artist']} — {seed['title']}", ""])
        for label, name in zip(("A", "B"), names, strict=True):
            filename = f"{query_number:02d}-{label}.m3u8"
            indices = [i, *matrices[name][i]]
            lines = ["#EXTM3U"]
            for j in indices:
                t = tracks[j]
                lines.extend(
                    [f"#EXTINF:{round(t['seconds'])},{t['artist']} - {t['title']}", t["path"]]
                )
            (listening / filename).write_text("\n".join(lines) + "\n")
            review.append(f"[{label} playlist]({filename})")
        review.append("")
        ratings.append(
            {
                "seed": seed["id"],
                "sonic_preference": None,
                "next_track_preference": None,
                "notes": "",
            }
        )
    write_json(args.run / "blind-key.json", assignments)
    write_json(listening / "ratings.json", ratings)
    (listening / "review.md").write_text("\n".join(review))
    audio_seconds = sum(track["seconds"] for track in tracks)
    lines = [
        "# MuQ versus EffNet: completed retrieval run",
        "",
        f"Both pipelines completed {len(tracks)} tracks ({audio_seconds / 3600:.2f} audio hours).",
        "",
        "| Measurement | EffNet | MuQ |",
        "| --- | ---: | ---: |",
    ]
    for label, field, scale, suffix in (
        ("Dimensions", "dimensions", 1, ""),
        ("Inference + pooling", "inference_seconds", 60, " min"),
        ("Decode + initial source check", "decode_seconds", 60, " min"),
        ("Peak process RSS (not total GPU memory)", "peak_process_rss_bytes", 1024**3, " GiB"),
        ("Neighbours in the same library folder", "same_folder_fraction", 0.01, "%"),
        ("Distinct tracks retrieved", "distinct_tracks_retrieved", 1, ""),
    ):
        values = [f"{results[name][field] / scale:.2f}{suffix}" for name in ("effnet", "muq")]
        lines.append(f"| {label} | {values[0]} | {values[1]} |")
    lines.extend(
        [
            "",
            (
                f"Mean top-five overlap: **{np.mean(overlap):.1%}** "
                f"({5 * np.mean(overlap):.2f} shared neighbours per query). "
                f"{summary['queries_with_no_shared_neighbours']} queries had no shared neighbours."
            ),
            "",
            (
                "These statistics establish that the pipelines ran and describe their differences. "
                "They do not establish musical quality, DJ usefulness, or ranking uplift. "
                "Folder categories include acquisition sources and are not genre ground truth."
            ),
            "",
            (
                f"[Start the {len(seed_indices)}-query blinded listening comparison]"
                "(listening/review.md). Record sonic and next-track preferences separately in "
                "[ratings.json](listening/ratings.json). "
                "Human judgments are pending. Leave blind-key.json closed "
                "until judging is complete."
            ),
            "",
            (
                "The manifest records the diversity sample and source identities. "
                "Recipe files record model revisions, file digests, preprocessing, "
                "pooling and package versions. MuQ uses "
                "FP32 last-layer features from consecutive 10-second chunks, "
                "frame-weighted mean pooling "
                "and final L2 normalization. EffNet uses SetTag's native full-track "
                "patch mean and L2."
            ),
            "",
            (
                "Timings exclude model loading and download. The MuQ smoke track is included. "
                "Some activity overlapped across processes, "
                "so timings are not controlled benchmarks. "
                "RSS measures process residency and does not capture all Apple GPU allocations."
            ),
            "",
            (
                "Music files and SetPath ranking were not modified. This experimental evidence was "
                "not imported into SetPath or backdated for historical replay."
            ),
        ]
    )
    (args.run / "report.md").write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "mean_top5_overlap": summary["mean_top5_overlap"],
                "models": {
                    k: {a: b for a, b in v.items() if a != "neighbours"} for k, v in results.items()
                },
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("select", "effnet", "muq", "report"))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--models", type=Path, default=Path.home() / ".cache/settag/models")
    args = parser.parse_args()
    args.run.mkdir(parents=True, exist_ok=True)
    {"select": select, "effnet": infer, "muq": infer, "report": report}[args.command](args)


if __name__ == "__main__":
    main()
