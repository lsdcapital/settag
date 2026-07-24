from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from settag.policy import Prediction
from settag.tags import GenreState, OwnedValues, TagChange, TagPlan

PLAN_SCHEMA = "settag.plan/v2"
LEGACY_PLAN_SCHEMA = "settag.plan/v1"
PLAN_ERROR_SCHEMA = "settag.plan-error/v1"


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class PlannedWrite:
    path: Path
    source_sha256: str
    source_size: int
    source_mtime_ns: int
    file_genre: tuple[str, ...]
    selected: tuple[Prediction, ...]
    desired: OwnedValues
    metadata_format: str
    owned_changes: tuple[str, ...]
    target_file_genre: tuple[str, ...] | None = None

    @property
    def readable_changes(self) -> tuple[str, ...]:
        standard_change = self.standard_genre_change
        if standard_change is None:
            return self.owned_changes
        return (*self.owned_changes, friendly_standard_genre_change(standard_change))

    @property
    def standard_genre_change(self) -> TagChange | None:
        if self.target_file_genre is None or self.target_file_genre == self.file_genre:
            return None
        return TagChange(
            field=_standard_genre_field(self.metadata_format),
            before=list(self.file_genre) or None,
            after=list(self.target_file_genre) or None,
        )


def stage_file_genre(
    item: PlannedWrite,
    genres: tuple[str, ...] | None,
) -> PlannedWrite:
    """Return a plan with an explicit conventional genre edit staged.

    ``None`` preserves the original file genre. An empty tuple explicitly
    removes it. Staging the original value collapses back to no edit.
    """
    target = None if genres is None or genres == item.file_genre else genres
    return replace(item, target_file_genre=target)


def plan_record(
    *,
    source: dict[str, object],
    genre_state: GenreState,
    selected: list[Prediction],
    tag_plan: TagPlan,
    readable_changes: list[str],
    model_id: str,
    analyzed_at: str,
    settag_version: str,
    config_sha256: str,
    target_file_genre: tuple[str, ...] | None = None,
) -> dict[str, object]:
    standard_change = (
        TagChange(
            field=_standard_genre_field(tag_plan.format),
            before=list(genre_state.standard) or None,
            after=list(target_file_genre) or None,
        )
        if target_file_genre is not None and target_file_genre != genre_state.standard
        else None
    )
    return {
        "schema": PLAN_SCHEMA,
        "path": source["path"],
        "source": {
            "sha256": source["sha256"],
            "size": source["size"],
            "mtime_ns": source["mtime_ns"],
        },
        "file_genre": list(genre_state.standard),
        "target_file_genre": (
            list(target_file_genre) if target_file_genre is not None else None
        ),
        "selected": [item.to_dict() for item in selected],
        "metadata_format": tag_plan.format,
        "provenance": {
            "settag_version": settag_version,
            "model": model_id,
            "analyzed_at": analyzed_at,
            "config_sha256": config_sha256,
        },
        "changes": {
            "settag": readable_changes,
            "file_genre": (
                friendly_standard_genre_change(standard_change)
                if standard_change is not None
                else None
            ),
        },
    }


def planned_write_record(item: PlannedWrite) -> dict[str, object]:
    model = item.desired["SETTAG_MODEL"]
    analyzed_at = item.desired["SETTAG_ANALYZED_AT"]
    version = item.desired["SETTAG_VERSION"]
    config = item.desired["SETTAG_CONFIG_SHA256"]
    return {
        "schema": PLAN_SCHEMA,
        "path": str(item.path),
        "source": {
            "sha256": item.source_sha256,
            "size": item.source_size,
            "mtime_ns": item.source_mtime_ns,
        },
        "file_genre": list(item.file_genre),
        "target_file_genre": (
            list(item.target_file_genre)
            if item.target_file_genre is not None
            else None
        ),
        "selected": [prediction.to_dict() for prediction in item.selected],
        "metadata_format": item.metadata_format,
        "provenance": {
            "settag_version": version[0] if version else "unknown",
            "model": model[0] if model else "unknown",
            "analyzed_at": analyzed_at[0] if analyzed_at else "unknown",
            "config_sha256": config[0] if config else "unknown",
        },
        "changes": {
            "settag": list(item.owned_changes),
            "file_genre": (
                friendly_standard_genre_change(item.standard_genre_change)
                if item.standard_genre_change is not None
                else None
            ),
        },
    }


def plan_error_record(path: Path, error: BaseException) -> dict[str, object]:
    return {
        "schema": PLAN_ERROR_SCHEMA,
        "path": str(path.expanduser().resolve()),
        "error": f"{type(error).__name__}: {error}",
    }


def load_plan(path: Path) -> list[PlannedWrite]:
    resolved = path.expanduser().resolve()
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PlanError(f"Cannot read plan {resolved}: {error}") from error

    planned: list[PlannedWrite] = []
    seen_paths: set[Path] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise PlanError(f"{resolved}:{line_number}: invalid JSON: {error.msg}") from error
        record = _mapping(value, f"{resolved}:{line_number}")
        schema = _string(record.get("schema"), f"{resolved}:{line_number}.schema")
        if schema == PLAN_ERROR_SCHEMA:
            message = _string(record.get("error"), f"{resolved}:{line_number}.error")
            raise PlanError(f"{resolved}:{line_number}: plan contains an analysis error: {message}")
        if schema not in {PLAN_SCHEMA, LEGACY_PLAN_SCHEMA}:
            raise PlanError(
                f"{resolved}:{line_number}: unsupported schema {schema!r}; "
                f"expected {PLAN_SCHEMA!r}"
            )
        item = _planned_write(
            record,
            resolved=resolved,
            line_number=line_number,
            schema=schema,
        )
        if item.path in seen_paths:
            raise PlanError(f"{resolved}:{line_number}: duplicate path {item.path}")
        seen_paths.add(item.path)
        planned.append(item)

    if not planned:
        raise PlanError(f"Plan contains no tracks: {resolved}")
    return planned


def _planned_write(
    record: dict[str, Any],
    *,
    resolved: Path,
    line_number: int,
    schema: str,
) -> PlannedWrite:
    location = f"{resolved}:{line_number}"
    file_path = Path(_string(record.get("path"), f"{location}.path"))
    if not file_path.is_absolute():
        raise PlanError(f"{location}.path: expected an absolute path")

    source = _mapping(record.get("source"), f"{location}.source")
    source_sha256 = _string(source.get("sha256"), f"{location}.source.sha256")
    invalid_sha256 = len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    )
    if invalid_sha256:
        raise PlanError(f"{location}.source.sha256: expected a lowercase SHA-256 digest")
    source_size = _non_negative_int(source.get("size"), f"{location}.source.size")
    source_mtime_ns = _non_negative_int(source.get("mtime_ns"), f"{location}.source.mtime_ns")

    file_genre = tuple(_string_list(record.get("file_genre"), f"{location}.file_genre"))
    target_file_genre: tuple[str, ...] | None
    if schema == LEGACY_PLAN_SCHEMA:
        target_file_genre = None
    else:
        target_value = record.get("target_file_genre")
        target_file_genre = (
            None
            if target_value is None
            else tuple(_string_list(target_value, f"{location}.target_file_genre"))
        )
    selected = tuple(_predictions(record.get("selected"), f"{location}.selected"))
    metadata_format = _string(
        record.get("metadata_format"),
        f"{location}.metadata_format",
    )
    provenance = _mapping(record.get("provenance"), f"{location}.provenance")
    settag_version = _string(
        provenance.get("settag_version"),
        f"{location}.provenance.settag_version",
    )
    model_id = _string(provenance.get("model"), f"{location}.provenance.model")
    analyzed_at = _string(
        provenance.get("analyzed_at"),
        f"{location}.provenance.analyzed_at",
    )
    config_sha256 = _string(
        provenance.get("config_sha256"),
        f"{location}.provenance.config_sha256",
    )
    changes_value = record.get("changes")
    if schema == LEGACY_PLAN_SCHEMA:
        owned_changes = tuple(_string_list(changes_value, f"{location}.changes"))
        recorded_standard_change = None
    else:
        changes = _mapping(changes_value, f"{location}.changes")
        owned_changes = tuple(
            _string_list(changes.get("settag"), f"{location}.changes.settag")
        )
        recorded_standard_change = changes.get("file_genre")
        if recorded_standard_change is not None:
            recorded_standard_change = _string(
                recorded_standard_change,
                f"{location}.changes.file_genre",
            )
    genres = [item.label for item in selected] or None
    scores = (
        [
            json.dumps(
                [item.to_dict() for item in selected],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ]
        if selected
        else None
    )
    desired: OwnedValues = {
        "SETTAG_GENRE": genres,
        "SETTAG_GENRE_SCORES": scores,
        "SETTAG_VERSION": [settag_version],
        "SETTAG_MODEL": [model_id],
        "SETTAG_ANALYZED_AT": [analyzed_at],
        "SETTAG_CONFIG_SHA256": [config_sha256],
    }

    planned = PlannedWrite(
        path=file_path,
        source_sha256=source_sha256,
        source_size=source_size,
        source_mtime_ns=source_mtime_ns,
        file_genre=file_genre,
        selected=selected,
        desired=desired,
        metadata_format=metadata_format,
        owned_changes=owned_changes,
        target_file_genre=target_file_genre,
    )
    expected_standard_change = planned.standard_genre_change
    expected_description = (
        friendly_standard_genre_change(expected_standard_change)
        if expected_standard_change is not None
        else None
    )
    if recorded_standard_change != expected_description:
        raise PlanError(
            f"{location}.changes.file_genre: does not match the staged file genre"
        )
    return planned


def _predictions(value: object, location: str) -> list[Prediction]:
    items = _list(value, location)
    predictions: list[Prediction] = []
    for index, item in enumerate(items):
        prediction = _mapping(item, f"{location}[{index}]")
        label = _string(prediction.get("label"), f"{location}[{index}].label")
        score_value = prediction.get("score")
        if isinstance(score_value, bool) or not isinstance(score_value, (int, float)):
            raise PlanError(f"{location}[{index}].score: expected a number")
        score = float(score_value)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise PlanError(f"{location}[{index}].score: expected a finite value from 0 to 1")
        predictions.append(Prediction(label=label, score=score))
    return predictions


def friendly_change(change: TagChange) -> str:
    logical = _logical_field(change.field)
    before_count = len(change.before or ())
    after_count = len(change.after or ())

    if logical == "SETTAG_GENRE":
        return f"Genre labels: {before_count} → {after_count}"
    if logical == "SETTAG_GENRE_SCORES":
        action = "add" if change.before is None else "remove" if change.after is None else "update"
        return f"Ranked score data: {action}"

    labels = {
        "SETTAG_VERSION": "SetTag version",
        "SETTAG_MODEL": "Analysis model",
        "SETTAG_ANALYZED_AT": "Analysis time",
        "SETTAG_CONFIG_SHA256": "Selection configuration",
    }
    label = labels.get(logical, logical)
    return f"{label}: {_friendly_values(change.before)} → {_friendly_values(change.after)}"


def friendly_standard_genre_change(change: TagChange) -> str:
    return (
        f"File genre: {_friendly_genres(change.before)} "
        f"→ {_friendly_genres(change.after)}"
    )


def _friendly_genres(values: list[str] | None) -> str:
    return ", ".join(values) if values else "None"


def _standard_genre_field(metadata_format: str) -> str:
    fields = {
        "id3": "TCON",
        "vorbis-comments": "GENRE",
        "mp4-freeform": "\xa9gen",
    }
    try:
        return fields[metadata_format]
    except KeyError as error:
        raise PlanError(f"Unsupported metadata format: {metadata_format}") from error


def _logical_field(native_field: str) -> str:
    if "SETTAG_" in native_field:
        return native_field[native_field.index("SETTAG_") :]
    return f"SETTAG_{native_field.rsplit(':', 1)[-1]}"


def _friendly_values(values: list[str] | None) -> str:
    if not values:
        return "not set"
    value = ", ".join(values)
    return f"{value[:16]}…" if len(value) > 20 else value


def _mapping(value: object, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanError(f"{location}: expected an object")
    return value


def _list(value: object, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise PlanError(f"{location}: expected an array")
    return value


def _string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlanError(f"{location}: expected a non-empty string")
    return value


def _string_list(value: object, location: str) -> list[str]:
    return [
        _string(item, f"{location}[{index}]")
        for index, item in enumerate(_list(value, location))
    ]


def _non_negative_int(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlanError(f"{location}: expected a non-negative integer")
    return value
