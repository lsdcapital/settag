from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import TextIO

from settag.analyzer import EssentiaGenreAnalyzer
from settag.catalog import DISCOGS519_MAEST
from settag.hashing import sha256_file
from settag.model_store import (
    DEFAULT_MODEL_DIR,
    download_models,
    installed_manifest,
    missing_files,
)
from settag.policy import select_predictions
from settag.records import analysis_record, config_record, error_record, source_record, utc_now
from settag.scanner import scan_mp3
from settag.tags import apply_owned_tags, build_owned_values, plan_owned_tags


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="settag",
        description="Analyze MP3 genre metadata without changing files by default.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    models = subparsers.add_parser("models", help="Manage Essentia model files.")
    model_commands = models.add_subparsers(dest="models_command", required=True)

    download = model_commands.add_parser(
        "download",
        help="Download the pinned model pair directly from Essentia.",
    )
    _add_model_dir(download)
    download.add_argument(
        "--force",
        action="store_true",
        help="Download and replace files that already exist.",
    )

    status = model_commands.add_parser(
        "status",
        help="Show whether all required model files are installed.",
    )
    _add_model_dir(status)

    analyze = subparsers.add_parser(
        "analyze",
        help="Analyze one MP3 or a directory recursively.",
    )
    analyze.add_argument("path", type=Path)
    _add_model_dir(analyze)
    analyze.add_argument(
        "--top",
        type=_positive_int,
        default=5,
        help="Maximum selected genres per track (default: 5).",
    )
    analyze.add_argument(
        "--threshold",
        type=_unit_float,
        default=0.10,
        help="Minimum model activation required for selection (default: 0.10).",
    )
    analyze.add_argument(
        "--write",
        action="store_true",
        help="Apply the displayed plan to settag-owned TXXX fields.",
    )
    analyze.add_argument(
        "--output",
        type=Path,
        help="Write JSONL to this file instead of stdout.",
    )
    return parser


def _add_model_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=DEFAULT_MODEL_DIR,
        help=f"Model directory (default: {DEFAULT_MODEL_DIR}).",
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _unit_float(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "models":
        return _run_models(args)
    if args.command == "analyze":
        return _run_analyze(args)
    raise AssertionError(f"Unhandled command: {args.command}")


def _run_models(args: argparse.Namespace) -> int:
    model_dir = args.model_dir.expanduser().resolve()
    if args.models_command == "download":
        manifest = download_models(model_dir, force=args.force)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    if args.models_command == "status":
        missing = missing_files(model_dir)
        if missing:
            print(f"model: {DISCOGS519_MAEST.id}")
            print(f"directory: {model_dir}")
            print("status: missing")
            for item in missing:
                print(f"missing: {item.filename}")
            return 1
        print(json.dumps(installed_manifest(model_dir), indent=2, sort_keys=True))
        return 0

    raise AssertionError(f"Unhandled models command: {args.models_command}")


def _run_analyze(args: argparse.Namespace) -> int:
    try:
        paths = scan_mp3(args.path)
        analyzer = EssentiaGenreAnalyzer(args.model_dir.expanduser().resolve())
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 2

    if not paths:
        print("No MP3 files found.", file=sys.stderr)
        return 0

    try:
        output_context = (
            args.output.expanduser().resolve().open("w", encoding="utf-8")
            if args.output
            else nullcontext(sys.stdout)
        )
    except OSError as error:
        print(f"Cannot open output: {error}", file=sys.stderr)
        return 2

    failures = 0
    with output_context as output:
        for index, path in enumerate(paths, start=1):
            print(f"[{index}/{len(paths)}] {path}", file=sys.stderr)
            try:
                _analyze_one(
                    path,
                    analyzer=analyzer,
                    top=args.top,
                    threshold=args.threshold,
                    write=args.write,
                    output=output,
                )
            except Exception as error:
                failures += 1
                _emit(output, error_record(path, error))
                print(f"error: {path}: {error}", file=sys.stderr)

    return 1 if failures else 0


def _analyze_one(
    path: Path,
    *,
    analyzer: EssentiaGenreAnalyzer,
    top: int,
    threshold: float,
    write: bool,
    output: TextIO,
) -> None:
    source = source_record(path)
    analyzed_at = utc_now()
    config = config_record(top=top, threshold=threshold)
    predictions = analyzer.analyze(path)
    selected = select_predictions(predictions, threshold=threshold, top=top)
    desired = build_owned_values(
        selected,
        model_id=analyzer.spec.id,
        analyzed_at=analyzed_at,
        config_sha256=str(config["sha256"]),
    )
    changes = plan_owned_tags(path, desired)

    if write:
        applied = apply_owned_tags(path, desired)
        if applied != changes:
            raise RuntimeError("Tag state changed between planning and writing")
        write_status = "written" if changes else "unchanged"
        result_sha256 = sha256_file(path)
    else:
        write_status = "not_requested"
        result_sha256 = None

    record = analysis_record(
        source=source,
        analyzed_at=analyzed_at,
        backend_version=analyzer.backend_version,
        model=analyzer.model_manifest,
        config=config,
        predictions=predictions,
        selected=selected,
        changes=changes,
        write_requested=write,
        write_status=write_status,
        result_sha256=result_sha256,
    )
    _emit(output, record)


def _emit(output: TextIO, record: dict[str, object]) -> None:
    print(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        file=output,
        flush=True,
    )
