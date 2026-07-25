from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TCON, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, AtomDataType, MP4FreeForm
from mutagen.wave import WAVE

from settag import __version__
from settag.policy import Prediction
from settag.tasks import TASK_FIELDS, TASK_ORDER, AnalysisTask, task_name

ENCODING_UTF8 = 3
MP4_MEAN = "com.lsdcapital.settag"
OWNED_DESCRIPTIONS = (
    "SETTAG_GENRE",
    "SETTAG_GENRE_SCORES",
    "SETTAG_MOOD_THEME",
    "SETTAG_MOOD_THEME_SCORES",
    "SETTAG_INSTRUMENT",
    "SETTAG_INSTRUMENT_SCORES",
    "SETTAG_VERSION",
    "SETTAG_MODEL",
    "SETTAG_ANALYZED_AT",
    "SETTAG_CONFIG_SHA256",
    "SETTAG_PROVENANCE",
)

OwnedValues = dict[str, list[str] | None]


class UnsupportedTagFormatError(ValueError):
    pass


class TagVerificationError(RuntimeError):
    pass


class TagStateChangedError(RuntimeError):
    pass


@dataclass(frozen=True)
class TagChange:
    field: str
    before: list[str] | None
    after: list[str] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class TagPlan:
    format: str
    changes: tuple[TagChange, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format,
            "changes": [item.to_dict() for item in self.changes],
        }


@dataclass(frozen=True)
class GenreState:
    standard: tuple[str, ...]
    settag: tuple[str, ...]


def build_task_owned_values(
    current: Mapping[str, list[str] | None],
    evidence_by_task: Mapping[AnalysisTask, list[Prediction]],
    provenance_by_task: Mapping[AnalysisTask, dict[str, object]],
) -> OwnedValues:
    """Merge newly analyzed tasks into the complete SetTag-owned metadata bundle."""
    desired: OwnedValues = {
        description: current.get(description) for description in OWNED_DESCRIPTIONS
    }
    provenance = read_task_provenance(current)
    for task in TASK_ORDER:
        evidence = evidence_by_task.get(task)
        task_provenance = provenance_by_task.get(task)
        if evidence is None or task_provenance is None:
            continue
        label_field, score_field = TASK_FIELDS[task]
        labels = [item.label for item in evidence]
        desired[label_field] = labels or None
        desired[score_field] = (
            [
                json.dumps(
                    [item.to_dict() for item in evidence],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            ]
            if labels
            else None
        )
        provenance[task] = task_provenance

    if evidence_by_task:
        desired["SETTAG_VERSION"] = [__version__]
    if "genre" in evidence_by_task:
        genre = provenance_by_task["genre"]
        model = genre.get("model")
        model_id = model.get("id") if isinstance(model, dict) else None
        analyzed_at = genre.get("analyzed_at")
        config = genre.get("config")
        config_sha256 = config.get("sha256") if isinstance(config, dict) else None
        desired["SETTAG_MODEL"] = [model_id] if isinstance(model_id, str) else None
        desired["SETTAG_ANALYZED_AT"] = [analyzed_at] if isinstance(analyzed_at, str) else None
        desired["SETTAG_CONFIG_SHA256"] = (
            [config_sha256] if isinstance(config_sha256, str) else None
        )

    desired["SETTAG_PROVENANCE"] = (
        [
            json.dumps(
                {
                    "schema": "settag.provenance/v2",
                    "tasks": {task: provenance[task] for task in TASK_ORDER if task in provenance},
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        ]
        if provenance
        else None
    )
    return desired


def read_task_provenance(
    owned: Mapping[str, list[str] | None],
) -> dict[AnalysisTask, dict[str, object]]:
    serialized = owned.get("SETTAG_PROVENANCE")
    parsed: dict[AnalysisTask, dict[str, object]] = {}
    if serialized is not None and len(serialized) == 1:
        try:
            value = json.loads(serialized[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and value.get("schema") == "settag.provenance/v2":
            tasks = value.get("tasks")
            if isinstance(tasks, dict):
                for raw_task, entry in tasks.items():
                    try:
                        task = task_name(raw_task)
                    except ValueError:
                        continue
                    if isinstance(entry, dict):
                        parsed[task] = entry
    return parsed


def task_evidence_from_owned(
    owned: Mapping[str, list[str] | None],
) -> dict[AnalysisTask, tuple[Prediction, ...]]:
    results: dict[AnalysisTask, tuple[Prediction, ...]] = {}
    for task in TASK_ORDER:
        label_field, score_field = TASK_FIELDS[task]
        labels = owned.get(label_field)
        serialized = owned.get(score_field)
        if not labels or serialized is None or len(serialized) != 1:
            continue
        try:
            values = json.loads(serialized[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(values, list):
            continue
        predictions: list[Prediction] = []
        valid = True
        for value in values:
            if not isinstance(value, dict):
                valid = False
                break
            label = value.get("label")
            score = value.get("score")
            if (
                not isinstance(label, str)
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not 0 <= float(score) <= 1
            ):
                valid = False
                break
            predictions.append(Prediction(label, float(score)))
        if valid and [item.label for item in predictions] == labels:
            results[task] = tuple(predictions)
    return results


class OwnedTagStore(ABC):
    format_name: str

    def __init__(self, path: Path, audio: Any) -> None:
        self.path = path
        self.audio = audio

    def plan(self, desired: OwnedValues) -> TagPlan:
        changes = tuple(
            TagChange(
                field=self.field_name(description),
                before=before,
                after=after,
            )
            for description, after in desired.items()
            if (before := self.read_value(description)) != after
        )
        return TagPlan(format=self.format_name, changes=changes)

    def apply(
        self,
        desired: OwnedValues,
        *,
        standard_genres: tuple[str, ...] | None = None,
    ) -> TagPlan:
        plan = self.plan(desired)
        standard_change = (
            self.plan_standard_genres(standard_genres) if standard_genres is not None else None
        )
        if not plan.changes and standard_change is None:
            return plan

        for description, values in desired.items():
            self.write_value(description, values)
        if standard_genres is not None:
            self.write_standard_genres(list(standard_genres))
        self.audio.save()
        return plan

    def plan_standard_genres(
        self,
        desired: tuple[str, ...],
    ) -> TagChange | None:
        before = self.read_standard_genres()
        after = list(desired)
        if before == after:
            return None
        return TagChange(
            field=self.standard_genre_field_name(),
            before=before or None,
            after=after or None,
        )

    def ensure_tags(self) -> Any:
        if self.audio.tags is None:
            self.audio.add_tags()
        if self.audio.tags is None:
            raise UnsupportedTagFormatError(f"Could not create metadata tags in {self.path}")
        return self.audio.tags

    def genre_state(self) -> GenreState:
        return GenreState(
            standard=tuple(self.read_standard_genres()),
            settag=tuple(self.read_value("SETTAG_GENRE") or ()),
        )

    @abstractmethod
    def field_name(self, description: str) -> str:
        pass

    @abstractmethod
    def read_standard_genres(self) -> list[str]:
        pass

    @abstractmethod
    def standard_genre_field_name(self) -> str:
        pass

    @abstractmethod
    def write_standard_genres(self, values: list[str]) -> None:
        pass

    @abstractmethod
    def read_value(self, description: str) -> list[str] | None:
        pass

    @abstractmethod
    def write_value(self, description: str, values: list[str] | None) -> None:
        pass


class Id3OwnedTagStore(OwnedTagStore):
    format_name = "id3"

    def field_name(self, description: str) -> str:
        return f"TXXX:{description}"

    def read_standard_genres(self) -> list[str]:
        tags = self.audio.tags
        if tags is None:
            return []
        if not isinstance(tags, ID3):
            raise UnsupportedTagFormatError(f"Expected ID3 metadata in {self.path}")
        return [str(value) for frame in tags.getall("TCON") for value in frame.genres]

    def standard_genre_field_name(self) -> str:
        return "TCON"

    def write_standard_genres(self, values: list[str]) -> None:
        tags = self.ensure_tags()
        if not isinstance(tags, ID3):
            raise UnsupportedTagFormatError(f"Expected ID3 metadata in {self.path}")
        tags.delall("TCON")
        if values:
            tags.add(TCON(encoding=ENCODING_UTF8, text=values))

    def read_value(self, description: str) -> list[str] | None:
        tags = self.audio.tags
        if tags is None:
            return None
        if not isinstance(tags, ID3):
            raise UnsupportedTagFormatError(f"Expected ID3 metadata in {self.path}")

        frames = tags.getall(self.field_name(description))
        if not frames:
            return None
        return [str(value) for frame in frames for value in frame.text]

    def write_value(self, description: str, values: list[str] | None) -> None:
        tags = self.ensure_tags()
        if not isinstance(tags, ID3):
            raise UnsupportedTagFormatError(f"Expected ID3 metadata in {self.path}")

        tags.delall(self.field_name(description))
        if values is not None:
            tags.add(TXXX(encoding=ENCODING_UTF8, desc=description, text=values))


class VorbisOwnedTagStore(OwnedTagStore):
    format_name = "vorbis-comments"

    def field_name(self, description: str) -> str:
        return description

    def read_standard_genres(self) -> list[str]:
        tags = self.audio.tags
        if tags is None:
            return []
        return [
            str(value)
            for key, values in tags.items()
            if str(key).casefold() == "genre"
            for value in values
        ]

    def standard_genre_field_name(self) -> str:
        return "GENRE"

    def write_standard_genres(self, values: list[str]) -> None:
        tags = self.ensure_tags()
        for key in list(tags):
            if str(key).casefold() == "genre":
                del tags[key]
        if values:
            tags["GENRE"] = values

    def read_value(self, description: str) -> list[str] | None:
        tags = self.audio.tags
        if tags is None or description not in tags:
            return None
        return [str(value) for value in tags[description]]

    def write_value(self, description: str, values: list[str] | None) -> None:
        tags = self.ensure_tags()
        if description in tags:
            del tags[description]
        if values is not None:
            tags[description] = values


class Mp4OwnedTagStore(OwnedTagStore):
    format_name = "mp4-freeform"

    def field_name(self, description: str) -> str:
        if not description.startswith("SETTAG_"):
            raise ValueError(f"Invalid SetTag-owned field: {description}")
        name = description[len("SETTAG_") :]
        return f"----:{MP4_MEAN}:{name}"

    def read_standard_genres(self) -> list[str]:
        tags = self.audio.tags
        if tags is None:
            return []
        return [str(value) for value in tags.get("\xa9gen", ())]

    def standard_genre_field_name(self) -> str:
        return "\xa9gen"

    def write_standard_genres(self, values: list[str]) -> None:
        tags = self.ensure_tags()
        if "\xa9gen" in tags:
            del tags["\xa9gen"]
        if values:
            tags["\xa9gen"] = values

    def read_value(self, description: str) -> list[str] | None:
        tags = self.audio.tags
        key = self.field_name(description)
        if tags is None or key not in tags:
            return None

        values: list[str] = []
        for value in tags[key]:
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, bytes):
                values.append(value.decode("utf-8"))
            else:
                raise UnsupportedTagFormatError(
                    f"Unexpected value in SetTag-owned MP4 atom {key} in {self.path}"
                )
        return values

    def write_value(self, description: str, values: list[str] | None) -> None:
        tags = self.ensure_tags()
        key = self.field_name(description)
        if key in tags:
            del tags[key]
        if values is not None:
            tags[key] = [
                MP4FreeForm(value.encode("utf-8"), dataformat=AtomDataType.UTF8) for value in values
            ]


def owned_tag_store(path: Path) -> OwnedTagStore:
    try:
        audio = mutagen.File(path)
    except mutagen.MutagenError as error:
        raise UnsupportedTagFormatError(f"Could not read metadata container: {path}") from error

    if audio is None:
        raise UnsupportedTagFormatError(f"Unrecognized metadata container: {path}")
    if isinstance(audio, (MP3, AIFF, WAVE)):
        return Id3OwnedTagStore(path, audio)
    if isinstance(audio, FLAC):
        return VorbisOwnedTagStore(path, audio)
    if isinstance(audio, MP4):
        return Mp4OwnedTagStore(path, audio)

    raise UnsupportedTagFormatError(
        f"Metadata writes are not supported for {type(audio).__name__}: {path}"
    )


def plan_owned_tags(path: Path, desired: OwnedValues) -> TagPlan:
    return owned_tag_store(path).plan(desired)


def plan_standard_genres(path: Path, desired: tuple[str, ...]) -> TagChange | None:
    return owned_tag_store(path).plan_standard_genres(desired)


def read_genre_state(path: Path) -> GenreState:
    return owned_tag_store(path).genre_state()


def read_owned_values(path: Path) -> OwnedValues:
    store = owned_tag_store(path)
    return {description: store.read_value(description) for description in OWNED_DESCRIPTIONS}


def apply_metadata_tags(
    path: Path,
    desired: OwnedValues,
    *,
    standard_genres: tuple[str, ...] | None = None,
    expected_plan: TagPlan | None = None,
    expected_standard: tuple[str, ...] | None = None,
    expected_standard_change: TagChange | None = None,
) -> TagPlan:
    """Apply one verified metadata transaction to a single file.

    SetTag-owned evidence and an explicitly staged conventional genre edit are
    written through the same parsed metadata container and saved once.
    """
    store = owned_tag_store(path)
    current_plan = store.plan(desired)
    current_standard = store.genre_state().standard
    current_standard_change = (
        store.plan_standard_genres(standard_genres) if standard_genres is not None else None
    )
    if expected_plan is not None and current_plan != expected_plan:
        raise TagStateChangedError(f"Tag state changed after planning and before writing {path}")
    if expected_standard is not None and current_standard != expected_standard:
        raise TagStateChangedError(
            f"File genre tag changed after planning and before writing {path}"
        )
    if current_standard_change != expected_standard_change:
        raise TagStateChangedError(
            f"Staged file genre change changed after planning and before writing {path}"
        )

    plan = store.apply(desired, standard_genres=standard_genres)
    remaining = owned_tag_store(path).plan(desired)
    if remaining.changes:
        fields = ", ".join(change.field for change in remaining.changes)
        raise TagVerificationError(f"SetTag metadata verification failed for {path}: {fields}")
    if standard_genres is not None:
        after = owned_tag_store(path).genre_state().standard
        if after != standard_genres:
            raise TagVerificationError(f"File genre tag verification failed for {path}")
    return plan
