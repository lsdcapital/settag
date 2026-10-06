"""Read-only audio quality experiment. Not part of the tagging CLI.

Flags lossy files presented as lossless, high-bitrate files transcoded from a lower
bitrate, and low-bitrate or low-sample-rate files, then writes a JSON/Markdown report:
    .venv/bin/python scripts/quality_check.py ~/Music/Disco --output /tmp/quality
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import Counter
from pathlib import Path
from typing import Any

import mutagen
import numpy as np
from mutagen.mp3 import BitrateMode

from settag.scanner import scan_audio

WINDOWS = 6
WINDOW_SECONDS = 10.0
FRAME = 8192
BAND_HZ = 100.0
# Cliffs below this are left alone: real mixes roll off gently there, and no
# common encoder lowpasses that low at a bitrate a DJ would keep.
SEARCH_FROM_HZ = 12_000.0
# A lowpass is a steep wall with near-nothing above it; natural rolloff is neither.
# Tuned with quality_validate.py (see docs/quality-check.md); 18/20 missed a tenth
# of known transcodes, and 12/12 flagged genuine lossless sources.
MIN_STEEP_DB = 14.0
MIN_DROP_DB = 16.0
# LAME 320 measures at 19.8-20.1 kHz. Resampling a 48 kHz master to 44.1 kHz leaves
# its own filter at 20.5-21 kHz, which must not read as a lossy wall.
LOSSLESS_MAX_CUTOFF_HZ = 20_400.0
# LAME 256 kbps and V0 sit around 19.5-19.7 kHz; 192 kbps around 18.6 kHz.
HIGH_BITRATE_MIN_CUTOFF_HZ = 18_800.0
HIGH_BITRATE_KBPS = 256
# Average-bitrate floor for VBR MP3 and AAC: V0 and iTunes 256 VBR often average
# 180-240 kbps on sparse material, so only clearly low averages are flagged.
VBR_FLOOR_KBPS = 160
# Rough LAME/iTunes lowpass points, for a readable guess at the source.
SOURCE_GUESSES = (
    (16_500.0, "≤128 kbps (16 kHz wall)"),
    (17_800.0, "~128-160 kbps"),
    (19_000.0, "~192 kbps"),
    (20_000.0, "~224-256 kbps or VBR V0"),
    (LOSSLESS_MAX_CUTOFF_HZ, "~320 kbps"),
)
LOSSLESS_TYPES = {"FLAC", "WAVE", "AIFF"}


def header_facts(path: Path) -> dict[str, Any]:
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("Unrecognised audio container")
    info = audio.info
    kind = type(audio).__name__
    codec = str(getattr(info, "codec", "") or "")
    lossless = kind in LOSSLESS_TYPES or codec == "alac"
    encoder = [
        str(getattr(info, "encoder_info", "") or ""),
        str(getattr(info, "encoder_settings", "") or ""),
        str(getattr(audio.tags, "vendor", "") or ""),
    ]
    if audio.tags is not None:
        # Each tag format names the encoder field differently, and Vorbis comments
        # raise on keys outside their character set, such as MP4's ©too.
        for key in ("TSSE", "ENCODER", "©too"):
            try:
                value = audio.tags.get(key)
            except (KeyError, ValueError):
                continue
            values = getattr(value, "text", value) or ()
            encoder.extend(str(v) for v in ([values] if isinstance(values, str) else values))
    mode = getattr(info, "bitrate_mode", None)
    return {
        "container": kind,
        "codec": codec or kind.lower(),
        "lossless": lossless,
        "bitrate_kbps": round((getattr(info, "bitrate", 0) or 0) / 1000),
        # Mutagen reports no mode for AAC, which iTunes and most stores encode as VBR.
        "vbr": mode in {BitrateMode.VBR, BitrateMode.ABR} or codec.startswith("mp4a"),
        "sample_rate": int(getattr(info, "sample_rate", 0) or 0),
        "bits_per_sample": getattr(info, "bits_per_sample", None),
        "duration_seconds": round(float(getattr(info, "length", 0.0) or 0.0), 1),
        "encoder": " · ".join(dict.fromkeys(e.strip() for e in encoder if e.strip())),
    }


def band_levels(path: Path, sample_rate: int) -> np.ndarray:
    """Average power per 100 Hz band, in dB, over a few windows across the track."""
    import essentia.standard as standard  # noqa: PLC0415
    from essentia import log  # noqa: PLC0415

    log.warningActive = False
    # Loading at the file's own rate skips resampling, which would erase the
    # very spectrum this checks. Mono is fine: encoders lowpass both channels.
    audio = vars(standard)["MonoLoader"](filename=str(path), sampleRate=sample_rate)()
    length = int(WINDOW_SECONDS * sample_rate)
    span = len(audio) - length
    # Skip the first and last tenth: intros and outros are often sparse.
    starts = [0] if span <= 0 else np.linspace(0.1 * span, 0.9 * span, WINDOWS).astype(int)
    window = np.hanning(FRAME)
    power = np.zeros(FRAME // 2 + 1)
    frames = 0
    for start in starts:
        chunk = audio[start : start + length]
        for offset in range(0, len(chunk) - FRAME + 1, FRAME // 2):
            power += np.abs(np.fft.rfft(chunk[offset : offset + FRAME] * window)) ** 2
            frames += 1
    if not frames:
        raise ValueError("Track is too short to measure")
    power /= frames
    freqs = np.fft.rfftfreq(FRAME, 1 / sample_rate)
    bands = int(freqs[-1] // BAND_HZ)
    index = np.minimum((freqs // BAND_HZ).astype(int), bands - 1)
    totals = np.bincount(index, weights=power, minlength=bands)
    counts = np.bincount(index, minlength=bands)
    return 10 * np.log10(np.maximum(totals / np.maximum(counts, 1), 1e-14))


def find_cliff(
    levels: np.ndarray, min_steep_db: float = MIN_STEEP_DB, min_drop_db: float = MIN_DROP_DB
) -> dict[str, Any]:
    """The highest-contrast wall above SEARCH_FROM_HZ, or none.

    `steep_db` compares the 300 Hz just below a candidate edge with 300 Hz just above
    it (skipping one band of transition); `drop_db` compares the level just below with
    the loudest stretch anywhere above, so a dip followed by real content is no wall.
    `nearest` is the steepest edge whether or not it passed, for tuning the thresholds.
    """
    best: dict[str, Any] = {"cutoff_hz": None, "steep_db": 0.0, "drop_db": 0.0}
    nearest: dict[str, Any] = {"hz": None, "steep_db": 0.0, "drop_db": 0.0}
    top = len(levels) - 2  # the last bands hug Nyquist and are unreliable
    for edge in range(int(SEARCH_FROM_HZ // BAND_HZ), top - 4):
        below = float(np.mean(levels[edge - 3 : edge]))
        steep = below - float(np.mean(levels[edge + 1 : edge + 4]))
        drop = below - float(np.percentile(levels[edge + 1 : top], 90))
        measured = {"steep_db": round(steep, 1), "drop_db": round(drop, 1)}
        if steep > nearest["steep_db"]:
            nearest = {"hz": edge * BAND_HZ, **measured}
        if steep >= min_steep_db and drop >= min_drop_db and drop > best["drop_db"]:
            best = {"cutoff_hz": edge * BAND_HZ, **measured}
    return {**best, "nearest": nearest}


def verdict(facts: dict[str, Any], cliff: dict[str, Any]) -> tuple[str, list[str]]:
    reasons = []
    cutoff = cliff.get("cutoff_hz")
    if cutoff is not None:
        guess = next((label for limit, label in SOURCE_GUESSES if cutoff <= limit), None)
        if facts["lossless"] and cutoff <= LOSSLESS_MAX_CUTOFF_HZ:
            reasons.append(f"lossless container with a lossy wall at {cutoff / 1000:.1f} kHz")
        elif (
            not facts["lossless"]
            and facts["bitrate_kbps"] >= HIGH_BITRATE_KBPS
            and cutoff < HIGH_BITRATE_MIN_CUTOFF_HZ
        ):
            reasons.append(
                f"{facts['bitrate_kbps']} kbps file with a wall at {cutoff / 1000:.1f} kHz"
            )
        if reasons and guess:
            reasons.append(f"source looks like {guess}")
    if not facts["lossless"]:
        floor = VBR_FLOOR_KBPS if facts["vbr"] else HIGH_BITRATE_KBPS
        if facts["bitrate_kbps"] < floor:
            reasons.append(f"low bitrate ({facts['bitrate_kbps']} kbps)")
    if facts["sample_rate"] and facts["sample_rate"] < 44_100:
        reasons.append(f"low sample rate ({facts['sample_rate']} Hz)")
    if facts["bits_per_sample"] and facts["bits_per_sample"] < 16:
        reasons.append(f"{facts['bits_per_sample']}-bit audio")
    # The encoder string is reported but never a reason: Beatport's own downloads
    # carry ffmpeg's Lavf marker, so it cannot tell a rip from a store purchase.
    return ("suspect" if reasons else "ok"), reasons


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def write_report(output: Path, report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = [
        "# Audio quality experiment",
        "",
        (
            f"Selected: {report['selected']} of {report['found']} · "
            f"Elapsed: {report['elapsed_seconds']:.1f}s · "
            f"Mean per track: {report['mean_track_seconds']:.2f}s"
        ),
        "",
        f"Results: {report['counts']}",
        "",
        (
            "A wall is a heuristic, not proof: some masters lowpass on purpose. Confirm "
            "suspects in a spectrogram (Spek) and record the call in `manual_verdict`."
        ),
        "",
        "No tag writes. Files are only read.",
        "",
        "| File | Format | Wall | Result | Reasons |",
        "| --- | --- | --- | --- | --- |",
    ]
    order = {"suspect": 0, "error": 1, "ok": 2}
    for row in sorted(report["tracks"], key=lambda r: (order[r["status"]], r["path"])):
        facts = row.get("facts", {})
        cutoff = row.get("cliff", {}).get("cutoff_hz")
        cells = [
            Path(row["path"]).name,
            (
                f"{facts['codec']} {facts['bitrate_kbps']} kbps"
                + (" VBR" if facts.get("vbr") else "")
                if facts
                else ""
            ),
            f"{cutoff / 1000:.1f} kHz" if cutoff else "",
            row["status"],
            "; ".join(row.get("reasons", [])),
        ]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    (output / "report.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="A track or a directory to scan")
    parser.add_argument("--output", type=Path, required=True, help="Report directory")
    parser.add_argument("--limit", type=int, default=200, help="Tracks to check (default 200)")
    parser.add_argument(
        "--seed", type=int, default=0, help="Sampling seed when a directory has more tracks"
    )
    args = parser.parse_args()
    found = scan_audio(args.path)
    if not found:
        parser.error("No supported audio files found")
    paths = found
    if len(found) > args.limit:
        # A random sample, because sorted order would check one artist folder only.
        paths = sorted(random.Random(args.seed).sample(found, args.limit))
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    for index, path in enumerate(paths, 1):
        row: dict[str, Any] = {"path": str(path), "manual_verdict": None}
        track_started = time.monotonic()
        try:
            facts = header_facts(path)
            cliff = find_cliff(band_levels(path, facts["sample_rate"] or 44_100))
            status, reasons = verdict(facts, cliff)
            row.update(facts=facts, cliff=cliff, status=status, reasons=reasons)
        except Exception as error:
            row.update(status="error", reasons=[f"{type(error).__name__}: {error}"])
        row["seconds"] = round(time.monotonic() - track_started, 3)
        rows.append(row)
        print(f"{index}/{len(paths)} · {row['status']} · {path.name}", flush=True)
    elapsed = time.monotonic() - started
    report = {
        "found": len(found),
        "selected": len(paths),
        "seed": args.seed,
        "counts": dict(Counter(row["status"] for row in rows)),
        "elapsed_seconds": round(elapsed, 3),
        "mean_track_seconds": round(elapsed / len(paths), 3),
        "manual_accuracy": None,
        "tracks": rows,
    }
    write_report(args.output, report)
    print(f"Report: {args.output / 'report.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
