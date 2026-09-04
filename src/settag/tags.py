from __future__ import annotations

import json
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import COMM, ID3, TCON, TSSE, TXXX, WXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, AtomDataType, MP4FreeForm
from mutagen.wave import WAVE

from settag import __version__
from settag.policy import Prediction
from settag.scanner import WRITE_TEMPORARY_MARKER
from settag.tasks import TASK_FIELDS, TASK_ORDER, AnalysisTask, task_name

ENCODING_UTF8 = 3
MP4_MEAN = "com.lsdcapital.settag"

# The provenance record's shape. Written and required by `read_task_provenance`, so the
# two cannot drift apart: bumping this makes every older record unreadable, which surfaces
# those tracks as stale and offers them for re-analysis. Bump it when the record gains or
# changes a field consumers depend on, not for a new model or a new evidence setting —
# those are already compared field by field.
#
# v3 added `model.vocabulary`, the taxonomy a task's labels are drawn from.
PROVENANCE_SCHEMA = "settag.provenance/v3"
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
HygieneValues = dict[str, list[str] | None]
HygieneCategory = Literal["comment", "encoder", "text", "url"]


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


@dataclass(frozen=True)
class HygieneTag:
    """One text tag SetTag can safely present for optional cleanup."""

    field: str
    label: str
    category: HygieneCategory
    values: tuple[str, ...]


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

    # Labels and their provenance have to leave together. The seed above carries every
    # owned value forward from `current` unconditionally, while provenance survives only
    # if the whole record still parses at PROVENANCE_SCHEMA — so a schema bump strands the
    # labels of every task this run does not regenerate. Stranded labels are worse than
    # absent ones: they stay readable and filterable while nothing can say which model or
    # taxonomy produced them, and neither scan goes looking, because both iterate the
    # configured tasks rather than the file's. Drop them instead. The removal is a planned
    # change like any other, so it is shown before it is written, and re-running the task
    # restores the labels with provenance attached.
    for task in TASK_ORDER:
        if task in provenance:
            continue
        label_field, score_field = TASK_FIELDS[task]
        desired[label_field] = None
        desired[score_field] = None

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
                    "schema": PROVENANCE_SCHEMA,
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
        if isinstance(value, dict) and value.get("schema") == PROVENANCE_SCHEMA:
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

    def plan_hygiene(self, desired: Mapping[str, list[str] | None]) -> TagPlan:
        changes = tuple(
            TagChange(
                field=field,
                before=before,
                after=after,
            )
            for field, after in desired.items()
            if (before := self.read_hygiene_value(field)) != after
        )
        return TagPlan(format=self.format_name, changes=changes)

    def apply(
        self,
        desired: OwnedValues,
        *,
        standard_genres: tuple[str, ...] | None = None,
        hygiene_values: Mapping[str, list[str] | None] | None = None,
    ) -> TagPlan:
        plan = self.plan(desired)
        hygiene_plan = self.plan_hygiene(hygiene_values or {})
        standard_change = (
            self.plan_standard_genres(standard_genres) if standard_genres is not None else None
        )
        if not plan.changes and standard_change is None and not hygiene_plan.changes:
            return plan

        for description, values in desired.items():
            self.write_value(description, values)
        if standard_genres is not None:
            self.write_standard_genres(list(standard_genres))
        for field, values in (hygiene_values or {}).items():
            self.write_hygiene_value(field, values)
        self._commit(desired, standard_genres, hygiene_values)
        return plan

    def _commit(
        self,
        desired: OwnedValues,
        standard_genres: tuple[str, ...] | None,
        hygiene_values: Mapping[str, list[str] | None] | None,
    ) -> None:
        """Save into a temporary copy, verify it, then swap it in with one atomic rename.

        Mutagen writes metadata in place, and growing a tag past the container's available
        padding rewrites the whole file. An interruption partway through that rewrite would
        leave the DJ's original damaged, and music files are not regenerable. So tag writes
        follow the same write-temp-then-replace discipline `model_store` already uses for
        downloads: the original is only ever exchanged for a candidate that has been written
        and read back successfully, and any failure leaves it untouched.

        The temporary keeps the original suffix so container detection behaves identically.
        """
        if not self.path.exists():
            # No original exists, so there is nothing an interrupted write could damage and
            # nothing to read a candidate back from. Write directly.
            self.audio.save()
            return

        temporary = self.path.with_name(
            f"{self.path.stem}{WRITE_TEMPORARY_MARKER}{self.path.suffix}"
        )
        try:
            shutil.copy2(self.path, temporary)
            self.audio.save(temporary)
            self._verify_candidate(temporary, desired, standard_genres, hygiene_values)
            # The rename is only atomic with respect to other processes. Without flushing
            # the candidate first, a power loss just after the swap can leave the name
            # pointing at a truncated file, and the original is already gone.
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _verify_candidate(
        self,
        candidate: Path,
        desired: OwnedValues,
        standard_genres: tuple[str, ...] | None,
        hygiene_values: Mapping[str, list[str] | None] | None,
    ) -> None:
        """Read the candidate back from disk and refuse to commit anything incomplete."""
        written = owned_tag_store(candidate)
        remaining = written.plan(desired)
        if remaining.changes:
            fields = ", ".join(change.field for change in remaining.changes)
            raise TagVerificationError(
                f"SetTag metadata verification failed for {self.path}: {fields}"
            )
        if standard_genres is not None and written.genre_state().standard != standard_genres:
            raise TagVerificationError(f"File genre tag verification failed for {self.path}")
        if hygiene_values is not None:
            remaining_hygiene = written.plan_hygiene(hygiene_values)
            if remaining_hygiene.changes:
                fields = ", ".join(change.field for change in remaining_hygiene.changes)
                raise TagVerificationError(
                    f"Metadata hygiene verification failed for {self.path}: {fields}"
                )

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

    @abstractmethod
    def read_hygiene_tags(self) -> tuple[HygieneTag, ...]:
        pass

    @abstractmethod
    def read_hygiene_value(self, field: str) -> list[str] | None:
        pass

    @abstractmethod
    def write_hygiene_value(self, field: str, values: list[str] | None) -> None:
        pass


_HYGIENE_NAME_CATEGORIES: tuple[tuple[tuple[str, ...], HygieneCategory], ...] = (
    (("encoder", "encoded by", "encoded-by", "encoding tool"), "encoder"),
    (("comment", "description", "note", "source"), "comment"),
    (("website", "url", "download"), "url"),
)


def _named_hygiene_category(name: str) -> HygieneCategory | None:
    normalized = name.strip().casefold().replace("_", " ")
    for markers, category in _HYGIENE_NAME_CATEGORIES:
        if any(marker in normalized for marker in markers):
            return category
    return None


def _id3_field(frame_id: str, *parts: str) -> str:
    encoded = ":".join(quote(part, safe="") for part in parts)
    return f"ID3:{frame_id}" + (f":{encoded}" if encoded else "")


def _parse_hygiene_field(field: str, expected: str) -> tuple[str, tuple[str, ...]]:
    parts = field.split(":")
    if len(parts) < 2 or parts[0] != expected:
        raise UnsupportedTagFormatError(f"Invalid {expected} hygiene field: {field}")
    return parts[1], tuple(unquote(part) for part in parts[2:])


def _id3_hash_key(frame_id: str, parts: tuple[str, ...]) -> str:
    if frame_id == "COMM" and len(parts) == 2:
        language, description = parts
        return f"COMM:{description}:{language}"
    if frame_id in {"TXXX", "WXXX"} and len(parts) == 1:
        return f"{frame_id}:{parts[0]}"
    if frame_id == "TSSE" and not parts:
        return "TSSE"
    raise UnsupportedTagFormatError(f"Invalid ID3 hygiene field: {frame_id}")


def _simple_hygiene_field(prefix: str, key: str) -> str:
    return f"{prefix}:{quote(key, safe='')}"


def _parse_simple_hygiene_field(field: str, expected: str) -> str:
    prefix = f"{expected}:"
    if not field.startswith(prefix):
        raise UnsupportedTagFormatError(f"Invalid {expected} hygiene field: {field}")
    return unquote(field[len(prefix) :])


def _mp4_text_values(values: Any) -> list[str] | None:
    decoded: list[str] = []
    for value in values:
        if isinstance(value, str):
            decoded.append(value)
        elif isinstance(value, bytes):
            try:
                decoded.append(value.decode("utf-8"))
            except UnicodeDecodeError:
                return None
        else:
            return None
    return decoded


def hygiene_field_label(field: str) -> str:
    """Turn a reversible adapter identifier into concise review/undo copy."""
    try:
        if field.startswith("ID3:"):
            frame_id, parts = _parse_hygiene_field(field, "ID3")
            if frame_id == "COMM" and len(parts) == 2:
                description = parts[1]
                return "Comment" if not description else f"Comment ({description})"
            if frame_id == "WXXX" and len(parts) == 1:
                return "URL" if not parts[0] else f"URL ({parts[0]})"
            if frame_id == "TXXX" and len(parts) == 1:
                return parts[0] or "Custom text"
            if frame_id == "TSSE":
                return "Encoder"
        if field.startswith("VORBIS:"):
            return _parse_simple_hygiene_field(field, "VORBIS")
        if field.startswith("MP4:"):
            key = _parse_simple_hygiene_field(field, "MP4")
            return {"\xa9cmt": "Comment", "\xa9too": "Encoder"}.get(
                key,
                key.rsplit(":", 1)[-1],
            )
    except UnsupportedTagFormatError:
        pass
    return field


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

    def read_hygiene_tags(self) -> tuple[HygieneTag, ...]:
        tags = self.audio.tags
        if tags is None:
            return ()
        if not isinstance(tags, ID3):
            raise UnsupportedTagFormatError(f"Expected ID3 metadata in {self.path}")

        cleanable: list[HygieneTag] = []
        for frame in tags.values():
            if isinstance(frame, COMM):
                label = "Comment" if not frame.desc else f"Comment ({frame.desc})"
                cleanable.append(
                    HygieneTag(
                        field=_id3_field("COMM", frame.lang, frame.desc),
                        label=label,
                        category="comment",
                        values=tuple(str(value) for value in frame.text),
                    )
                )
            elif isinstance(frame, WXXX):
                label = "URL" if not frame.desc else f"URL ({frame.desc})"
                cleanable.append(
                    HygieneTag(
                        field=_id3_field("WXXX", frame.desc),
                        label=label,
                        category="url",
                        values=(str(frame.url),),
                    )
                )
            elif isinstance(frame, TSSE):
                cleanable.append(
                    HygieneTag(
                        field=_id3_field("TSSE"),
                        label="Encoder",
                        category="encoder",
                        values=tuple(str(value) for value in frame.text),
                    )
                )
            elif isinstance(frame, TXXX):
                category = _named_hygiene_category(frame.desc)
                if category is None or frame.desc.startswith("SETTAG_"):
                    continue
                cleanable.append(
                    HygieneTag(
                        field=_id3_field("TXXX", frame.desc),
                        label=frame.desc or "Custom text",
                        category=category,
                        values=tuple(str(value) for value in frame.text),
                    )
                )
        return tuple(cleanable)

    def read_hygiene_value(self, field: str) -> list[str] | None:
        tags = self.audio.tags
        if tags is None:
            return None
        if not isinstance(tags, ID3):
            raise UnsupportedTagFormatError(f"Expected ID3 metadata in {self.path}")
        frame_id, parts = _parse_hygiene_field(field, "ID3")
        hash_key = _id3_hash_key(frame_id, parts)
        frame = tags.get(hash_key)
        if frame is None:
            return None
        if isinstance(frame, WXXX):
            return [str(frame.url)]
        text = getattr(frame, "text", None)
        if not isinstance(text, list):
            raise UnsupportedTagFormatError(f"Expected text metadata in {field} in {self.path}")
        return [str(value) for value in text]

    def write_hygiene_value(self, field: str, values: list[str] | None) -> None:
        tags = self.ensure_tags()
        if not isinstance(tags, ID3):
            raise UnsupportedTagFormatError(f"Expected ID3 metadata in {self.path}")
        frame_id, parts = _parse_hygiene_field(field, "ID3")
        hash_key = _id3_hash_key(frame_id, parts)
        tags.delall(hash_key)
        if not values:
            return
        if frame_id == "COMM":
            language, description = parts
            tags.add(
                COMM(
                    encoding=ENCODING_UTF8,
                    lang=language,
                    desc=description,
                    text=values,
                )
            )
        elif frame_id == "TXXX":
            tags.add(TXXX(encoding=ENCODING_UTF8, desc=parts[0], text=values))
        elif frame_id == "WXXX":
            if len(values) != 1:
                raise UnsupportedTagFormatError(f"Expected one URL value for {field}")
            tags.add(WXXX(encoding=ENCODING_UTF8, desc=parts[0], url=values[0]))
        elif frame_id == "TSSE":
            tags.add(TSSE(encoding=ENCODING_UTF8, text=values))
        else:  # pragma: no cover - guarded by _id3_hash_key
            raise UnsupportedTagFormatError(f"Unsupported ID3 hygiene field {field}")


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

    def read_hygiene_tags(self) -> tuple[HygieneTag, ...]:
        tags = self.audio.tags
        if tags is None:
            return ()
        cleanable: list[HygieneTag] = []
        for raw_key, raw_values in tags.items():
            key = str(raw_key)
            category = _named_hygiene_category(key)
            if category is None or key.casefold().startswith("settag_"):
                continue
            cleanable.append(
                HygieneTag(
                    field=_simple_hygiene_field("VORBIS", key),
                    label=key,
                    category=category,
                    values=tuple(str(value) for value in raw_values),
                )
            )
        return tuple(cleanable)

    def read_hygiene_value(self, field: str) -> list[str] | None:
        key = _parse_simple_hygiene_field(field, "VORBIS")
        tags = self.audio.tags
        if tags is None or key not in tags:
            return None
        return [str(value) for value in tags[key]]

    def write_hygiene_value(self, field: str, values: list[str] | None) -> None:
        key = _parse_simple_hygiene_field(field, "VORBIS")
        tags = self.ensure_tags()
        if key in tags:
            del tags[key]
        if values:
            tags[key] = values


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

    def read_hygiene_tags(self) -> tuple[HygieneTag, ...]:
        tags = self.audio.tags
        if tags is None:
            return ()
        cleanable: list[HygieneTag] = []
        for raw_key, raw_values in tags.items():
            key = str(raw_key)
            if key == "\xa9cmt":
                label, category = "Comment", "comment"
            elif key == "\xa9too":
                label, category = "Encoder", "encoder"
            elif key.startswith("----:"):
                name = key.rsplit(":", 1)[-1]
                category = _named_hygiene_category(name)
                if category is None:
                    continue
                label = name
            else:
                continue
            values = _mp4_text_values(raw_values)
            if values is None:
                continue
            cleanable.append(
                HygieneTag(
                    field=_simple_hygiene_field("MP4", key),
                    label=label,
                    category=category,
                    values=tuple(values),
                )
            )
        return tuple(cleanable)

    def read_hygiene_value(self, field: str) -> list[str] | None:
        key = _parse_simple_hygiene_field(field, "MP4")
        tags = self.audio.tags
        if tags is None or key not in tags:
            return None
        values = _mp4_text_values(tags[key])
        if values is None:
            raise UnsupportedTagFormatError(f"Expected text metadata in {key} in {self.path}")
        return values

    def write_hygiene_value(self, field: str, values: list[str] | None) -> None:
        key = _parse_simple_hygiene_field(field, "MP4")
        tags = self.ensure_tags()
        if key in tags:
            del tags[key]
        if not values:
            return
        if key.startswith("----:"):
            tags[key] = [
                MP4FreeForm(value.encode("utf-8"), dataformat=AtomDataType.UTF8) for value in values
            ]
        else:
            tags[key] = values


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


def read_hygiene_tags(path: Path) -> tuple[HygieneTag, ...]:
    return owned_tag_store(path).read_hygiene_tags()


def plan_hygiene_tags(
    path: Path,
    desired: Mapping[str, list[str] | None],
) -> TagPlan:
    return owned_tag_store(path).plan_hygiene(desired)


def read_duration_seconds(path: Path) -> float | None:
    """Return the track length from its container, without decoding audio.

    ``None`` when the container does not report one. Callers treat an unknown
    length as analyzable rather than guessing, so a missing duration never
    silently excludes a track.
    """
    try:
        parsed = mutagen.File(path)
    except Exception:
        return None
    length = getattr(getattr(parsed, "info", None), "length", None)
    if not isinstance(length, (int, float)) or length <= 0:
        return None
    return float(length)


def apply_metadata_tags(
    path: Path,
    desired: OwnedValues,
    *,
    standard_genres: tuple[str, ...] | None = None,
    expected_plan: TagPlan | None = None,
    expected_standard: tuple[str, ...] | None = None,
    expected_standard_change: TagChange | None = None,
    hygiene_values: Mapping[str, list[str] | None] | None = None,
    expected_hygiene_plan: TagPlan | None = None,
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
    current_hygiene_plan = store.plan_hygiene(hygiene_values or {})
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
    if expected_hygiene_plan is not None and current_hygiene_plan != expected_hygiene_plan:
        raise TagStateChangedError(
            f"Metadata hygiene state changed after planning and before writing {path}"
        )

    plan = store.apply(
        desired,
        standard_genres=standard_genres,
        hygiene_values=hygiene_values,
    )
    remaining = owned_tag_store(path).plan(desired)
    if remaining.changes:
        fields = ", ".join(change.field for change in remaining.changes)
        raise TagVerificationError(f"SetTag metadata verification failed for {path}: {fields}")
    if standard_genres is not None:
        after = owned_tag_store(path).genre_state().standard
        if after != standard_genres:
            raise TagVerificationError(f"File genre tag verification failed for {path}")
    if hygiene_values is not None:
        remaining_hygiene = owned_tag_store(path).plan_hygiene(hygiene_values)
        if remaining_hygiene.changes:
            fields = ", ".join(change.field for change in remaining_hygiene.changes)
            raise TagVerificationError(f"Metadata hygiene verification failed for {path}: {fields}")
    return plan
