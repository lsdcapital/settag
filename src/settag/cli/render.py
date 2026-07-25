"""Everything the CLI prints, prompts, or logs.

Layout only. Counts and phrases about what SetTag will do or did come from the
domain layer (``workflow.WriteSummary``, ``journal.WriteRecord.readable_changes``)
so the CLI and the app cannot describe the same batch differently.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from settag.journal import BatchRecorder, JournalBatch, WriteJournal
from settag.plans import PlannedWrite
from settag.policy import Prediction
from settag.tags import GenreState
from settag.workflow import AnalysisBatch, UndoPreflight, WriteSummary

LOGGER = logging.getLogger("settag")


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


def _print_recorder_outcome(recorder: BatchRecorder) -> None:
    error = recorder.error
    if error is not None:
        print(f"Warning: {error}", file=sys.stderr)
    if recorder.recorded:
        print(f"Revert with: settag undo {recorder.batch_id}", file=sys.stderr)


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
    print(f"  Files to restore: {preflight.restore_count}", file=sys.stderr)
    if preflight.blocked_count:
        print(f"  Files skipped:    {preflight.blocked_count}", file=sys.stderr)
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


def _print_batch_plan_summary(
    plan_path: Path,
    summary: WriteSummary,
) -> None:
    print(file=sys.stderr)
    print("Batch write plan", file=sys.stderr)
    print(plan_path, file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Tracks reviewed:        {summary.track_count}", file=sys.stderr)
    print(f"  Files to write:         {summary.write_count}", file=sys.stderr)
    print(f"  SetTag bundles:         {summary.bundle_changes}", file=sys.stderr)
    print(f"  Internal field changes: {summary.field_changes}", file=sys.stderr)
    print(f"  Standard genre edits:   {summary.standard_genre_edits}", file=sys.stderr)
    print(f"  Stored evidence scores: {summary.evidence_scores}", file=sys.stderr)
    print(f"  Empty file genre tags:  {summary.empty_file_genres}", file=sys.stderr)
    print(file=sys.stderr)
    print("Every source SHA-256 and metadata plan matches the reviewed file.", file=sys.stderr)
    if summary.standard_genre_edits:
        print(
            "Only the explicitly staged standard genre edits will change.",
            file=sys.stderr,
        )
    else:
        print("File genre tags will remain unchanged.", file=sys.stderr)
    print("Unrelated metadata will remain unchanged.", file=sys.stderr)
    print(file=sys.stderr)


def _prompt_for_undo(restore_count: int) -> bool:
    return _prompt_yes_no(
        f"Restore the previous metadata on {restore_count} file{'s' if restore_count != 1 else ''}?"
    )


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
