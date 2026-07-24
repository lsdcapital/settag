from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from rich.console import Console
from rich.text import Text

from settag import __version__
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
    friendly_change,
    load_plan,
    plan_error_record,
    plan_record,
)
from settag.policy import Prediction, select_predictions
from settag.records import analysis_record, config_record, error_record, source_record, utc_now
from settag.scanner import scan_audio
from settag.tags import (
    GenreState,
    OwnedValues,
    TagPlan,
    apply_owned_tags,
    build_owned_values,
    plan_owned_tags,
    read_genre_state,
    read_owned_values,
)
from settag.terminal import (
    analysis_progress,
    confirm_guided_write,
    print_guided_header,
    print_guided_summary,
    print_plan_details,
    prompt_guided_action,
    terminal_console,
)

LOGGER = logging.getLogger("settag")
ReviewDecision = Literal["write", "decline", "quit", "interrupt"]
ReviewPrompt = Callable[[Path], ReviewDecision]
AnalyzeControl = Literal["continue", "quit", "interrupt"]
COMMANDS = frozenset({"run", "models", "analyze", "inspect", "preview", "apply"})


@dataclass(frozen=True)
class PreparedTrack:
    source: dict[str, object]
    analyzed_at: str
    config: dict[str, object]
    predictions: list[Prediction]
    selected: list[Prediction]
    desired: OwnedValues
    genre_state: GenreState
    tag_plan: TagPlan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="settag",
        description="Analyze audio genre metadata without changing files by default.",
        epilog='Most users can run: settag "/path/to/music"',
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    guided = subparsers.add_parser(
        "run",
        help="Analyze a path in a guided, human-readable workflow.",
    )
    guided.add_argument("path", type=Path)
    _add_analysis_options(guided)

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
    write_mode = analyze.add_mutually_exclusive_group()
    write_mode.add_argument(
        "--write",
        action="store_true",
        help="Apply every displayed plan without prompting.",
    )
    write_mode.add_argument(
        "--review",
        action="store_true",
        help="Review each displayed plan and confirm before writing.",
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
        help="Maximum selected genres per track (default: 5).",
    )
    parser.add_argument(
        "--threshold",
        type=_unit_float,
        default=0.10,
        help="Minimum model activation required for selection (default: 0.10).",
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
            return _run_guided(args)
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


def _run_guided(args: argparse.Namespace) -> int:
    console = terminal_console()
    try:
        paths = scan_audio(args.path)
        analyzer = EssentiaGenreAnalyzer(args.model_dir.expanduser().resolve())
    except Exception as error:
        console.print(Text(str(error), style="bold red"))
        return 2

    if not paths:
        console.print("No supported audio files found.")
        return 0

    print_guided_header(console, args.path, len(paths))
    planned: list[PlannedWrite] = []
    plan_records: list[dict[str, object]] = []
    failures: list[tuple[Path, str]] = []

    if not console.is_terminal:
        console.print(f"Analyzing {len(paths)} track{'s' if len(paths) != 1 else ''}…")

    progress = analysis_progress(console, len(paths))
    with progress:
        task = progress.add_task("Preparing analysis", total=len(paths))
        for path in paths:
            progress.update(task, description=path.name)
            try:
                track = _prepare_track(
                    path,
                    analyzer=analyzer,
                    top=args.top,
                    threshold=args.threshold,
                )
                planned.append(_planned_write_for_track(track))
                plan_records.append(_plan_record_for_track(track))
            except Exception as error:
                failures.append((path, f"{type(error).__name__}: {error}"))
                plan_records.append(plan_error_record(path, error))
            finally:
                progress.advance(task)

    print_guided_summary(console, planned, failures)
    if len(planned) == 1:
        print_plan_details(console, planned)

    failure_status = 1 if failures else 0
    if not sys.stdin.isatty():
        console.print()
        console.print("[dim]Non-interactive session: nothing was written.[/dim]")
        return failure_status

    can_write = not failures and any(item.readable_changes for item in planned)
    if not can_write and not failures:
        console.print()
        console.print("[green]Everything is already up to date; nothing to write.[/green]")
        return 0

    while True:
        action = prompt_guided_action(console, can_write=can_write)
        if action == "view":
            print_plan_details(console, planned)
            continue
        if action == "save":
            try:
                saved_path = _save_guided_plan(plan_records)
            except OSError as error:
                console.print(Text(f"Could not save plan: {error}", style="bold red"))
                continue
            console.print()
            console.print("Plan saved:")
            console.print(Text(str(saved_path), style="bold cyan"))
            continue
        if action == "quit":
            console.print()
            console.print("[dim]Nothing was written.[/dim]")
            return failure_status

        return _guided_write(planned, console)


def _guided_write(planned: Sequence[PlannedWrite], console: Console) -> int:
    try:
        prepared = _preflight_plan(planned)
    except (OSError, RuntimeError, ValueError) as error:
        console.print(Text(f"Cannot write: {error}", style="bold red"))
        console.print("[dim]No files were written.[/dim]")
        return 2

    write_count = sum(bool(tag_plan.changes) for _item, _state, tag_plan in prepared)
    console.print()
    console.print("[green]Every source and metadata plan passed preflight.[/green]")
    if not confirm_guided_write(console, write_count):
        console.print()
        console.print("[dim]Cancelled; nothing written.[/dim]")
        return 0

    try:
        prepared = _preflight_plan(planned)
    except (OSError, RuntimeError, ValueError) as error:
        console.print(Text(f"Plan became stale before writing: {error}", style="bold red"))
        console.print("[dim]No files were written.[/dim]")
        return 2

    return _write_prepared(prepared, write_count, console=console)


def _save_guided_plan(records: Sequence[dict[str, object]]) -> Path:
    stamp = (
        utc_now()
        .replace("-", "")
        .replace(":", "")
        .replace("T", "-")
        .removesuffix("Z")
    )
    candidate = Path.cwd() / f"settag-plan-{stamp}.jsonl"
    suffix = 2
    while candidate.exists():
        candidate = Path.cwd() / f"settag-plan-{stamp}-{suffix}.jsonl"
        suffix += 1

    with candidate.open("x", encoding="utf-8") as output:
        for record in records:
            _emit_jsonl(output, record)
    return candidate.resolve()


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
    if args.plan and (args.write or args.review):
        print(
            "settag: --plan is a dry-run artifact and cannot be combined with writing",
            file=sys.stderr,
        )
        return 2
    if args.review and not sys.stdin.isatty():
        print("settag: --review requires an interactive terminal", file=sys.stderr)
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
    control: AnalyzeControl = "continue"
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
            if not args.review:
                LOGGER.info("[%d/%d] %s", index, len(paths), path)
            try:
                control = _analyze_one(
                    path,
                    analyzer=analyzer,
                    top=args.top,
                    threshold=args.threshold,
                    write=args.write,
                    review=args.review,
                    review_index=index,
                    review_total=len(paths),
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
            if control != "continue":
                break

    if args.plan:
        resolved_plan = args.plan.expanduser().resolve()
        _print_saved_plan_summary(
            resolved_plan,
            planned_count,
            failures,
        )
    if control == "interrupt":
        return 130
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
        if item.file_genre:
            print(f"  {', '.join(item.file_genre)} (will not be changed)")
        else:
            print("  None (will not be changed)")
            if item.selected:
                primary = item.selected[0]
                print(
                    f"  Suggested candidate: {primary.label} "
                    f"(model score {primary.score:.3f})"
                )
                print("  Candidate only; SetTag will not write the file genre tag.")
        print()

        print("SetTag model evidence")
        if item.selected:
            width = max(len(prediction.label) for prediction in item.selected)
            for rank, prediction in enumerate(item.selected, start=1):
                print(
                    f"  {rank:>2}. {prediction.label:<{width}}  "
                    f"score {prediction.score:.3f}"
                )
        else:
            print("  No labels met the selection threshold.")
        print()

        change_count = len(item.readable_changes)
        noun = "change" if change_count == 1 else "changes"
        print(f"Metadata {noun} ({change_count})")
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
    selected_count = sum(len(item.selected) for item in planned)
    empty_file_genres = sum(not item.file_genre for item in planned)
    print()
    print("Summary")
    print(f"  Tracks reviewed:        {len(planned)}")
    print(f"  Files to write:         {write_count}")
    print(f"  Selected label scores:  {selected_count}")
    print(f"  Empty file genre tags:  {empty_file_genres}")
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
        prepared = _preflight_plan(planned)
    except (OSError, PlanError, RuntimeError, ValueError) as error:
        print(f"Cannot apply plan: {error}", file=sys.stderr)
        print("No files were written.", file=sys.stderr)
        return 2

    write_count = sum(bool(tag_plan.changes) for _item, _state, tag_plan in prepared)
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
        prepared = _preflight_plan(planned)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Plan became stale before writing: {error}", file=sys.stderr)
        print("No files were written.", file=sys.stderr)
        return 2

    return _write_prepared(prepared, write_count)


def _write_prepared(
    prepared: Sequence[tuple[PlannedWrite, GenreState, TagPlan]],
    write_count: int,
    *,
    console: Console | None = None,
) -> int:
    def output(message: str = "") -> None:
        if console is None:
            print(message, file=sys.stderr)
        else:
            console.print(Text(message))

    written = 0
    output()
    output(f"Applying {write_count} file{'s' if write_count != 1 else ''}")
    try:
        for item, genre_state, tag_plan in prepared:
            if not tag_plan.changes:
                continue
            if sha256_file(item.path) != item.source_sha256:
                raise RuntimeError(f"Source changed before its write: {item.path}")
            written += 1
            output(f"  [{written}/{write_count}] {item.path}")
            applied = apply_owned_tags(
                item.path,
                item.desired,
                expected_plan=tag_plan,
                expected_standard=genre_state.standard,
            )
            if applied != tag_plan:
                raise RuntimeError(f"Applied tag plan differed for {item.path}")
            _verify_file_genre_tag(item.path, genre_state.standard)
    except KeyboardInterrupt:
        output(f"\nInterrupted after {written} of {write_count} planned writes.")
        raise
    except Exception as error:
        output(f"\nStopped after {written} of {write_count} planned writes: {error}")
        return 1

    output()
    output(f"Done. {written} file{'s' if written != 1 else ''} written and verified.")
    return 0


def _preflight_plan(
    planned: Sequence[PlannedWrite],
) -> list[tuple[PlannedWrite, GenreState, TagPlan]]:
    prepared: list[tuple[PlannedWrite, GenreState, TagPlan]] = []
    errors: list[str] = []
    for item in planned:
        try:
            if not item.path.is_file():
                raise RuntimeError(f"file is missing: {item.path}")
            if sha256_file(item.path) != item.source_sha256:
                raise RuntimeError(f"source SHA-256 changed: {item.path}")
            genre_state = read_genre_state(item.path)
            if genre_state.standard != item.file_genre:
                raise RuntimeError(f"file genre tag changed: {item.path}")
            current_plan = plan_owned_tags(item.path, item.desired)
            if current_plan.format != item.metadata_format:
                raise RuntimeError(f"metadata format changed: {item.path}")
            readable_changes = tuple(friendly_change(change) for change in current_plan.changes)
            if readable_changes != item.readable_changes:
                raise RuntimeError(f"planned metadata changes do not match: {item.path}")
            prepared.append((item, genre_state, current_plan))
        except Exception as error:
            errors.append(str(error))
    if errors:
        details = "\n  ".join(errors)
        raise RuntimeError(f"{len(errors)} stale or invalid track(s):\n  {details}")
    return prepared


def _print_batch_plan_summary(
    plan_path: Path,
    prepared: Sequence[tuple[PlannedWrite, GenreState, TagPlan]],
    write_count: int,
) -> None:
    field_changes = sum(len(tag_plan.changes) for _item, _state, tag_plan in prepared)
    empty_file_genres = sum(not item.file_genre for item, _state, _plan in prepared)
    selected_labels = sum(len(item.selected) for item, _state, _plan in prepared)

    print(file=sys.stderr)
    print("Batch write plan", file=sys.stderr)
    print(plan_path, file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Tracks reviewed:        {len(prepared)}", file=sys.stderr)
    print(f"  Files to write:         {write_count}", file=sys.stderr)
    print(f"  SetTag field changes:   {field_changes}", file=sys.stderr)
    print(f"  Selected label scores:  {selected_labels}", file=sys.stderr)
    print(f"  Empty file genre tags:  {empty_file_genres}", file=sys.stderr)
    print(file=sys.stderr)
    print("Every source SHA-256 and metadata plan matches the reviewed file.", file=sys.stderr)
    print("File genre tags and unrelated metadata will remain unchanged.", file=sys.stderr)
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


def _prepare_track(
    path: Path,
    *,
    analyzer: EssentiaGenreAnalyzer,
    top: int,
    threshold: float,
) -> PreparedTrack:
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
    genre_state = read_genre_state(path)
    tag_plan = plan_owned_tags(path, desired)
    return PreparedTrack(
        source=source,
        analyzed_at=analyzed_at,
        config=config,
        predictions=predictions,
        selected=selected,
        desired=desired,
        genre_state=genre_state,
        tag_plan=tag_plan,
    )


def _plan_record_for_track(track: PreparedTrack) -> dict[str, object]:
    model_id = track.desired["SETTAG_MODEL"]
    config_sha256 = track.desired["SETTAG_CONFIG_SHA256"]
    settag_version = track.desired["SETTAG_VERSION"]
    return plan_record(
        source=track.source,
        genre_state=track.genre_state,
        selected=track.selected,
        tag_plan=track.tag_plan,
        readable_changes=[friendly_change(change) for change in track.tag_plan.changes],
        model_id=model_id[0] if model_id else "unknown",
        analyzed_at=track.analyzed_at,
        settag_version=settag_version[0] if settag_version else __version__,
        config_sha256=config_sha256[0] if config_sha256 else "unknown",
    )


def _planned_write_for_track(track: PreparedTrack) -> PlannedWrite:
    return PlannedWrite(
        path=Path(str(track.source["path"])),
        source_sha256=str(track.source["sha256"]),
        source_size=int(track.source["size"]),
        source_mtime_ns=int(track.source["mtime_ns"]),
        file_genre=track.genre_state.standard,
        selected=tuple(track.selected),
        desired=track.desired,
        metadata_format=track.tag_plan.format,
        readable_changes=tuple(
            friendly_change(change) for change in track.tag_plan.changes
        ),
    )


def _analyze_one(
    path: Path,
    *,
    analyzer: EssentiaGenreAnalyzer,
    top: int,
    threshold: float,
    write: bool,
    output: TextIO | None,
    plan_output: TextIO | None = None,
    review: bool = False,
    review_index: int = 1,
    review_total: int = 1,
    prompt: ReviewPrompt | None = None,
) -> AnalyzeControl:
    track = _prepare_track(
        path,
        analyzer=analyzer,
        top=top,
        threshold=threshold,
    )
    source = track.source
    analyzed_at = track.analyzed_at
    config = track.config
    predictions = track.predictions
    selected = track.selected
    desired = track.desired
    genre_state = track.genre_state
    tag_plan = track.tag_plan

    control: AnalyzeControl = "continue"
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
    elif review:
        _print_review_header(review_index, review_total, path)
        if tag_plan.changes:
            _print_review_plan(
                genre_state=genre_state,
                selected=selected,
                tag_plan=tag_plan,
            )
            decision = (prompt or _prompt_for_write)(path)
            if decision == "write":
                applied_plan = apply_owned_tags(
                    path,
                    desired,
                    expected_plan=tag_plan,
                    expected_standard=genre_state.standard,
                )
                if applied_plan != tag_plan:
                    raise RuntimeError("Applied tag plan did not match the displayed plan")
                _verify_file_genre_tag(path, genre_state.standard)
                write_status = "written"
                write_requested = True
                result_sha256 = sha256_file(path)
                _print_review_result("Written and verified.")
            else:
                write_requested = False
                result_sha256 = None
                if decision == "decline":
                    write_status = "declined"
                    _print_review_result("Skipped; nothing written.")
                elif decision == "quit":
                    write_status = "cancelled"
                    control = "quit"
                    _print_review_result("Review ended; nothing written.")
                else:
                    write_status = "interrupted"
                    control = "interrupt"
                    _print_review_result("Interrupted; nothing written.")
        else:
            write_status = "unchanged"
            write_requested = True
            result_sha256 = sha256_file(path)
            _print_review_plan(
                genre_state=genre_state,
                selected=selected,
                tag_plan=tag_plan,
            )
            _print_review_result("Already up to date; nothing written.")
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
        selected=selected,
        tag_plan=tag_plan,
        write_requested=write_requested,
        write_status=write_status,
        result_sha256=result_sha256,
    )
    if not review:
        _log_summary(
            genre_state=genre_state,
            selected=selected,
            change_count=len(tag_plan.changes),
            write_status=write_status,
        )
    _emit(output, record)
    if plan_output is not None:
        _emit_jsonl(plan_output, _plan_record_for_track(track))
    return control


def _print_review_header(index: int, total: int, path: Path) -> None:
    print(file=sys.stderr)
    print(f"Review {index} of {total}", file=sys.stderr)
    print(path.name, file=sys.stderr)
    print(path.parent, file=sys.stderr)
    print(file=sys.stderr)


def _print_review_plan(
    *,
    genre_state: GenreState,
    selected: Sequence[Prediction],
    tag_plan: TagPlan,
) -> None:
    print("File genre tag", file=sys.stderr)
    if genre_state.standard:
        print(f"  {', '.join(genre_state.standard)} (will not be changed)", file=sys.stderr)
    else:
        print("  None", file=sys.stderr)
        if selected:
            primary = selected[0]
            print(
                f"  Suggested candidate: {primary.label} (model score {primary.score:.3f})",
                file=sys.stderr,
            )
        else:
            print("  No selected model candidate.", file=sys.stderr)
        print("  Candidate only; SetTag will not write the file genre tag.", file=sys.stderr)
    print(file=sys.stderr)

    print("SetTag model evidence", file=sys.stderr)
    if selected:
        width = max(len(item.label) for item in selected)
        for index, item in enumerate(selected, start=1):
            print(
                f"  {index:>2}. {item.label:<{width}}  score {item.score:.3f}",
                file=sys.stderr,
            )
    else:
        print("  No labels met the selection threshold.", file=sys.stderr)

    desired_labels = tuple(item.label for item in selected)
    if genre_state.settag == desired_labels:
        print("  These ranked labels are already stored.", file=sys.stderr)
    elif genre_state.settag:
        print(f"  Replaces {len(genre_state.settag)} existing SetTag label(s).", file=sys.stderr)
    else:
        print("  No SetTag labels are currently stored.", file=sys.stderr)
    print(file=sys.stderr)

    count = len(tag_plan.changes)
    noun = "change" if count == 1 else "changes"
    print(f"Metadata {noun} ({count})", file=sys.stderr)
    if tag_plan.changes:
        for change in tag_plan.changes:
            print(f"  {friendly_change(change)}", file=sys.stderr)
    else:
        print("  None", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "Only SetTag-owned metadata can change; "
        "the file genre tag and unrelated tags are preserved.",
        file=sys.stderr,
    )
    print(file=sys.stderr)


def _print_review_result(message: str) -> None:
    print(file=sys.stderr)
    print(message, file=sys.stderr)


def _prompt_for_write(path: Path) -> ReviewDecision:
    while True:
        print(
            "Write this SetTag metadata? [y] write  [n] skip  [q] quit > ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        try:
            answer = sys.stdin.readline()
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return "interrupt"
        if answer == "":
            print(file=sys.stderr)
            return "quit"
        normalized = answer.strip().casefold()
        if normalized in {"y", "yes"}:
            return "write"
        if normalized in {"n", "no"}:
            return "decline"
        if normalized in {"q", "quit"}:
            return "quit"
        print(f"Please answer y, n, or q for {path.name}.", file=sys.stderr)


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
    selected: Sequence[Prediction],
    change_count: int,
    write_status: str,
) -> None:
    standard = ", ".join(genre_state.standard) or "none"
    existing_settag = ", ".join(genre_state.settag) or "none"
    desired_labels = tuple(item.label for item in selected)
    desired_settag = (
        ", ".join(f"{item.label} score {item.score:.3f}" for item in selected) or "none"
    )

    LOGGER.info("  file genre tag: %s (unchanged)", standard)
    if genre_state.settag == desired_labels:
        LOGGER.info("  SetTag genres: %s (unchanged)", desired_settag)
    else:
        LOGGER.info("  SetTag genres: %s -> %s", existing_settag, desired_settag)

    fields = f"{change_count} SetTag field{'s' if change_count != 1 else ''}"
    if write_status == "not_requested":
        action = f"dry run: {fields} would change; nothing written"
    elif write_status == "written":
        action = f"write: {fields} changed and verified"
    else:
        action = "write: SetTag fields already up to date"

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
