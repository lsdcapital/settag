from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, cast

AnalysisTask = Literal["genre", "mood-theme", "instrument"]

TASK_ORDER: tuple[AnalysisTask, ...] = ("genre", "mood-theme", "instrument")
TASK_SET = frozenset(TASK_ORDER)

TASK_FIELDS: dict[AnalysisTask, tuple[str, str]] = {
    "genre": ("SETTAG_GENRE", "SETTAG_GENRE_SCORES"),
    "mood-theme": ("SETTAG_MOOD_THEME", "SETTAG_MOOD_THEME_SCORES"),
    "instrument": ("SETTAG_INSTRUMENT", "SETTAG_INSTRUMENT_SCORES"),
}


def parse_tasks(value: str) -> tuple[AnalysisTask, ...]:
    requested = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(requested) - TASK_SET)
    if unknown:
        choices = ", ".join(TASK_ORDER)
        raise ValueError(f"unknown analysis task(s): {', '.join(unknown)}; choose from {choices}")
    if not requested:
        raise ValueError("at least one analysis task is required")
    return ordered_tasks(requested)


def ordered_tasks(tasks: Iterable[str]) -> tuple[AnalysisTask, ...]:
    selected = set(tasks)
    return tuple(task for task in TASK_ORDER if task in selected)


def task_name(value: str) -> AnalysisTask:
    if value not in TASK_SET:
        raise ValueError(f"unknown analysis task: {value}")
    return cast(AnalysisTask, value)
