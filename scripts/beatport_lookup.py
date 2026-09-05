"""Bounded MP3-only, read-only Beatport experiment. Not part of the tagging CLI.

Run with the project environment and a JSON list of explicitly selected file paths:
    .venv/bin/python scripts/beatport_lookup.py --paths sample.json --output /tmp/lookup
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mutagen.mp3 import MP3

from settag.beatport import (
    LookupStopped,
    PublicPageProvider,
    TrackIdentity,
    decide,
    identity_conflicts,
    normalized,
)
from settag.tags import OWNED_DESCRIPTIONS, task_evidence_from_owned


def read_identity(path: Path) -> tuple[TrackIdentity, tuple[str, ...], list[dict[str, Any]]]:
    if path.suffix.lower() != ".mp3":
        raise ValueError("This prototype supports MP3 only")
    audio = MP3(path)
    tags = audio.tags

    def values(key: str) -> tuple[str, ...]:
        frame = tags.get(key) if tags is not None else None
        return tuple(str(value).strip() for value in getattr(frame, "text", ()))

    def first(key: str) -> str:
        return next(iter(values(key)), "")

    identity = TrackIdentity(
        title=first("TIT2"),
        artists=values("TPE1"),
        duration_seconds=getattr(audio.info, "length", None),
        isrc=first("TSRC"),
        beatport_id=first("TXXX:BEATPORT_TRACK_ID"),
    )
    owned = {key: list(values(f"TXXX:{key}")) or None for key in OWNED_DESCRIPTIONS}
    predictions = task_evidence_from_owned(owned).get("genre", ())
    return identity, values("TCON"), [asdict(p) for p in predictions[:3]]


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def write_report(output: Path, report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = [
        "# Beatport read-only lookup experiment",
        "",
        (
            f"Selected: {report['selected']} · HTTP requests: {report['requests']} · "
            f"Cache hits: {report['cache_hits']} · Elapsed: {report['elapsed_seconds']:.1f}s"
        ),
        "",
        f"Results: {report['counts']}",
        "",
        (
            "Proposals are identity-check results, not verified accuracy. Manual reference "
            "labels have not been assigned. Blocked/unattempted tracks are not catalog misses."
        ),
        "",
        f"Batch stop: {report['stopped_reason'] or 'None'}",
        "",
        "No tag writes or inference. Model predictions were already stored in the files.",
        "",
        "| File | Existing genre | Model first choice | Beatport proposal | Result |",
        "| --- | --- | --- | --- | --- |",
    ]
    sources = []
    for row in report["tracks"]:
        model = row.get("model_predictions", [])
        proposal = row.get("proposal")
        cells = [
            Path(row["path"]).name,
            ", ".join(row.get("existing_genres", [])),
            f"{model[0]['label']} ({model[0]['score']:.3f})" if model else "",
            ", ".join(proposal["genres"]) if proposal else "",
            row["status"] + (f" · {row['existing_comparison']}" if proposal else ""),
        ]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
        if proposal:
            sources.append(f"Source for {_cell(Path(row['path']).name)}: {proposal['url']}")
    lines.extend(["", *sources])
    (output / "report.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths", type=Path, required=True, help="JSON array of selected MP3 paths"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Reports and cached public pages"
    )
    parser.add_argument("--offline", action="store_true", help="Use cached pages only")
    parser.add_argument("--limit", type=int, default=30, choices=range(1, 51), metavar="1..50")
    args = parser.parse_args()
    raw_paths = json.loads(args.paths.read_text())
    if not isinstance(raw_paths, list) or not all(isinstance(p, str) for p in raw_paths):
        parser.error("--paths must contain a JSON array of path strings")
    paths = list(dict.fromkeys(Path(p).expanduser().resolve() for p in raw_paths))[: args.limit]
    if not paths:
        parser.error("No tracks selected")
    provider = PublicPageProvider(args.output / "cache", offline=args.offline)
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    stopped = ""
    for index, path in enumerate(paths, 1):
        row: dict[str, Any] = {"path": str(path), "manual_verdict": None}
        try:
            identity, genres, predictions = read_identity(path)
            row.update(
                identity=asdict(identity), existing_genres=genres, model_predictions=predictions
            )
            if stopped:
                row.update(status="not_attempted", reason=stopped)
            elif not identity.title or not identity.artists:
                row.update(status="unresolved", reason="Missing artist/title; no query issued")
            else:
                before = path.stat()
                candidates = provider.candidates(identity)
                decision = decide(identity, candidates)
                row.update(
                    status=decision.status,
                    reason=decision.reason,
                    candidates=[
                        {**asdict(c), "url": c.url, "conflicts": identity_conflicts(identity, c)}
                        for c in candidates
                    ],
                )
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    row.update(status="source_changed", reason="File changed during lookup")
                elif decision.candidate:
                    row["proposal"] = {**asdict(decision.candidate), "url": decision.candidate.url}
                    row["existing_comparison"] = (
                        "missing"
                        if not genres
                        else "agrees"
                        if {normalized(g) for g in genres}
                        == {normalized(g) for g in decision.candidate.genres}
                        else "differs"
                    )
        except LookupStopped as error:
            stopped = str(error)
            row.update(status="lookup_stopped", reason=stopped)
        except Exception as error:
            row.update(status="read_or_lookup_error", reason=f"{type(error).__name__}: {error}")
        rows.append(row)
        print(f"{index}/{len(paths)} · {row['status']} · {path.name}", flush=True)
    report = {
        "selected": len(paths),
        "counts": dict(Counter(row["status"] for row in rows)),
        "requests": provider.requests,
        "cache_hits": provider.cache_hits,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stopped_reason": stopped,
        "manual_accuracy": None,
        "tracks": rows,
    }
    write_report(args.output, report)
    print(f"Report: {args.output / 'report.md'}", flush=True)
    return 2 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
