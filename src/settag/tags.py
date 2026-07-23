from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, AtomDataType, MP4FreeForm
from mutagen.wave import WAVE

from settag import __version__
from settag.policy import Prediction

ENCODING_UTF8 = 3
MP4_MEAN = "com.lsdcapital.settag"

OwnedValues = dict[str, list[str] | None]


class UnsupportedTagFormatError(ValueError):
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


def build_owned_values(
    selected: list[Prediction],
    *,
    model_id: str,
    analyzed_at: str,
    config_sha256: str,
) -> OwnedValues:
    genres = [item.label for item in selected]
    scores = json.dumps(
        [item.to_dict() for item in selected],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "SETTAG_GENRE": genres or None,
        "SETTAG_GENRE_SCORES": [scores] if genres else None,
        "SETTAG_VERSION": [__version__],
        "SETTAG_MODEL": [model_id],
        "SETTAG_ANALYZED_AT": [analyzed_at],
        "SETTAG_CONFIG_SHA256": [config_sha256],
    }


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

    def apply(self, desired: OwnedValues) -> TagPlan:
        plan = self.plan(desired)
        if not plan.changes:
            return plan

        for description, values in desired.items():
            self.write_value(description, values)
        self.audio.save()
        return plan

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


def read_genre_state(path: Path) -> GenreState:
    return owned_tag_store(path).genre_state()


def apply_owned_tags(path: Path, desired: OwnedValues) -> TagPlan:
    return owned_tag_store(path).apply(desired)
