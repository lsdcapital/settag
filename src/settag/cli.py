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
from typing import Any, TextIO

from settag.analysis_worker import SubprocessAnalysisLoader
from settag.analyzer import EssentiaGenreAnalyzer, EssentiaTaskAnalyzer
from settag.catalog import MODEL_SPECS_BY_TASK
from settag.config import DEFAULT_CONFIG_PATH, load_config
from settag.journal import (
    DEFAULT_JOURNAL_DB,
    BatchRecorder,
    JournalBatch,
    JournalError,
    WriteJournal,
    default_journal_db,
)
from settag.model_store import (
    DEFAULT_MODEL_DIR,
    download_task_models,
    installed_task_manifests,
    missing_task_files,
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
    read_genre_state,
    read_owned_values,
)
from settag.tasks import AnalysisTask, parse_tasks
from settag.tui import SetTagApp
from settag.workflow import (
    AnalysisBatch,
    CancelCallback,
    MetadataBatch,
    PartialWriteError,
    PreparedWrite,
    UndoPreflight,
    analyze_paths,
    apply_prepared,
    apply_undo,
    inspect_paths,
    plan_record_for_track,
    preflight_plan,
    preflight_undo,
    prepare_track,
)

LOGGER = logging.getLogger("settag")
COMMANDS = frozenset({"run", "models", "analyze", "inspect", "preview", "apply", "undo"})


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
    _add_tasks(run, default=None)
    run.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=(f"TOML config file used for TUI defaults (default: {DEFAULT_CONFIG_PATH})."),
    )
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
    _add_journal_db(run)

    models = subparsers.add_parser("models", help="Manage Essentia model files.")
    model_commands = models.add_subparsers(dest="models_command", required=True)

    download = model_commands.add_parser(
        "download",
        help="Download the pinned model pair directly from Essentia.",
    )
    _add_model_dir(download)
    _add_tasks(download)
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
    _add_tasks(status)

    analyze = subparsers.add_parser(
        "analyze",
        help="Analyze one supported audio file or a directory recursively.",
    )
    analyze.add_argument("path", type=Path)
    _add_analysis_options(analyze)
    _add_tasks(analyze)
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
    _add_journal_db(apply)

    undo = subparsers.add_parser(
        "undo",
        help="Restore the tag values a previous SetTag write replaced.",
    )
    undo.add_argument(
        "batch",
        nargs="?",
        metavar="BATCH_ID",
        help="Write batch to revert (default: the most recent one).",
    )
    undo.add_argument(
        "--list",
        action="store_true",
        help="List recent write batches instead of reverting one.",
    )
    undo.add_argument(
        "--limit",
        type=_positive_int,
        default=10,
        help="How many batches --list shows (default: 10).",
    )
    undo.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be restored without changing any file.",
    )
    undo.add_argument(
        "--force",
        action="store_true",
        help="Restore even if a file changed after SetTag wrote it.",
    )
    undo.add_argument(
        "--yes",
        action="store_true",
        help="Revert without the single confirmation prompt.",
    )
    _add_journal_db(undo)
    return parser


def _add_journal_db(parser: argparse.ArgumentParser) -> None:
    # Resolved lazily in _journal_db so SETTAG_JOURNAL_DB is honoured at run
    # time rather than only at import time.
    parser.add_argument(
        "--journal-db",
        type=Path,
        default=None,
        help=f"Write journal database (default: {DEFAULT_JOURNAL_DB}).",
    )


def _journal_db(args: argparse.Namespace) -> WriteJournal:
    return WriteJournal(args.journal_db or default_journal_db())


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


def _add_tasks(
    parser: argparse.ArgumentParser,
    *,
    default: tuple[AnalysisTask, ...] | None = ("genre",),
) -> None:
    default_text = "genre" if default == ("genre",) else "config, then genre"
    parser.add_argument(
        "--tasks",
        type=_analysis_tasks,
        default=default,
        metavar="TASKS",
        help=(
            "Comma-separated analysis tasks: genre,mood-theme,instrument "
            f"(default: {default_text})."
        ),
    )


def _analysis_tasks(value: str) -> tuple[AnalysisTask, ...]:
    try:
        return parse_tasks(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


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
        if args.command == "undo":
            return _run_undo(args)
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
        tasks = args.tasks if args.tasks is not None else load_config(args.config).tasks
        paths = scan_audio(args.path)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 2

    if not paths:
        print("No supported audio files found.", file=sys.stderr)
        return 0

    model_dir = args.model_dir.expanduser().resolve()
    analyzer: EssentiaGenreAnalyzer | EssentiaTaskAnalyzer | None = None

    def load_analysis_in_process(
        selected_paths: Sequence[Path],
        on_progress,
        should_cancel: CancelCallback,
    ) -> AnalysisBatch:
        nonlocal analyzer
        if analyzer is None:
            analyzer = (
                EssentiaGenreAnalyzer(model_dir)
                if tasks == ("genre",)
                else EssentiaTaskAnalyzer(model_dir, tasks)
            )
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
            tasks=tasks,
        )
        config_sha256 = str(current_config["sha256"])
        expected_model_ids = {task: MODEL_SPECS_BY_TASK[task].id for task in tasks}
        store = WorkbenchStore(args.state_db)

        def load_metadata(on_progress) -> MetadataBatch:
            metadata = inspect_paths(
                paths,
                expected_model_ids=expected_model_ids,
                expected_config_sha256=config_sha256,
                expected_config=current_config,
                on_progress=on_progress,
            )
            current_paths = [track.path for track in metadata.tracks if track.status == "current"]
            if current_paths:
                store.delete(current_paths)
            cached = store.load(
                [track.path for track in metadata.tracks if track.status != "current"],
                expected_model_ids=expected_model_ids,
                expected_config_sha256=config_sha256,
                expected_config=current_config,
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

        analysis_loader = SubprocessAnalysisLoader(
            model_dir,
            tasks,
            top=args.top,
            threshold=args.threshold,
        )
        try:
            try:
                analysis_loader.start()
            except Exception as error:
                print(
                    f"Could not start the interactive analyzer: {type(error).__name__}: {error}",
                    file=sys.stderr,
                )
                return 2
            outcome = SetTagApp(
                source=args.path,
                metadata_loader=load_metadata,
                analysis_loader=analysis_loader,
                persist_plan=store.save,
                discard_plans=store.delete,
                journal=_journal_db(args),
                review_top=args.top,
                score_cutoff=args.threshold,
                analysis_tasks=tasks,
            ).run()
        finally:
            analysis_loader.close()
        if outcome is None:
            return 0
        print(outcome.message, file=sys.stderr)
        return outcome.status

    try:
        batch = load_analysis_in_process(
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
            f"  Changes:     SetTag analysis bundle ({len(item.owned_changes)} internal fields)",
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
        manifest = download_task_models(model_dir, args.tasks, force=args.force)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    if args.models_command == "status":
        missing = missing_task_files(model_dir, args.tasks)
        if missing:
            print(f"tasks: {','.join(args.tasks)}")
            print(f"directory: {model_dir}")
            print("status: missing or invalid")
            for task, files in missing.items():
                for item in files:
                    print(f"missing[{task}]: {item.filename}")
            return 1
        manifest = {
            "schema": "settag.models/v2",
            "tasks": installed_task_manifests(model_dir, args.tasks),
        }
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    raise AssertionError(f"Unhandled models command: {args.models_command}")


def _run_analyze(args: argparse.Namespace) -> int:
    if (
        args.plan
        and args.output
        and args.plan.expanduser().resolve() == args.output.expanduser().resolve()
    ):
        print("settag: --plan and --output must use different files", file=sys.stderr)
        return 2

    try:
        paths = scan_audio(args.path)
        model_dir = args.model_dir.expanduser().resolve()
        analyzer = (
            EssentiaGenreAnalyzer(model_dir)
            if args.tasks == ("genre",)
            else EssentiaTaskAnalyzer(model_dir, args.tasks)
        )
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
                print(f"  Suggested candidate: {primary.label} (model score {primary.score:.3f})")
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

        print(f"SetTag analysis bundle ({len(item.owned_changes)} internal field changes)")
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

    return _write_prepared(prepared, write_count, journal=_journal_db(args))


def _write_prepared(
    prepared: Sequence[PreparedWrite],
    write_count: int,
    *,
    journal: WriteJournal,
) -> int:
    print(file=sys.stderr)
    print(
        f"Applying {write_count} file{'s' if write_count != 1 else ''}",
        file=sys.stderr,
    )

    def progress(completed: int, total: int, path: Path) -> None:
        print(f"  [{completed}/{total}] {path}", file=sys.stderr)

    recorder = BatchRecorder(journal)
    try:
        written = apply_prepared(prepared, on_progress=progress, on_write=recorder)
    except KeyboardInterrupt:
        raise
    except PartialWriteError as error:
        print(file=sys.stderr)
        print(str(error), file=sys.stderr)
        _print_recorder_outcome(recorder)
        return 1

    print(file=sys.stderr)
    print(
        f"Done. {written} file{'s' if written != 1 else ''} written and verified.",
        file=sys.stderr,
    )
    _print_recorder_outcome(recorder)
    return 0


def _print_recorder_outcome(recorder: BatchRecorder) -> None:
    error = recorder.error
    if error is not None:
        print(f"Warning: {error}", file=sys.stderr)
    if recorder.recorded:
        print(f"Revert with: settag undo {recorder.batch_id}", file=sys.stderr)


def _run_undo(args: argparse.Namespace) -> int:
    journal = _journal_db(args)
    try:
        if args.list:
            return _print_recent_batches(journal, limit=args.limit)
        batch = journal.batch(args.batch) if args.batch else journal.latest()
    except JournalError as error:
        print(f"Cannot read the write journal: {error}", file=sys.stderr)
        return 2

    if batch is None:
        if args.batch:
            print(f"No write batch named {args.batch}", file=sys.stderr)
            print("List recent writes with: settag undo --list", file=sys.stderr)
            return 1
        print("There is nothing to undo; no SetTag writes have been journaled.", file=sys.stderr)
        return 0

    preflight = preflight_undo(batch.entries, force=args.force)
    _print_undo_summary(batch, preflight)

    if not preflight.restorable:
        print("Nothing can be restored from this batch.", file=sys.stderr)
        if preflight.blocked and not args.force:
            print("Restore anyway with --force.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("Dry run; no files were changed.", file=sys.stderr)
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("settag: undo requires an interactive terminal or --yes", file=sys.stderr)
            return 2
        if not _prompt_for_undo(len(preflight.restorable)):
            print("Cancelled; nothing changed.", file=sys.stderr)
            return 0

    def progress(completed: int, total: int, path: Path) -> None:
        print(f"  [{completed}/{total}] {path}", file=sys.stderr)

    print(file=sys.stderr)
    try:
        restored = apply_undo(preflight.restorable, on_progress=progress)
    except KeyboardInterrupt:
        raise
    except PartialWriteError as error:
        print(file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 1

    journal.mark_reverted(batch.batch_id)
    print(file=sys.stderr)
    print(
        f"Done. {restored} file{'s' if restored != 1 else ''} restored and verified.",
        file=sys.stderr,
    )
    return 0


def _print_recent_batches(journal: WriteJournal, *, limit: int) -> int:
    batches = journal.recent(limit=limit)
    if not batches:
        print("No SetTag writes have been journaled yet.", file=sys.stderr)
        return 0

    print(file=sys.stderr)
    print("Recent SetTag writes", file=sys.stderr)
    print(file=sys.stderr)
    for batch in batches:
        print(f"  {batch.batch_id}  {batch.started_at}  {batch.summary}", file=sys.stderr)
    print(file=sys.stderr)
    print("Revert one with: settag undo BATCH_ID", file=sys.stderr)
    return 0


def _print_undo_summary(batch: JournalBatch, preflight: UndoPreflight) -> None:
    print(file=sys.stderr)
    print(f"Write batch {batch.batch_id}", file=sys.stderr)
    print(f"Written {batch.started_at}", file=sys.stderr)
    if batch.reverted_at is not None:
        print(f"Already reverted {batch.reverted_at}", file=sys.stderr)
    print(file=sys.stderr)

    for entry in preflight.restorable:
        print(f"  {entry.path}", file=sys.stderr)
        for line in entry.readable_changes:
            print(f"      undo {line}", file=sys.stderr)
    for blocked in preflight.blocked:
        print(f"  {blocked.entry.path}", file=sys.stderr)
        print(f"      skipped: {blocked.reason}", file=sys.stderr)

    print(file=sys.stderr)
    restorable = len(preflight.restorable)
    print(f"  Files to restore: {restorable}", file=sys.stderr)
    if preflight.blocked:
        print(f"  Files skipped:    {len(preflight.blocked)}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "Only the SetTag metadata and staged genre edits above are rewritten.",
        file=sys.stderr,
    )
    print(
        "This restores tag values, not the original bytes; the file checksum will differ.",
        file=sys.stderr,
    )
    print(file=sys.stderr)


def _prompt_for_undo(restore_count: int) -> bool:
    return _prompt_yes_no(
        f"Restore the previous metadata on {restore_count} file{'s' if restore_count != 1 else ''}?"
    )


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
    return _prompt_yes_no(
        f"Apply this exact plan to {write_count} file{'s' if write_count != 1 else ''}?"
    )


def _prompt_yes_no(question: str) -> bool:
    while True:
        print(
            f"{question} [y] yes  [n] no > ",
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
    analyzer: Any,
    top: int,
    threshold: float,
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
    evidence = track.evidence
    genre_state = track.genre_state
    tag_plan = track.tag_plan

    record = analysis_record(
        source=source,
        analyzed_at=analyzed_at,
        backend_version=analyzer.backend_version,
        config=config,
        tasks={
            task: {
                "provenance": track.task_provenance[task],
                "predictions": [item.to_dict() for item in track.task_predictions[task]],
                "evidence": [item.to_dict() for item in track.task_evidence[task]],
                "selected": [item.to_dict() for item in track.task_selected[task]],
            }
            for task in track.task_predictions
        },
        tag_plan=tag_plan,
    )
    _log_summary(
        genre_state=genre_state,
        evidence=evidence,
        change_count=len(tag_plan.changes),
    )
    _emit(output, record)
    if plan_output is not None:
        _emit_jsonl(plan_output, plan_record_for_track(track))


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

    LOGGER.info(
        "  dry run: SetTag analysis bundle would change (%d internal fields); nothing written",
        change_count,
    )


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
