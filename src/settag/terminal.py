from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from rich import box
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from settag.plans import PlannedWrite

GuidedAction = Literal["view", "write", "save", "quit"]


def terminal_console(*, stderr: bool = True) -> Console:
    return Console(
        stderr=stderr,
        highlight=False,
        no_color="NO_COLOR" in os.environ,
    )


def analysis_progress(console: Console, total: int) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", style="progress.description", markup=False),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        disable=not console.is_terminal,
        transient=True,
    )


def print_guided_header(console: Console, source: Path, track_count: int) -> None:
    console.print()
    console.rule("[bold cyan]SetTag")
    console.print(Text(str(source.expanduser().resolve()), style="dim"))
    noun = "track" if track_count == 1 else "tracks"
    console.print(f"Found [bold]{track_count}[/bold] supported {noun}.")
    console.print()


def print_guided_summary(
    console: Console,
    planned: Sequence[PlannedWrite],
    failures: Sequence[tuple[Path, str]],
) -> None:
    write_count = sum(bool(item.readable_changes) for item in planned)
    unchanged_count = len(planned) - write_count
    empty_genres = sum(not item.file_genre for item in planned)

    summary = Table(
        title="Analysis summary",
        box=box.ROUNDED,
        show_header=False,
        pad_edge=False,
    )
    summary.add_column(style="dim")
    summary.add_column(justify="right", style="bold")
    summary.add_row("Analyzed", str(len(planned)))
    summary.add_row("Would write", str(write_count))
    summary.add_row("Already current", str(unchanged_count))
    summary.add_row("Without file genre", str(empty_genres))
    summary.add_row("Errors", str(len(failures)), style="red" if failures else None)
    console.print(summary)

    if planned:
        tracks = Table(
            title="Tracks",
            box=box.SIMPLE_HEAD,
            show_lines=False,
            expand=True,
        )
        tracks.add_column("Track", overflow="ellipsis", ratio=3)
        tracks.add_column("File genre", overflow="ellipsis", ratio=2)
        tracks.add_column("Primary model evidence", overflow="ellipsis", ratio=3)
        tracks.add_column("Score", justify="right", width=7)
        tracks.add_column("Changes", justify="right", width=7)

        visible = planned[:20]
        for item in visible:
            primary = item.selected[0] if item.selected else None
            file_genre = ", ".join(item.file_genre) or "None"
            tracks.add_row(
                Text(item.path.name),
                Text(file_genre, style=None if item.file_genre else "dim"),
                Text(primary.label if primary else "No selected label"),
                f"{primary.score:.3f}" if primary else "—",
                str(len(item.readable_changes)),
            )
        console.print(tracks)
        if len(planned) > len(visible):
            console.print(
                f"[dim]Showing the first {len(visible)} of {len(planned)} tracks. "
                "Choose view for every track.[/dim]"
            )

    for path, message in failures:
        console.print(Text(f"Error: {path}: {message}", style="bold red"))

    console.print(
        "[dim]Only SetTag-owned metadata may be written. "
        "File genre tags and unrelated metadata remain unchanged.[/dim]"
    )


def print_plan_details(console: Console, planned: Sequence[PlannedWrite]) -> None:
    for index, item in enumerate(planned, start=1):
        console.print()
        console.rule(f"[bold]Track {index} of {len(planned)}[/bold]")
        console.print(Text(item.path.name, style="bold"))
        console.print(Text(str(item.path.parent), style="dim"))
        console.print()

        genre = ", ".join(item.file_genre) or "None"
        console.print("[bold]File genre tag[/bold]")
        console.print(Text(f"  {genre} (will not be changed)"))
        if not item.file_genre and item.selected:
            primary = item.selected[0]
            console.print(
                Text(
                    f"  Suggested candidate: {primary.label} "
                    f"(model score {primary.score:.3f})"
                )
            )
            console.print(
                Text("  Candidate only; SetTag will not write the file genre tag.", style="dim")
            )
        console.print()

        evidence = Table(
            title="SetTag model evidence",
            box=box.SIMPLE_HEAD,
            show_lines=False,
        )
        evidence.add_column("#", justify="right", style="dim")
        evidence.add_column("Label")
        evidence.add_column("Score", justify="right")
        if item.selected:
            for rank, prediction in enumerate(item.selected, start=1):
                evidence.add_row(str(rank), Text(prediction.label), f"{prediction.score:.3f}")
        else:
            evidence.add_row("—", Text("No labels met the selection threshold.", style="dim"), "—")
        console.print(evidence)

        console.print("[bold]Metadata changes[/bold]")
        if item.readable_changes:
            for change in item.readable_changes:
                console.print(Text(f"  • {change}"))
        else:
            console.print("  None")


def prompt_guided_action(
    console: Console,
    *,
    can_write: bool,
) -> GuidedAction:
    choices = "[v] view  "
    if can_write:
        choices += "[w] write  "
    choices += "[s] save plan  [q] quit"

    while True:
        console.print()
        console.print(Text(choices))
        console.print(Text("Choice [q]: "), end="")
        answer = sys.stdin.readline()
        if answer == "":
            console.print()
            return "quit"
        normalized = answer.strip().casefold()
        if normalized in {"", "q", "quit"}:
            return "quit"
        if normalized in {"v", "view"}:
            return "view"
        if normalized in {"s", "save"}:
            return "save"
        if can_write and normalized in {"w", "write"}:
            return "write"
        console.print("[yellow]Please choose one of the displayed actions.[/yellow]")


def confirm_guided_write(console: Console, write_count: int) -> bool:
    noun = "file" if write_count == 1 else "files"
    console.print()
    prompt = Text("Write SetTag-owned metadata to ")
    prompt.append(str(write_count), style="bold")
    prompt.append(f" {noun}? [y/N] ")
    console.print(prompt, end="")
    answer = sys.stdin.readline()
    if answer == "":
        console.print()
        return False
    return answer.strip().casefold() in {"y", "yes"}
