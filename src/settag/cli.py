from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import replace
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
from settag.plans import (
    PlanError,
    PlannedWrite,
    load_plan,
    plan_error_record,
)
from settag.policy import Prediction, select_predictions
from settag.records import analysis_record, config_record, error_record
from settag.scanner import scan_audio
from settag.state import DEFAULT_STATE_DB, WorkbenchStore
from settag.tags import (
    GenreState,
    apply_owned_tags,
    read_genre_state,
    read_owned_values,
)
from settag.tui import SetTagApp
from settag.workflow import (
    AnalysisBatch,
    CancelCallback,
    MetadataBatch,
    PartialWriteError,
    PreparedWrite,
    analyze_paths,
    apply_prepared,
    inspect_paths,
    plan_record_for_track,
    preflight_plan,
    prepare_track,
)

LOGGER = logging.getLogger("settag")
COMMANDS = frozenset({"run", "models", "analyze", "inspect", "preview", "apply"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="settag",
        description="Analyze audio genre metadata without changing files by default.",
        epilog='Most users can run: settag "/path/to/music"',
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run",
        help="Open the interactive app for a file or directory.",
    )
    run.add_argument("path", type=Path)
    _add_analysis_options(run)
    run.add_argument(
        "--no-tui",
        action="store_true",
        help="Print a plain dry-run summary instead of opening the app.",
    )
    run.add_argument(
        "--state-db",
        type=Path,
        default=DEFAULT_STATE_DB,
        help=f"Local TUI workbench database (default: {DEFAULT_STATE_DB}).",
    )

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
        help="Analyze one supported audio file or a directory recursively.",
    )
    analyze.add_argument("path", type=Path)
    _add_analysis_options(analyze)
    analyze.add_argument(
        "--write",
        action="store_true",
        help="Write each SetTag analysis bundle without prompting.",
    )
    analyze.add_argument(
        "--output",
        type=Path,
        help="Write the complete JSONL analysis record to this file.",
    )
    analyze.add_argument(
        "--plan",
        type=Path,
        help="Write a compact JSONL plan that can be reviewed and applied later.",
    )

    inspect = subparsers.add_parser(
        "inspect",
        help="Show the existing file genre tag and SetTag metadata without analysis.",
    )
    inspect.add_argument("path", type=Path)

    preview = subparsers.add_parser(
        "preview",
        help="Display a saved JSONL plan in a human-readable form without writing.",
    )
    preview.add_argument("plan", type=Path)

    apply = subparsers.add_parser(
        "apply",
        help="Verify and apply a compact JSONL plan without rerunning analysis.",
    )
    apply.add_argument("plan", type=Path)
    apply.add_argument(
        "--yes",
        action="store_true",
        help="Apply a valid plan without the single confirmation prompt.",
    )
    return parser


def _add_model_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=DEFAULT_MODEL_DIR,
        help=f"Model directory (default: {DEFAULT_MODEL_DIR}).",
    )


def _add_analysis_options(parser: argparse.ArgumentParser) -> None:
    _add_model_dir(parser)
    parser.add_argument(
        "--top",
        type=_positive_int,
        default=5,
        help=(
            "Maximum candidates marked for SetTag review per track; stored evidence "
            "is unaffected (default: 5)."
        ),
    )
    parser.add_argument(
        "--score-cutoff",
        "--threshold",
        dest="threshold",
        metavar="SCORE",
        type=_unit_float,
        default=0.10,
        help=(
            "Minimum model score marked for review or used as a suggestion; "
            "stored evidence is unaffected (default: 0.10)."
        ),
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
    args = build_parser().parse_args(_normalize_argv(argv))
    try:
        _configure_logging()
    except ValueError as error:
        print(f"settag: {error}", file=sys.stderr)
        return 2

    try:
        if args.command == "run":
            return _run_default(args)
        if args.command == "models":
            return _run_models(args)
        if args.command == "analyze":
            return _run_analyze(args)
        if args.command == "inspect":
            return _run_inspect(args)
        if args.command == "preview":
            return _run_preview(args)
        if args.command == "apply":
            return _run_apply(args)
        raise AssertionError(f"Unhandled command: {args.command}")
    except KeyboardInterrupt:
        print("\nsettag: interrupted", file=sys.stderr)
        return 130


def _normalize_argv(argv: Sequence[str] | None) -> list[str]:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and not values[0].startswith("-") and values[0] not in COMMANDS:
        return ["run", *values]
    return values


def _configure_logging() -> None:
    name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, name, None)
    if not isinstance(level, int):
        choices = "DEBUG, INFO, WARNING, ERROR, CRITICAL"
        raise ValueError(f"invalid LOG_LEVEL {name!r}; choose one of: {choices}")

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False


def _run_default(args: argparse.Namespace) -> int:
    try:
        paths = scan_audio(args.path)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 2

    if not paths:
        print("No supported audio files found.", file=sys.stderr)
        return 0

    model_dir = args.model_dir.expanduser().resolve()
    analyzer: EssentiaGenreAnalyzer | None = None

    def load_analysis(
        selected_paths: Sequence[Path],
        on_progress,
        should_cancel: CancelCallback,
    ) -> AnalysisBatch:
        nonlocal analyzer
        if analyzer is None:
            analyzer = EssentiaGenreAnalyzer(model_dir)
        return analyze_paths(
            selected_paths,
            analyzer=analyzer,
            top=args.top,
            threshold=args.threshold,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and not args.no_tui:
        current_config = config_record(
            top=args.top,
            threshold=args.threshold,
        )
        config_sha256 = str(current_config["sha256"])
        store = WorkbenchStore(args.state_db)

        def load_metadata(on_progress) -> MetadataBatch:
            metadata = inspect_paths(
                paths,
                expected_model_id=DISCOGS519_MAEST.id,
                expected_config_sha256=config_sha256,
                on_progress=on_progress,
            )
            current_paths = [
                track.path
                for track in metadata.tracks
                if track.status == "current"
            ]
            if current_paths:
                store.delete(current_paths)
            cached = store.load(
                [
                    track.path
                    for track in metadata.tracks
                    if track.status != "current"
                ],
                expected_model_id=DISCOGS519_MAEST.id,
                expected_config_sha256=config_sha256,
            )
            merged = []
            for track in metadata.tracks:
                entry = cached.get(track.path.expanduser().resolve())
                if entry is None:
                    merged.append(track)
                    continue

                cached_plan = replace(
                    entry.plan,
                    selected=tuple(
                        select_predictions(
                            entry.plan.evidence,
                            threshold=args.threshold,
                            top=args.top,
                        )
                    ),
                )
                cache_status = entry.status
                cache_reason = entry.reason
                if cached_plan.file_genre != track.genre_state.standard:
                    cache_status = "stale"
                    cache_reason = "file genre tag changed"
                merged.append(
                    replace(
                        track,
                        cached_plan=cached_plan,
                        cache_status=cache_status,
                        cache_reason=cache_reason,
                    )
                )
            return replace(metadata, tracks=tuple(merged))

        outcome = SetTagApp(
            source=args.path,
            metadata_loader=load_metadata,
            analysis_loader=load_analysis,
            persist_plan=store.save,
            discard_plans=store.delete,
            review_top=args.top,
            score_cutoff=args.threshold,
        ).run()
        if outcome is None:
            return 0
        print(outcome.message, file=sys.stderr)
        return outcome.status

    try:
        batch = load_analysis(
            paths,
            lambda index, total, path: LOGGER.info(
                "[%d/%d] %s",
                index,
                total,
                path,
            ),
            lambda: False,
        )
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 2
    _print_plain_batch(args.path, batch)
    return 1 if batch.failures else 0


def _print_plain_batch(source: Path, batch: AnalysisBatch) -> None:
    planned = batch.planned
    write_count = sum(bool(item.readable_changes) for item in planned)
    unchanged = len(planned) - write_count
    missing_genre = sum(not item.file_genre for item in planned)

    print(file=sys.stderr)
    print("SetTag dry run", file=sys.stderr)
    print(source.expanduser().resolve(), file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Analyzed:            {len(planned)}", file=sys.stderr)
    print(f"  Would write:         {write_count}", file=sys.stderr)
    print(f"  Already current:     {unchanged}", file=sys.stderr)
    print(f"  Without file genre:  {missing_genre}", file=sys.stderr)
    print(f"  Errors:              {len(batch.failures)}", file=sys.stderr)

    for item in planned:
        primary = item.selected[0] if item.selected else None
        standard = ", ".join(item.file_genre) or "None"
        suggestion = (
            f"{primary.label}  score {primary.score:.3f}"
            if primary is not None
            else "No selected label"
        )
        print(file=sys.stderr)
        print(item.path.name, file=sys.stderr)
        print(f"  File genre:  {standard} (unchanged)", file=sys.stderr)
        print(f"  Suggested:   {suggestion}", file=sys.stderr)
        print(
            "  Changes:     SetTag analysis bundle"
            f" ({len(item.owned_changes)} internal fields)",
            file=sys.stderr,
        )

    for failure in batch.failures:
        print(file=sys.stderr)
        print(f"Error: {failure.path}: {failure.description}", file=sys.stderr)

    print(file=sys.stderr)
    print(
        "Dry run only; nothing was written. Run in a terminal for the interactive app.",
        file=sys.stderr,
    )


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
    if args.plan and args.write:
        print(
            "settag: --plan is a dry-run artifact and cannot be combined with writing",
            file=sys.stderr,
        )
        return 2
    if (
        args.plan
        and args.output
        and args.plan.expanduser().resolve() == args.output.expanduser().resolve()
    ):
        print("settag: --plan and --output must use different files", file=sys.stderr)
        return 2

    try:
        paths = scan_audio(args.path)
        analyzer = EssentiaGenreAnalyzer(args.model_dir.expanduser().resolve())
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 2

    if not paths:
        print("No supported audio files found.", file=sys.stderr)
        return 0

    failures = 0
    planned_count = 0
    with ExitStack() as stack:
        try:
            output = (
                stack.enter_context(args.output.expanduser().resolve().open("w", encoding="utf-8"))
                if args.output
                else None
            )
            plan_output = (
                stack.enter_context(args.plan.expanduser().resolve().open("w", encoding="utf-8"))
                if args.plan
                else None
            )
        except OSError as error:
            print(f"Cannot open output: {error}", file=sys.stderr)
            return 2

        for index, path in enumerate(paths, start=1):
            LOGGER.info("[%d/%d] %s", index, len(paths), path)
            try:
                _analyze_one(
                    path,
                    analyzer=analyzer,
                    top=args.top,
                    threshold=args.threshold,
                    write=args.write,
                    output=output,
                    plan_output=plan_output,
                )
                if plan_output is not None:
                    planned_count += 1
            except Exception as error:
                failures += 1
                _emit(output, error_record(path, error))
                _emit_jsonl(plan_output, plan_error_record(path, error))
                LOGGER.error("%s: %s", path, error)

    if args.plan:
        resolved_plan = args.plan.expanduser().resolve()
        _print_saved_plan_summary(
            resolved_plan,
            planned_count,
            failures,
        )
    return 1 if failures else 0


def _print_saved_plan_summary(
    plan_path: Path,
    planned_count: int,
    failure_count: int,
) -> None:
    print(file=sys.stderr)
    print("Review plan created", file=sys.stderr)
    print(plan_path, file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Tracks analyzed:  {planned_count}", file=sys.stderr)
    print(f"  Analysis errors:  {failure_count}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        f"Preview: uv run settag preview {shlex.quote(str(plan_path))}",
        file=sys.stderr,
    )
    if failure_count:
        print(
            "This plan cannot be applied until every track analyzes successfully.",
            file=sys.stderr,
        )
    else:
        print(
            f"Apply:   uv run settag apply {shlex.quote(str(plan_path))}",
            file=sys.stderr,
        )


def _run_preview(args: argparse.Namespace) -> int:
    plan_path = args.plan.expanduser().resolve()
    try:
        planned = load_plan(plan_path)
    except (OSError, PlanError, ValueError) as error:
        print(f"Cannot preview plan: {error}", file=sys.stderr)
        return 2

    _print_plan_preview(plan_path, planned)
    return 0


def _print_plan_preview(plan_path: Path, planned: Sequence[PlannedWrite]) -> None:
    print("SetTag batch plan")
    print(plan_path)
    print()
    print(f"{len(planned)} track{'s' if len(planned) != 1 else ''}")

    for index, item in enumerate(planned, start=1):
        print()
        print(f"Track {index} of {len(planned)}")
        print(item.path.name)
        print(item.path.parent)
        print()

        print("File genre tag")
        before_genre = ", ".join(item.file_genre) or "None"
        if item.target_file_genre is not None:
            after_genre = ", ".join(item.target_file_genre) or "None"
            print(f"  {before_genre} → {after_genre} (staged)")
        else:
            print(f"  {before_genre} (will not be changed)")
            if not item.file_genre and item.selected:
                primary = item.selected[0]
                print(
                    f"  Suggested candidate: {primary.label} "
                    f"(model score {primary.score:.3f})"
                )
                print("  Candidate only; SetTag will not write the file genre tag.")
        print()

        print("SetTag model evidence")
        if item.evidence:
            width = max(len(prediction.label) for prediction in item.evidence)
            selected = set(item.selected)
            for rank, prediction in enumerate(item.evidence, start=1):
                marker = "selected" if prediction in selected else "available"
                print(
                    f"  {rank:>2}. {prediction.label:<{width}}  "
                    f"score {prediction.score:.3f}  {marker}"
                )
        else:
            print("  No ranked evidence was returned by the model.")
        print()

        print(
            "SetTag analysis bundle"
            f" ({len(item.owned_changes)} internal field changes)"
        )
        if item.readable_changes:
            for change in item.readable_changes:
                print(f"  {change}")
        else:
            print("  None")
        print()

        model = item.desired["SETTAG_MODEL"]
        analyzed_at = item.desired["SETTAG_ANALYZED_AT"]
        print("Plan details")
        print(f"  Metadata format: {item.metadata_format}")
        print(f"  Model: {model[0] if model else 'not set'}")
        print(f"  Analyzed: {analyzed_at[0] if analyzed_at else 'not set'}")

    write_count = sum(bool(item.readable_changes) for item in planned)
    evidence_count = sum(len(item.evidence) for item in planned)
    empty_file_genres = sum(not item.file_genre for item in planned)
    standard_edits = sum(item.standard_genre_change is not None for item in planned)
    print()
    print("Summary")
    print(f"  Tracks reviewed:        {len(planned)}")
    print(f"  Files to write:         {write_count}")
    print(f"  Stored evidence scores: {evidence_count}")
    print(f"  Empty file genre tags:  {empty_file_genres}")
    print(f"  Standard genre edits:   {standard_edits}")
    print()
    print("This preview reads only the saved plan; no audio files were checked or written.")
    print("Apply verifies every source and asks once before writing:")
    print(f"  uv run settag apply {shlex.quote(str(plan_path))}")


def _run_inspect(args: argparse.Namespace) -> int:
    try:
        paths = scan_audio(args.path)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 2

    if not paths:
        print("No supported audio files found.", file=sys.stderr)
        return 0

    failures = 0
    for index, path in enumerate(paths, start=1):
        LOGGER.info("[%d/%d] %s", index, len(paths), path)
        try:
            genre_state = read_genre_state(path)
            owned = read_owned_values(path)
            _log_inspection(genre_state, owned)
        except Exception as error:
            failures += 1
            LOGGER.error("%s: %s", path, error)
    return 1 if failures else 0


def _run_apply(args: argparse.Namespace) -> int:
    plan_path = args.plan.expanduser().resolve()
    try:
        planned = load_plan(plan_path)
        prepared = preflight_plan(planned)
    except (OSError, PlanError, RuntimeError, ValueError) as error:
        print(f"Cannot apply plan: {error}", file=sys.stderr)
        print("No files were written.", file=sys.stderr)
        return 2

    write_count = sum(item.has_changes for item in prepared)
    _print_batch_plan_summary(plan_path, prepared, write_count)
    if write_count == 0:
        print("Everything in this plan is already up to date.", file=sys.stderr)
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("settag: apply requires an interactive terminal or --yes", file=sys.stderr)
            return 2
        if not _prompt_for_batch_apply(write_count):
            print("Cancelled; nothing written.", file=sys.stderr)
            return 0

    try:
        prepared = preflight_plan(planned)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Plan became stale before writing: {error}", file=sys.stderr)
        print("No files were written.", file=sys.stderr)
        return 2

    return _write_prepared(prepared, write_count)


def _write_prepared(
    prepared: Sequence[PreparedWrite],
    write_count: int,
) -> int:
    print(file=sys.stderr)
    print(
        f"Applying {write_count} file{'s' if write_count != 1 else ''}",
        file=sys.stderr,
    )

    def progress(completed: int, total: int, path: Path) -> None:
        print(f"  [{completed}/{total}] {path}", file=sys.stderr)

    try:
        written = apply_prepared(prepared, on_progress=progress)
    except KeyboardInterrupt:
        raise
    except PartialWriteError as error:
        print(file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 1

    print(file=sys.stderr)
    print(
        f"Done. {written} file{'s' if written != 1 else ''} written and verified.",
        file=sys.stderr,
    )
    return 0


def _print_batch_plan_summary(
    plan_path: Path,
    prepared: Sequence[PreparedWrite],
    write_count: int,
) -> None:
    field_changes = sum(len(item.owned_plan.changes) for item in prepared)
    bundle_changes = sum(bool(item.owned_plan.changes) for item in prepared)
    standard_edits = sum(item.standard_genre_change is not None for item in prepared)
    empty_file_genres = sum(not item.item.file_genre for item in prepared)
    evidence_scores = sum(len(item.item.evidence) for item in prepared)

    print(file=sys.stderr)
    print("Batch write plan", file=sys.stderr)
    print(plan_path, file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Tracks reviewed:        {len(prepared)}", file=sys.stderr)
    print(f"  Files to write:         {write_count}", file=sys.stderr)
    print(f"  SetTag bundles:         {bundle_changes}", file=sys.stderr)
    print(f"  Internal field changes: {field_changes}", file=sys.stderr)
    print(f"  Standard genre edits:   {standard_edits}", file=sys.stderr)
    print(f"  Stored evidence scores: {evidence_scores}", file=sys.stderr)
    print(f"  Empty file genre tags:  {empty_file_genres}", file=sys.stderr)
    print(file=sys.stderr)
    print("Every source SHA-256 and metadata plan matches the reviewed file.", file=sys.stderr)
    if standard_edits:
        print(
            "Only the explicitly staged standard genre edits will change.",
            file=sys.stderr,
        )
    else:
        print("File genre tags will remain unchanged.", file=sys.stderr)
    print("Unrelated metadata will remain unchanged.", file=sys.stderr)
    print(file=sys.stderr)


def _prompt_for_batch_apply(write_count: int) -> bool:
    while True:
        print(
            f"Apply this exact plan to {write_count} file"
            f"{'s' if write_count != 1 else ''}? [y] yes  [n] no > ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        answer = sys.stdin.readline()
        if answer == "":
            print(file=sys.stderr)
            return False
        normalized = answer.strip().casefold()
        if normalized in {"y", "yes"}:
            return True
        if normalized in {"n", "no"}:
            return False
        print("Please answer y or n.", file=sys.stderr)


def _analyze_one(
    path: Path,
    *,
    analyzer: EssentiaGenreAnalyzer,
    top: int,
    threshold: float,
    write: bool,
    output: TextIO | None,
    plan_output: TextIO | None = None,
) -> None:
    track = prepare_track(
        path,
        analyzer=analyzer,
        top=top,
        threshold=threshold,
    )
    source = track.source
    analyzed_at = track.analyzed_at
    config = track.config
    predictions = track.predictions
    evidence = track.evidence
    selected = track.selected
    desired = track.desired
    genre_state = track.genre_state
    tag_plan = track.tag_plan

    if write:
        applied_plan = apply_owned_tags(
            path,
            desired,
            expected_plan=tag_plan,
            expected_standard=genre_state.standard,
        )
        if applied_plan != tag_plan:
            raise RuntimeError("Applied tag plan did not match the displayed plan")
        _verify_file_genre_tag(path, genre_state.standard)
        write_status = "written" if tag_plan.changes else "unchanged"
        write_requested = True
        result_sha256 = sha256_file(path)
    else:
        write_status = "not_requested"
        write_requested = False
        result_sha256 = None

    record = analysis_record(
        source=source,
        analyzed_at=analyzed_at,
        backend_version=analyzer.backend_version,
        model=analyzer.model_manifest,
        config=config,
        predictions=predictions,
        evidence=evidence,
        selected=selected,
        tag_plan=tag_plan,
        write_requested=write_requested,
        write_status=write_status,
        result_sha256=result_sha256,
    )
    _log_summary(
        genre_state=genre_state,
        evidence=evidence,
        change_count=len(tag_plan.changes),
        write_status=write_status,
    )
    _emit(output, record)
    if plan_output is not None:
        _emit_jsonl(plan_output, plan_record_for_track(track))


def _verify_file_genre_tag(path: Path, expected: tuple[str, ...]) -> None:
    after = read_genre_state(path)
    if after.standard != expected:
        raise RuntimeError(f"File genre tag changed unexpectedly while writing {path}")


def _log_inspection(genre_state: GenreState, owned: dict[str, list[str] | None]) -> None:
    standard = ", ".join(genre_state.standard) or "none"
    LOGGER.info("  file genre tag: %s", standard)
    LOGGER.info("  SetTag genres: %s", _format_owned_genres(genre_state, owned))

    provenance = (
        ("version", "SETTAG_VERSION"),
        ("model", "SETTAG_MODEL"),
        ("analyzed", "SETTAG_ANALYZED_AT"),
        ("config", "SETTAG_CONFIG_SHA256"),
    )
    for label, field in provenance:
        values = owned[field]
        LOGGER.info("  SetTag %s: %s", label, ", ".join(values) if values else "none")


def _format_owned_genres(
    genre_state: GenreState,
    owned: dict[str, list[str] | None],
) -> str:
    serialized = owned["SETTAG_GENRE_SCORES"]
    if serialized:
        try:
            scores = json.loads(serialized[0])
            if isinstance(scores, list):
                formatted = [
                    f"{item['label']} score {float(item['score']):.3f}"
                    for item in scores
                    if isinstance(item, dict) and "label" in item and "score" in item
                ]
                if formatted:
                    return ", ".join(formatted)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            LOGGER.warning("  SetTag score metadata is invalid; showing labels only")
    return ", ".join(genre_state.settag) or "none"


def _log_summary(
    *,
    genre_state: GenreState,
    evidence: Sequence[Prediction],
    change_count: int,
    write_status: str,
) -> None:
    standard = ", ".join(genre_state.standard) or "none"
    existing_settag = ", ".join(genre_state.settag) or "none"
    desired_labels = tuple(item.label for item in evidence)
    desired_settag = (
        ", ".join(f"{item.label} score {item.score:.3f}" for item in evidence) or "none"
    )

    LOGGER.info("  file genre tag: %s (unchanged)", standard)
    if genre_state.settag == desired_labels:
        LOGGER.info("  SetTag genres: %s (unchanged)", desired_settag)
    else:
        LOGGER.info("  SetTag genres: %s -> %s", existing_settag, desired_settag)

    if write_status == "not_requested":
        action = (
            "dry run: SetTag analysis bundle would change"
            f" ({change_count} internal fields); nothing written"
        )
    elif write_status == "written":
        action = (
            "write: SetTag analysis bundle changed and verified"
            f" ({change_count} internal fields)"
        )
    else:
        action = "write: SetTag analysis bundle already up to date"

    LOGGER.info("  %s", action)


def _emit(output: TextIO | None, record: dict[str, object]) -> None:
    serialized = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if output is not None:
        print(serialized, file=output, flush=True)
    LOGGER.debug("%s", serialized)


def _emit_jsonl(output: TextIO | None, record: dict[str, object]) -> None:
    if output is None:
        return
    serialized = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    print(serialized, file=output, flush=True)
