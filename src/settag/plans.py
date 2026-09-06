from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from settag.freshness import (
    EnrichmentState,
    EnrichmentStatus,
    catalog_evidence,
    current_catalog_evidence,
    enrichment_status,
    evidence_genres,
)
from settag.policy import EVIDENCE_LIMIT, Prediction
from settag.tags import (
    OWNED_DESCRIPTIONS,
    OwnedValues,
    TagChange,
    hygiene_field_label,
    read_task_provenance,
    task_evidence_from_owned,
)
from settag.tasks import TASK_ORDER

PLAN_SCHEMA = "settag.plan/v6"
PLAN_ERROR_SCHEMA = "settag.plan-error/v1"

# v5 added ``source.audio_sha256``. A v4 record is still read, with that digest
# left unknown, because the workbench cache is stored in this format and
# rejecting it would throw away every analysis a user had already paid for.
# Anything that needs the digest recomputes it from the file.
READABLE_PLAN_SCHEMAS = frozenset({"settag.plan/v4", "settag.plan/v5", PLAN_SCHEMA})
REFRESH_ONLY_CHANGE_PREFIXES = ("Analysis time:", "Task provenance:")
BOOKKEEPING_CHANGE_PREFIXES = (
    *REFRESH_ONLY_CHANGE_PREFIXES,
    "Enrichment status:",
    "SetTag version:",
)


GenreEditSource = Literal["manual", "beatport", "model"]


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class PlannedWrite:
    path: Path
    source_sha256: str
    source_size: int
    source_mtime_ns: int
    file_genre: tuple[str, ...]
    evidence: tuple[Prediction, ...]
    selected: tuple[Prediction, ...]
    desired: OwnedValues
    metadata_format: str
    owned_changes: tuple[str, ...]
    target_file_genre: tuple[str, ...] | None = None
    # ``None`` only for a plan read back in the v4 format, which predates this
    # digest. Callers recompute rather than treating unknown as mismatched.
    source_audio_sha256: str | None = None
    # Exact observed SetTag metadata; older records fall back to whole-file validation.
    source_owned_sha256: str | None = None
    notices: tuple[str, ...] = ()
    genre_edit_source: GenreEditSource | None = None
    enrichment: dict[str, Any] | None = None

    @property
    def evidence_view(self) -> OwnedValues:
        if self.enrichment is None:
            return self.desired
        return EnrichmentState(self.enrichment, self.desired.get("SETTAG_BEATPORT")).evidence_view(
            self.desired
        )

    @property
    def enrichment_status(self) -> EnrichmentStatus:
        return enrichment_status(self.evidence_view, audio_current=True)

    @property
    def source_details(self) -> tuple[str, ...]:
        lines = list(self.notices)
        data = catalog_evidence(self.desired)
        if data:
            agreed = ", ".join(evidence_genres(data)) or "None"
            qualifier = "" if current_catalog_evidence(self.evidence_view) else "Stored "
            lines.append(f"{qualifier}Beatport genres: {agreed}")
            if data["alternative_genres"]:
                lines.append(f"Additional release labels: {', '.join(data['alternative_genres'])}")
            lines.extend(source["url"] for source in data["sources"])
        elif self.desired.get("SETTAG_BEATPORT"):
            lines.append("Stored Beatport evidence is unreadable; enrich again.")
        return tuple(lines)

    @property
    def readable_changes(self) -> tuple[str, ...]:
        standard_change = self.standard_genre_change
        if standard_change is None:
            return self.owned_changes
        return (*self.owned_changes, friendly_standard_genre_change(standard_change))

    @property
    def owned_change_count(self) -> int:
        return len(self.owned_changes)

    @property
    def needs_write_review(self) -> bool:
        """Bookkeeping alone does not warrant an interactive file write.

        Keep the complete plan for local reuse. When evidence or the conventional
        genre changes, its bookkeeping still travels with that reviewed write.
        Unknown change types stay reviewable.
        """
        return self.standard_genre_change is not None or any(
            not change.startswith(BOOKKEEPING_CHANGE_PREFIXES) for change in self.owned_changes
        )

    @property
    def evidence_score_count(self) -> int:
        """Ranked scores stored for this track across every task.

        ``evidence`` holds genre predictions only, so counting that instead
        under-reports a multi-task run. Both UIs made exactly that mistake
        independently, which is why the count lives here.
        """
        return sum(len(values) for values in task_evidence_from_owned(self.desired).values())

    @property
    def evidence_write_kind(self) -> Literal["unchanged", "refreshed", "updated"]:
        """Describe the SetTag-owned half of this write at user level.

        A deliberate reanalysis always refreshes its timestamp and provenance,
        even when the ranked evidence is identical. Those two implementation
        fields are one user-facing refresh, not two meaningful edits.
        """
        if not self.owned_changes:
            return "unchanged"
        if all(change.startswith(REFRESH_ONLY_CHANGE_PREFIXES) for change in self.owned_changes):
            return "refreshed"
        return "updated"

    @property
    def evidence_write_label(self) -> str:
        return {
            "unchanged": "Evidence unchanged",
            "refreshed": "Evidence refreshed",
            "updated": "Evidence update",
        }[self.evidence_write_kind]

    @property
    def write_plan_label(self) -> str:
        """Compact table label for the metadata this plan would write."""
        evidence = {
            "unchanged": "",
            "refreshed": "Refresh",
            "updated": "Evidence",
        }[self.evidence_write_kind]
        genre = self.standard_genre_change is not None
        if evidence and genre:
            return f"{evidence} + genre"
        if evidence:
            return evidence
        return "Genre edit" if genre else "None"

    @property
    def standard_genre_change(self) -> TagChange | None:
        if self.target_file_genre is None or self.target_file_genre == self.file_genre:
            return None
        return TagChange(
            field=standard_genre_field(self.metadata_format),
            before=list(self.file_genre) or None,
            after=list(self.target_file_genre) or None,
        )


def stage_file_genre(
    item: PlannedWrite,
    genres: tuple[str, ...] | None,
    *,
    source: GenreEditSource | None = "manual",
) -> PlannedWrite:
    """Return a plan with an explicit conventional genre edit staged.

    ``None`` preserves the original file genre. An empty tuple explicitly
    removes it. Staging the original value collapses back to no edit.
    """
    target = None if genres is None or genres == item.file_genre else genres
    return replace(item, target_file_genre=target, genre_edit_source=source)


def catalog_genres(owned: OwnedValues, current: tuple[str, ...] = ()) -> tuple[str, ...]:
    catalog = current_catalog_evidence(owned)
    if catalog is None:
        return ()
    genres = evidence_genres(catalog)
    # Keep an existing primary only when a verified Beatport release also provides it.
    primary = next((g for old in current for g in genres if old.casefold() == g.casefold()), None)
    return (primary, *(g for g in genres if g != primary)) if primary else genres


def catalog_genre_summary(owned: OwnedValues) -> str:
    return "Multiple Beatport genres" if len(catalog_genres(owned)) > 1 else "Use Beatport genre"


def catalog_suggestion(owned: OwnedValues) -> str | None:
    return ", ".join(catalog_genres(owned)) or None


def suggested_file_genre(item: PlannedWrite) -> str | None:
    return genre_suggestion(item.evidence_view, item.selected, item.file_genre)


def genre_suggestion(
    owned: OwnedValues, selected: tuple[Prediction, ...], current: tuple[str, ...] = ()
) -> str | None:
    genres = catalog_genres(owned, current)
    if genres:
        return ", ".join(genres)
    if not selected:
        return None
    return standard_genre_from_model_label(selected[0].label)


def standard_genre_from_model_label(label: str) -> str | None:
    """Remove the taxonomy parent prefix while preserving the model's genre detail."""
    normalized = label.strip()
    child = normalized.rsplit("---", 1)[-1].strip()
    if not child:
        return None
    return child


def stage_default_file_genre(item: PlannedWrite) -> PlannedWrite:
    """Prefer all verified catalog genres; audio only fills an empty genre."""
    if item.genre_edit_source == "manual" or (
        item.target_file_genre is not None and item.genre_edit_source is None
    ):
        return item
    genres = catalog_genres(item.evidence_view, item.file_genre)
    if genres:
        return stage_file_genre(item, genres, source="beatport")
    suggestion = genre_suggestion(item.evidence_view, item.selected)
    if not item.file_genre and suggestion:
        return stage_file_genre(item, (suggestion,), source="model")
    return stage_file_genre(item, None, source=None)


def planned_write_record(item: PlannedWrite) -> dict[str, object]:
    model = item.desired["SETTAG_MODEL"]
    analyzed_at = item.desired["SETTAG_ANALYZED_AT"]
    version = item.desired["SETTAG_VERSION"]
    config = item.desired["SETTAG_CONFIG_SHA256"]
    task_evidence = task_evidence_from_owned(item.desired)
    task_provenance = read_task_provenance(item.desired)
    return {
        "schema": PLAN_SCHEMA,
        "path": str(item.path),
        "notices": list(item.notices),
        "genre_edit_source": item.genre_edit_source,
        "enrichment": item.enrichment,
        "source": {
            "sha256": item.source_sha256,
            "audio_sha256": item.source_audio_sha256,
            "owned_sha256": item.source_owned_sha256,
            "size": item.source_size,
            "mtime_ns": item.source_mtime_ns,
        },
        "file_genre": list(item.file_genre),
        "target_file_genre": (
            list(item.target_file_genre) if item.target_file_genre is not None else None
        ),
        "evidence": [prediction.to_dict() for prediction in item.evidence],
        "selected": [prediction.to_dict() for prediction in item.selected],
        "tasks": {
            task: {
                "evidence": [prediction.to_dict() for prediction in task_evidence.get(task, ())],
                "provenance": task_provenance.get(task),
            }
            for task in TASK_ORDER
            if task in task_evidence or task in task_provenance
        },
        "metadata": {
            "fields": {field: item.desired.get(field) for field in OWNED_DESCRIPTIONS},
        },
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
        if schema not in READABLE_PLAN_SCHEMAS:
            raise PlanError(
                f"{resolved}:{line_number}: unsupported schema {schema!r}; expected {PLAN_SCHEMA!r}"
            )
        item = planned_write_from_record(
            record,
            location=f"{resolved}:{line_number}",
        )
        if item.path in seen_paths:
            raise PlanError(f"{resolved}:{line_number}: duplicate path {item.path}")
        seen_paths.add(item.path)
        planned.append(item)

    if not planned:
        raise PlanError(f"Plan contains no tracks: {resolved}")
    return planned


def planned_write_from_record(
    value: object,
    *,
    location: str = "plan record",
) -> PlannedWrite:
    record = _mapping(value, location)
    schema = _string(record.get("schema"), f"{location}.schema")
    if schema == PLAN_ERROR_SCHEMA:
        message = _string(record.get("error"), f"{location}.error")
        raise PlanError(f"{location}: plan contains an analysis error: {message}")
    if schema not in READABLE_PLAN_SCHEMAS:
        raise PlanError(f"{location}: unsupported schema {schema!r}; expected {PLAN_SCHEMA!r}")
    return _planned_write(record, location=location)


def _planned_write(
    record: dict[str, Any],
    *,
    location: str,
) -> PlannedWrite:
    file_path = Path(_string(record.get("path"), f"{location}.path"))
    if not file_path.is_absolute():
        raise PlanError(f"{location}.path: expected an absolute path")

    source = _mapping(record.get("source"), f"{location}.source")
    source_sha256 = _string(source.get("sha256"), f"{location}.source.sha256")
    if not _is_sha256(source_sha256):
        raise PlanError(f"{location}.source.sha256: expected a lowercase SHA-256 digest")
    audio_value = source.get("audio_sha256")
    if audio_value is None:
        source_audio_sha256 = None
    else:
        source_audio_sha256 = _string(audio_value, f"{location}.source.audio_sha256")
        if not _is_sha256(source_audio_sha256):
            raise PlanError(f"{location}.source.audio_sha256: expected a lowercase SHA-256 digest")
    owned_value = source.get("owned_sha256")
    source_owned_sha256 = None
    if owned_value is not None:
        source_owned_sha256 = _string(owned_value, f"{location}.source.owned_sha256")
        if not _is_sha256(source_owned_sha256):
            raise PlanError(f"{location}.source.owned_sha256: expected a lowercase SHA-256 digest")
    source_size = _non_negative_int(source.get("size"), f"{location}.source.size")
    source_mtime_ns = _non_negative_int(source.get("mtime_ns"), f"{location}.source.mtime_ns")

    file_genre = tuple(_string_list(record.get("file_genre"), f"{location}.file_genre"))
    target_value = record.get("target_file_genre")
    target_file_genre = (
        None
        if target_value is None
        else tuple(_string_list(target_value, f"{location}.target_file_genre"))
    )
    genre_edit_source = record.get("genre_edit_source")
    if genre_edit_source not in (None, "manual", "beatport", "model"):
        raise PlanError(f"{location}.genre_edit_source: invalid genre edit source")
    selected = tuple(_predictions(record.get("selected"), f"{location}.selected"))
    evidence = tuple(_predictions(record.get("evidence"), f"{location}.evidence"))
    _validate_evidence(evidence, f"{location}.evidence")
    if not _is_ordered_subset(selected, evidence):
        raise PlanError(f"{location}.selected: expected an ordered subset of the stored evidence")
    metadata_format = _string(
        record.get("metadata_format"),
        f"{location}.metadata_format",
    )
    provenance = _mapping(record.get("provenance"), f"{location}.provenance")
    for field in ("settag_version", "model", "analyzed_at", "config_sha256"):
        _string(provenance.get(field), f"{location}.provenance.{field}")

    changes = _mapping(record.get("changes"), f"{location}.changes")
    owned_changes = tuple(_string_list(changes.get("settag"), f"{location}.changes.settag"))
    recorded_standard_change = changes.get("file_genre")
    if recorded_standard_change is not None:
        recorded_standard_change = _string(
            recorded_standard_change,
            f"{location}.changes.file_genre",
        )

    metadata = _mapping(record.get("metadata"), f"{location}.metadata")
    fields = _mapping(metadata.get("fields"), f"{location}.metadata.fields")
    unknown = sorted(set(fields) - set(OWNED_DESCRIPTIONS))
    missing = sorted(
        set(OWNED_DESCRIPTIONS) - set(fields) - {"SETTAG_BEATPORT", "SETTAG_ENRICHMENT"}
    )
    if unknown or missing:
        raise PlanError(f"{location}.metadata.fields: expected the complete SetTag field set")
    desired = {
        field: _optional_string_list(
            fields.get(field),
            f"{location}.metadata.fields.{field}",
        )
        for field in OWNED_DESCRIPTIONS
    }
    stored_genre = task_evidence_from_owned(desired).get("genre", ())
    if tuple(stored_genre) != evidence:
        raise PlanError(f"{location}.evidence: does not match the stored genre metadata")
    _validate_task_records(record.get("tasks"), desired, location)

    planned = PlannedWrite(
        path=file_path,
        source_sha256=source_sha256,
        source_size=source_size,
        source_mtime_ns=source_mtime_ns,
        file_genre=file_genre,
        evidence=evidence,
        selected=selected,
        desired=desired,
        metadata_format=metadata_format,
        owned_changes=owned_changes,
        target_file_genre=target_file_genre,
        genre_edit_source=genre_edit_source,
        source_audio_sha256=source_audio_sha256,
        source_owned_sha256=source_owned_sha256,
        notices=tuple(_string_list(record.get("notices", []), f"{location}.notices")),
        enrichment=(
            _mapping(record["enrichment"], f"{location}.enrichment")
            if record.get("enrichment") is not None
            else None
        ),
    )
    expected_standard_change = planned.standard_genre_change
    expected_description = (
        friendly_standard_genre_change(expected_standard_change)
        if expected_standard_change is not None
        else None
    )
    if recorded_standard_change != expected_description:
        raise PlanError(f"{location}.changes.file_genre: does not match the staged file genre")
    return planned


def _is_ordered_subset(
    selected: tuple[Prediction, ...],
    evidence: tuple[Prediction, ...],
) -> bool:
    position = 0
    for prediction in selected:
        try:
            position = evidence.index(prediction, position) + 1
        except ValueError:
            return False
    return True


def _validate_evidence(
    evidence: tuple[Prediction, ...],
    location: str,
) -> None:
    if len(evidence) > EVIDENCE_LIMIT:
        raise PlanError(f"{location}: expected no more than {EVIDENCE_LIMIT} ranked scores")
    labels = [prediction.label for prediction in evidence]
    if len(set(labels)) != len(labels):
        raise PlanError(f"{location}: expected unique model labels")
    ranked = tuple(
        sorted(
            evidence,
            key=lambda prediction: (-prediction.score, prediction.label),
        )
    )
    if evidence != ranked:
        raise PlanError(f"{location}: expected descending score order")


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


def _optional_string_list(value: object, location: str) -> list[str] | None:
    if value is None:
        return None
    return _string_list(value, location)


def _validate_task_records(
    value: object,
    desired: OwnedValues,
    location: str,
) -> None:
    tasks = _mapping(value, f"{location}.tasks")
    evidence = task_evidence_from_owned(desired)
    provenance = read_task_provenance(desired)
    expected = {task for task in TASK_ORDER if task in evidence or task in provenance}
    if set(tasks) != expected:
        raise PlanError(f"{location}.tasks: does not match the stored task metadata")
    for task in expected:
        entry = _mapping(tasks.get(task), f"{location}.tasks.{task}")
        recorded = tuple(_predictions(entry.get("evidence"), f"{location}.tasks.{task}.evidence"))
        if recorded != evidence.get(task, ()):
            raise PlanError(f"{location}.tasks.{task}.evidence: does not match stored metadata")
        if entry.get("provenance") != provenance.get(task):
            raise PlanError(f"{location}.tasks.{task}.provenance: does not match stored metadata")


def friendly_change(change: TagChange) -> str:
    logical = _logical_field(change.field)
    before_count = len(change.before or ())
    after_count = len(change.after or ())

    if logical == "SETTAG_ENRICHMENT":
        return "Enrichment status: update"
    if logical == "SETTAG_BEATPORT":
        return "Beatport genre evidence: update"
    if logical == "SETTAG_GENRE":
        return f"Genre labels: {before_count} → {after_count}"
    if logical == "SETTAG_GENRE_SCORES":
        action = "add" if change.before is None else "remove" if change.after is None else "update"
        return f"Ranked score data: {action}"
    if logical in {"SETTAG_MOOD_THEME", "SETTAG_INSTRUMENT"}:
        name = "Mood/theme" if logical == "SETTAG_MOOD_THEME" else "Instrument"
        return f"{name} labels: {before_count} → {after_count}"
    if logical in {"SETTAG_MOOD_THEME_SCORES", "SETTAG_INSTRUMENT_SCORES"}:
        name = "Mood/theme" if "MOOD_THEME" in logical else "Instrument"
        action = "add" if change.before is None else "remove" if change.after is None else "update"
        return f"{name} ranked score data: {action}"

    labels = {
        "SETTAG_VERSION": "SetTag version",
        "SETTAG_MODEL": "Analysis model",
        "SETTAG_ANALYZED_AT": "Analysis time",
        "SETTAG_CONFIG_SHA256": "Evidence configuration",
        "SETTAG_PROVENANCE": "Task provenance",
    }
    label = labels.get(logical, logical)
    return f"{label}: {_friendly_values(change.before)} → {_friendly_values(change.after)}"


def friendly_standard_genre_change(change: TagChange) -> str:
    return f"File genre: {_friendly_genres(change.before)} → {_friendly_genres(change.after)}"


def friendly_hygiene_change(change: TagChange) -> str:
    before = _friendly_values(change.before)
    after = _friendly_values(change.after)
    return f"Tag hygiene {hygiene_field_label(change.field)}: {before} → {after}"


def _friendly_genres(values: list[str] | None) -> str:
    return ", ".join(values) if values else "None"


def standard_genre_field(metadata_format: str) -> str:
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
    if not all(isinstance(key, str) for key in value):
        raise PlanError(f"{location}: expected string object keys")
    return cast(dict[str, Any], value)


def _list(value: object, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise PlanError(f"{location}: expected an array")
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlanError(f"{location}: expected a non-empty string")
    return value


def _string_list(value: object, location: str) -> list[str]:
    return [
        _string(item, f"{location}[{index}]") for index, item in enumerate(_list(value, location))
    ]


def _non_negative_int(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlanError(f"{location}: expected a non-negative integer")
    return value
