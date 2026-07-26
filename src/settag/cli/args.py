"""Command-line grammar for SetTag.

Parsing only: no command does work here, so the accepted surface can be read
in one place.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from settag import __version__
from settag.config import DEFAULT_CONFIG_PATH
from settag.journal import DEFAULT_JOURNAL_DB
from settag.model_store import DEFAULT_MODEL_DIR
from settag.state import DEFAULT_STATE_DB
from settag.tasks import AnalysisTask, parse_tasks

COMMANDS = frozenset({"run", "models", "analyze", "inspect", "preview", "apply", "undo"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="settag",
        description="Analyze audio genre metadata without changing files by default.",
        epilog='Most users can run: settag "/path/to/music"',
    )
    # The same string SetTag stamps into SETTAG_VERSION, so a tagged file can
    # always be traced back to the build that wrote it.
    parser.add_argument(
        "--version",
        action="version",
        version=f"settag {__version__}",
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
