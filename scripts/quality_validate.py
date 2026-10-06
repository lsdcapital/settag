"""Score quality_check.py against transcodes of known-lossless tracks.

Each lossless source is cut to a 90-second excerpt, then encoded with LAME into
files that should pass (the excerpt, 320 CBR, V0) and files that should be flagged
(320 from a 128 or 192 source, FLAC from a 128 or 320 source). Needs ffmpeg and lame:
    .venv/bin/python scripts/quality_validate.py ~/Music/Library --output /tmp/validate
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from quality_check import band_levels, find_cliff, header_facts, verdict

from settag.scanner import scan_audio

EXCERPT_SECONDS = 90
# name → (expected status, steps); each step is a LAME argument list, or "flac".
VARIANTS: dict[str, tuple[str, list[Any]]] = {
    "source.wav": ("ok", []),
    "320.mp3": ("ok", [["-b", "320"]]),
    "v0.mp3": ("ok", [["-V0"]]),
    "128-to-320.mp3": ("suspect", [["-b", "128"], ["-b", "320"]]),
    "192-to-320.mp3": ("suspect", [["-b", "192"], ["-b", "320"]]),
    "128-to-flac.flac": ("suspect", [["-b", "128"], "flac"]),
    "320-to-flac.flac": ("suspect", [["-b", "320"], "flac"]),
}


def run(*args: str | Path) -> None:
    subprocess.run([str(a) for a in args], check=True, capture_output=True)


def excerpt(source: Path, target: Path) -> None:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", source],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(probe.stdout.strip() or 0)
    start = max(0.0, duration / 2 - EXCERPT_SECONDS / 2)
    # 16-bit 44.1 kHz stereo, the input LAME and a CD-quality FLAC both expect.
    run("ffmpeg", "-v", "error", "-y", "-ss", f"{start:.1f}", "-t", str(EXCERPT_SECONDS),
        "-i", source, "-ar", "44100", "-ac", "2", "-sample_fmt", "s16", target)  # fmt: skip


def build(source_wav: Path, name: str, steps: list[Any], work: Path) -> Path:
    current = source_wav
    for index, step in enumerate(steps):
        last = index == len(steps) - 1
        if step == "flac":
            out = work / name
            run("ffmpeg", "-v", "error", "-y", "-i", current, out)
        else:
            out = work / name if last else work / f"{name}.{index}.mp3"
            run("lame", "--quiet", *step, current, out)
        if not last and out.suffix == ".mp3":
            decoded = work / f"{name}.{index}.wav"
            run("lame", "--quiet", "--decode", out, decoded)
            out = decoded
        current = out
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="A directory containing lossless tracks")
    parser.add_argument("--output", type=Path, required=True, help="Report directory")
    parser.add_argument("--limit", type=int, default=40, help="Lossless sources to use")
    args = parser.parse_args()
    sources = []
    for path in scan_audio(args.path):
        try:
            facts = header_facts(path)
        except Exception:
            continue
        # Sources are not prefiltered by the check: a lossless source it flags is either
        # a false positive or a fake in the library, and the report should show which.
        if facts["lossless"]:
            sources.append(path)
        if len(sources) >= args.limit:
            break
    rows: list[dict[str, Any]] = []
    levels: list[np.ndarray] = []
    with tempfile.TemporaryDirectory() as tmp:
        for index, source in enumerate(sources, 1):
            work = Path(tmp) / str(index)
            work.mkdir()
            source_wav = work / "source.wav"
            try:
                excerpt(source, source_wav)
            except subprocess.CalledProcessError as error:
                print(f"skip {source.name}: {error.stderr!r}", flush=True)
                continue
            for name, (expected, steps) in VARIANTS.items():
                built = build(source_wav, name, steps, work)
                facts = header_facts(built)
                levels.append(band_levels(built, facts["sample_rate"]))
                cliff = find_cliff(levels[-1])
                status, reasons = verdict(facts, cliff)
                rows.append(
                    {
                        "source": str(source),
                        "variant": name,
                        "expected": expected,
                        "status": status,
                        "facts": facts,
                        "cliff": cliff,
                        "reasons": reasons,
                    }
                )
            print(f"{index}/{len(sources)} · {source.name}", flush=True)
    by_variant: dict[str, Counter[str]] = {}
    for row in rows:
        by_variant.setdefault(row["variant"], Counter())[row["status"]] += 1
    clean = [r for r in rows if r["expected"] == "ok"]
    fake = [r for r in rows if r["expected"] == "suspect"]
    summary = {
        "sources": len(sources),
        "false_positive_rate": round(sum(r["status"] != "ok" for r in clean) / len(clean), 3),
        "detection_rate": round(sum(r["status"] == "suspect" for r in fake) / len(fake), 3),
        "by_variant": {k: dict(v) for k, v in by_variant.items()},
    }
    args.output.mkdir(parents=True, exist_ok=True)
    # Saved so thresholds can be retuned without re-encoding: row i is levels[i].
    np.save(args.output / "levels.npy", np.stack(levels))
    (args.output / "validate.json").write_text(
        json.dumps({**summary, "rows": rows}, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
