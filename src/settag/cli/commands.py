"""Command dispatch and the work each SetTag command performs."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from settag.analysis_worker import SubprocessAnalysisLoader
from settag.analyzer import EssentiaGenreAnalyzer, EssentiaTaskAnalyzer
from settag.catalog import MODEL_SPECS_BY_TASK
from settag.cli.args import COMMANDS, build_parser
from settag.cli.render import (
    LOGGER,
    _configure_logging,
    _emit,
    _emit_jsonl,
    _log_inspection,
    _log_summary,
    _print_batch_plan_summary,
    _print_plain_batch,
    _print_plan_preview,
    _print_recent_batches,
    _print_recorder_outcome,
    _print_saved_plan_summary,
    _print_undo_summary,
    _prompt_for_batch_apply,
    _prompt_for_undo,
)
from settag.config import SetTagConfig, load_config
from settag.journal import BatchRecorder, JournalError, WriteJournal, default_journal_db
from settag.model_store import (
    download_task_models,
    installed_task_manifests,
    missing_task_files,
)
from settag.plans import PlanError, load_plan, plan_error_record
from settag.policy import select_predictions
from settag.records import analysis_record, config_record, error_record
from settag.scanner import scan_audio
from settag.state import WorkbenchStore
from settag.tags import read_genre_state, read_owned_values
from settag.tui import SetTagApp
from settag.workflow import (
    AnalysisBatch,
    CancelCallback,
    MetadataBatch,
    PartialWriteError,
    PreparedWrite,
    analyze_paths,
    apply_prepared,
    apply_undo,
    inspect_paths,
    plan_record_for_track,
    preflight_plan,
    preflight_undo,
    prepare_track,
    summarize_writes,
)


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


def _journal_db(args: argparse.Namespace) -> WriteJournal:
    return WriteJournal(args.journal_db or default_journal_db())


def _run_default(args: argparse.Namespace) -> int:
    try:
        # Read the file only when something is still unset, so explicit flags keep
        # working against a config this build cannot parse.
        needed = args.tasks is None or args.genre_sample is None
        configured = load_config(args.config) if needed else SetTagConfig()
        tasks = args.tasks if args.tasks is not None else configured.tasks
        sample = args.genre_sample if args.genre_sample is not None else configured.genre_sample
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
                EssentiaGenreAnalyzer(model_dir, sample=sample)
                if tasks == ("genre",)
                else EssentiaTaskAnalyzer(model_dir, tasks, sample=sample)
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
            genre_sample=sample,
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
            sample=sample,
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
            EssentiaGenreAnalyzer(model_dir, sample=args.genre_sample)
            if args.tasks == ("genre",)
            else EssentiaTaskAnalyzer(model_dir, args.tasks, sample=args.genre_sample)
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


def _run_preview(args: argparse.Namespace) -> int:
    plan_path = args.plan.expanduser().resolve()
    try:
        planned = load_plan(plan_path)
    except (OSError, PlanError, ValueError) as error:
        print(f"Cannot preview plan: {error}", file=sys.stderr)
        return 2

    _print_plan_preview(plan_path, planned)
    return 0


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
            _log_inspection(genre_state, owned, scores=args.scores)
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

    summary = summarize_writes(prepared)
    write_count = summary.write_count
    _print_batch_plan_summary(plan_path, summary)
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
        if not _prompt_for_undo(preflight.restore_count):
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
