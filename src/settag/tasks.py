from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

AnalysisTask = Literal["genre", "mood-theme", "instrument", "mood-aggressive"]

TASK_ORDER: tuple[AnalysisTask, ...] = ("genre", "mood-theme", "instrument", "mood-aggressive")
TASK_SET = frozenset(TASK_ORDER)
# What runs when neither a flag nor the config names tasks. Both mood tasks share one
# Discogs-EffNet pass (18 MB, ~1.2 s a track) beside MAEST's 348 MB, so they cost little
# on top of genre. Instrument stays opt-in.
DEFAULT_TASKS: tuple[AnalysisTask, ...] = ("genre", "mood-theme", "mood-aggressive")

TASK_FIELDS: dict[AnalysisTask, tuple[str, str]] = {
    "genre": ("SETTAG_GENRE", "SETTAG_GENRE_SCORES"),
    "mood-theme": ("SETTAG_MOOD_THEME", "SETTAG_MOOD_THEME_SCORES"),
    "instrument": ("SETTAG_INSTRUMENT", "SETTAG_INSTRUMENT_SCORES"),
    "mood-aggressive": ("SETTAG_MOOD_AGGRESSIVE", "SETTAG_MOOD_AGGRESSIVE_SCORES"),
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


def checked_expected_models(
    expected_model_ids: Mapping[AnalysisTask, str],
) -> dict[AnalysisTask, str]:
    if not expected_model_ids:
        raise ValueError("at least one expected analysis model is required")
    return dict(expected_model_ids)


def task_name(value: str) -> AnalysisTask:
    if value not in TASK_SET:
        raise ValueError(f"unknown analysis task: {value}")
    return value
