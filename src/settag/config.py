from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # ty: ignore[unresolved-import]

from settag.tasks import AnalysisTask, ordered_tasks


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SetTagConfig:
    tasks: tuple[AnalysisTask, ...] = ("genre",)


def default_config_path() -> Path:
    override = os.environ.get("SETTAG_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (base / "settag" / "config.toml").resolve()


DEFAULT_CONFIG_PATH = default_config_path()


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> SetTagConfig:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return SetTagConfig()
    if not resolved.is_file():
        raise ConfigError(f"SetTag config is not a file: {resolved}")

    try:
        with resolved.open("rb") as source:
            value = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"Could not read SetTag config {resolved}: {error}") from error

    analysis = value.get("analysis", {})
    if not isinstance(analysis, dict):
        raise ConfigError(f"{resolved}: [analysis] must be a TOML table")
    raw_tasks: Any = analysis.get("tasks", ["genre"])
    if (
        not isinstance(raw_tasks, list)
        or not raw_tasks
        or not all(isinstance(task, str) and task for task in raw_tasks)
    ):
        raise ConfigError(f"{resolved}: analysis.tasks must be a non-empty array of task names")

    requested = tuple(raw_tasks)
    selected = ordered_tasks(requested)
    unknown = sorted(set(requested) - set(selected))
    if unknown:
        choices = "genre, mood-theme, instrument"
        raise ConfigError(
            f"{resolved}: unknown analysis task(s): {', '.join(unknown)}; choose from {choices}"
        )
    return SetTagConfig(tasks=selected)
