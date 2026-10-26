from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
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
from settag.beatport import TrackIdentity
from settag.hashing import sha256_json, sha256_untagged
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
    "SETTAG_BEATPORT",
    "SETTAG_ENRICHMENT",
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


class TagWriteRefusedError(RuntimeError):
    """The file is one a copy-and-replace write would change in ways beyond its tags."""


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

    def __init__(self, path: Path, audio: Any, loaded_stat: os.stat_result | None = None) -> None:
        self.path = path
        self.audio = audio
        # Taken before parsing, so any change after the tags were read shows up as a
        # different stat when the candidate is about to replace the original.
        self.loaded_stat = loaded_stat

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
            self._save()
            return

        original = self.loaded_stat or self.path.stat()
        _refuse_unsafe_replacement(self.path, original)
        _sweep_stale_temporaries(self.path.parent)
        temporary = _temporary_for(self.path)
        try:
            shutil.copy2(self.path, temporary)
            self._save(temporary)
            _copy_extended_attributes(self.path, temporary)
            self._verify_candidate(temporary, desired, standard_genres, hygiene_values)
            _keep_file_identity(temporary, original)
            # The rename is only atomic with respect to other processes. Without flushing
            # the candidate first, a power loss just after the swap can leave the name
            # pointing at a truncated file, and the original is already gone.
            with temporary.open("rb") as handle:
                _full_fsync(handle.fileno())
            # Copying, saving and verifying a large WAV can take seconds. Another tool
            # writing the original in that window would be silently overwritten, so
            # check once more as close to the swap as possible.
            self._check_unchanged_since_load()
            os.replace(temporary, self.path)
            # The rename itself lives in the directory. Until that is flushed, a power loss
            # can bring the old file back even though the journal records the write.
            _fsync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _check_unchanged_since_load(self) -> None:
        if self.loaded_stat is None:
            return
        current = self.path.stat()
        before = self.loaded_stat
        if (current.st_size, current.st_mtime_ns, current.st_ino) != (
            before.st_size,
            before.st_mtime_ns,
            before.st_ino,
        ):
            raise TagStateChangedError(f"{self.path} changed on disk while SetTag was writing it")

    def _save(self, target: Path | None = None) -> None:
        if target is None:
            self.audio.save()
        else:
            self.audio.save(target)

    def _verify_candidate(
        self,
        candidate: Path,
        desired: OwnedValues,
        standard_genres: tuple[str, ...] | None,
        hygiene_values: Mapping[str, list[str] | None] | None,
    ) -> None:
        """Read the candidate back from disk and refuse to commit anything incomplete,
        or anything that changed more than it was asked to."""
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
        self._verify_nothing_else_changed(
            candidate, written, desired, standard_genres, hygiene_values
        )

    def _verify_nothing_else_changed(
        self,
        candidate: Path,
        written: OwnedTagStore,
        desired: OwnedValues,
        standard_genres: tuple[str, ...] | None,
        hygiene_values: Mapping[str, list[str] | None] | None,
    ) -> None:
        """Refuse a candidate that differs from the original anywhere it was not asked to.

        Checking only the fields SetTag wrote would let a library bug drop an unrelated
        item unnoticed: artwork, or the Serato cue points and beatgrids kept in ID3 GEOB
        frames. So every other tag item, every non-tag metadata block, and the audio must
        read back exactly as the original has them.
        """
        # The original is re-read because in memory the write has already been applied.
        # Confirming it is unchanged first means any difference below is the candidate's,
        # not another app's.
        self._check_unchanged_since_load()
        original = owned_tag_store(self.path)
        touched = self._touched_keys(desired, standard_genres, hygiene_values)
        before = original._preserved_items(touched)
        after = written._preserved_items(touched)
        if before != after:
            changed = ", ".join(sorted({key for key, _ in set(before) ^ set(after)}))
            raise TagVerificationError(
                f"Metadata verification failed for {self.path}: "
                f"the write would also have changed {changed}"
            )
        if sha256_untagged(candidate) != sha256_untagged(self.path):
            raise TagVerificationError(
                f"Audio verification failed for {self.path}: "
                "the write would have changed the audio data"
            )

    def _touched_keys(
        self,
        desired: OwnedValues,
        standard_genres: tuple[str, ...] | None,
        hygiene_values: Mapping[str, list[str] | None] | None,
    ) -> frozenset[str]:
        keys = {self._item_key(self.field_name(description)) for description in desired}
        if standard_genres is not None:
            keys.add(self._item_key(self.standard_genre_field_name()))
        keys.update(self._hygiene_item_key(field) for field in hygiene_values or {})
        return frozenset(keys)

    def _item_key(self, name: str) -> str:
        """The key a tag item is compared under; see `_preserved_items`."""
        return name

    @abstractmethod
    def _hygiene_item_key(self, field: str) -> str:
        pass

    @abstractmethod
    def _preserved_items(self, touched: frozenset[str]) -> list[tuple[str, str]]:
        """Every tag item and metadata block a write must leave alone, as sorted
        (key, fingerprint) pairs, excluding the items whose keys are in ``touched``."""

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

    def _save(self, target: Path | None = None) -> None:
        """Save in the tag's original ID3 version.

        Mutagen saves v2.4 by default, and converting a v2.3 tag drops frames with no
        v2.4 equivalent (TSIZ, TRDA, RVAD, EQUA, unknown frames) that undo cannot bring
        back. `owned_tag_store` loads v2.3 tags without translating them, so saving as
        v2.3 leaves every unrelated frame as it was. Multiple values are written
        null-separated rather than joined with "/", which would be ambiguous for genres
        like "Deep/House" and fail verification.
        """
        tags = self.audio.tags
        if isinstance(tags, ID3) and tags.version[:2] == (2, 3):
            options: dict[str, Any] = {"v2_version": 3, "v23_sep": None}
            if target is None:
                self.audio.save(**options)
            else:
                self.audio.save(target, **options)
            return
        super()._save(target)

    def field_name(self, description: str) -> str:
        return f"TXXX:{description}"

    def _hygiene_item_key(self, field: str) -> str:
        return _id3_hash_key(*_parse_hygiene_field(field, "ID3"))

    def _preserved_items(self, touched: frozenset[str]) -> list[tuple[str, str]]:
        tags = self.audio.tags
        if tags is None:
            return []
        items = [
            (frame.HashKey, _frame_fingerprint(frame))
            for frame in tags.values()
            if frame.HashKey not in touched
        ]
        # Frames mutagen cannot parse are carried through a save as raw bytes.
        items.extend(("unknown frame", repr(raw)) for raw in getattr(tags, "unknown_frames", ()))
        return sorted(items)

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

    def _item_key(self, name: str) -> str:
        # Vorbis comment names are case-insensitive.
        return name.casefold()

    def _hygiene_item_key(self, field: str) -> str:
        return self._item_key(_parse_simple_hygiene_field(field, "VORBIS"))

    def _preserved_items(self, touched: frozenset[str]) -> list[tuple[str, str]]:
        tags = self.audio.tags
        items = [
            (self._item_key(str(key)), str(value))
            for key, value in (tags if tags is not None else ())
            if self._item_key(str(key)) not in touched
        ]
        # Pictures, cue sheets, seek tables and application blocks sit beside the
        # comments and are rewritten with them.
        items.extend(
            (type(block).__name__, hashlib.sha256(block.write()).hexdigest())
            for block in getattr(self.audio, "metadata_blocks", ())
            if block.code not in (_FLAC_PADDING, _FLAC_VORBIS_COMMENT)
        )
        return sorted(items)

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
        # Vorbis keys are case-insensitive and mutagen's deletion is too, so this removes
        # every GENRE/Genre/genre comment. Iterating the dict would yield (key, value) pairs.
        if "GENRE" in tags:
            del tags["GENRE"]
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

    def _hygiene_item_key(self, field: str) -> str:
        return _parse_simple_hygiene_field(field, "MP4")

    def _preserved_items(self, touched: frozenset[str]) -> list[tuple[str, str]]:
        tags = self.audio.tags
        if tags is None:
            return []
        return sorted((str(key), repr(value)) for key, value in tags.items() if key not in touched)

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
                try:
                    values.append(value.decode("utf-8"))
                except UnicodeDecodeError as error:
                    raise UnsupportedTagFormatError(
                        f"SetTag-owned MP4 atom {key} is not UTF-8 text in {self.path}"
                    ) from error
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
        # Kept values are only ever text that decoded as UTF-8, but the atom may have been
        # stored as implicit rather than explicit UTF-8. Keep its type so a partial cleanup
        # does not change how other software classifies the atom.
        existing = {getattr(value, "dataformat", None) for value in tags.get(key, ())}
        if key in tags:
            del tags[key]
        if not values:
            return
        if key.startswith("----:"):
            dataformat = (
                existing.pop()
                if existing in ({AtomDataType.UTF8}, {AtomDataType.IMPLICIT})
                else AtomDataType.UTF8
            )
            tags[key] = [
                MP4FreeForm(value.encode("utf-8"), dataformat=dataformat) for value in values
            ]
        else:
            tags[key] = values


_FLAC_PADDING = 1
_FLAC_VORBIS_COMMENT = 4


def _frame_fingerprint(frame: Any) -> str:
    """Describe an ID3 frame by its content.

    The text encoding is left out: saving a v2.3 tag re-encodes text into one v2.3 can
    store, which changes the bytes but not what the frame says.
    """
    copy = type(frame)(frame)
    if hasattr(copy, "encoding"):
        copy.encoding = ENCODING_UTF8
    return repr(copy)


def _refuse_unsafe_replacement(path: Path, original: os.stat_result) -> None:
    """Refuse a write that replacing the file would turn into more than a tag change.

    A rename swaps the directory entry, not the file. Other hard links to the original
    would keep pointing at it, so those names would silently keep the old tags. And the
    replacement belongs to whoever wrote it, so a file owned by another user would change
    hands, which only root can undo.
    """
    if original.st_nlink > 1:
        raise TagWriteRefusedError(
            f"{path} has {original.st_nlink} hard links; replacing it would leave the "
            "other names with the old tags"
        )
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() not in (0, original.st_uid):
        raise TagWriteRefusedError(
            f"{path} belongs to another user; replacing it would take it away from them"
        )


def _keep_file_identity(candidate: Path, original: os.stat_result) -> None:
    """Give the candidate the original's group and creation date, as an in-place write would.

    Best effort, like the extended attributes: a group the user does not belong to cannot
    be kept, and the file is still theirs. The owner already matches unless SetTag runs as
    root, which is the one case it can be restored in.
    """
    with suppress(OSError, AttributeError):
        os.chown(candidate, original.st_uid, original.st_gid)
    _keep_creation_time(candidate, original)


# setattrlist(2) constants and structures, for the creation date.
_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMN_CRTIME = 0x00000200
_FSOPT_NOFOLLOW = 0x00000001


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def _keep_creation_time(candidate: Path, original: os.stat_result) -> None:
    """Carry the original's creation date, which Finder shows as Date Created, over.

    A replacement is a new file to the filesystem. `copy2` gives it the original's
    modification time, and APFS pulls a later creation date back to that, so without
    this every tagged track's Date Created would become its last-modified time.
    Only macOS can set a creation date; Linux exposes none to set.
    """
    birthtime = getattr(original, "st_birthtime", None)
    if sys.platform != "darwin" or birthtime is None:
        return
    with suppress(Exception):
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        seconds = int(birthtime)
        nanoseconds = min(round((birthtime - seconds) * 1_000_000_000), 999_999_999)
        attributes = _AttrList(bitmapcount=_ATTR_BIT_MAP_COUNT, commonattr=_ATTR_CMN_CRTIME)
        value = _Timespec(seconds, nanoseconds)
        libc.setattrlist(
            os.fsencode(candidate),
            ctypes.byref(attributes),
            ctypes.byref(value),
            ctypes.sizeof(value),
            _FSOPT_NOFOLLOW,
        )


# A write takes seconds, so a candidate this old was abandoned by a process that died.
STALE_TEMPORARY_SECONDS = 60 * 60

# Candidate names `_temporary_for` makes, and the visible name earlier versions used.
_TEMPORARY_NAME = re.compile(
    rf"^(?:\..+{re.escape(WRITE_TEMPORARY_MARKER)}-\d+|.+{re.escape(WRITE_TEMPORARY_MARKER)})"
    rf"\.[A-Za-z0-9]+$",
    re.ASCII,
)

# Directories already swept by this process. Sweeping once per directory, rather than
# once per write, keeps a batch over one large folder from listing it for every file.
_swept_directories: set[Path] = set()


def _temporary_for(path: Path) -> Path:
    """Name the candidate a write is built in.

    Hidden, so a candidate abandoned by a hard kill does not show up in Finder or in a
    DJ app's folder import, and unique to the process, so two runs never share one. It
    keeps the original suffix so container detection behaves identically.
    """
    return path.with_name(f".{path.stem}{WRITE_TEMPORARY_MARKER}-{os.getpid()}{path.suffix}")


def _sweep_stale_temporaries(directory: Path) -> None:
    """Delete candidates a crashed write left in ``directory``.

    The scanner already ignores them, but each is a full copy of a track. Only names of
    exactly the candidate shape are touched, and only once no write can still be using
    them.
    """
    if directory in _swept_directories:
        return
    _swept_directories.add(directory)
    cutoff = time.time() - STALE_TEMPORARY_SECONDS
    with suppress(OSError):
        for entry in os.scandir(directory):
            if not _TEMPORARY_NAME.match(entry.name):
                continue
            with suppress(OSError):
                if (
                    entry.is_file(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff
                ):
                    os.unlink(entry.path)


# copyfile(3) flags. COPYFILE_STAT is deliberately absent: it would copy the original's
# mtime onto the candidate, hiding the write from other tools and from undo's staleness check.
_COPYFILE_ACL = 1 << 0
_COPYFILE_XATTR = 1 << 2


def _copy_extended_attributes(source: Path, target: Path) -> None:
    """Carry Finder tags and other extended attributes over to the candidate.

    ``shutil.copy2`` already does this on Linux, but CPython has no ``os.listxattr`` on
    macOS, so a DJ's colour tags and comments set in Finder would vanish on every write.
    Best effort: failing here leaves the file exactly as the old behaviour did.
    """
    if sys.platform != "darwin":
        return
    with suppress(Exception):
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.copyfile(
            os.fsencode(source), os.fsencode(target), None, _COPYFILE_ACL | _COPYFILE_XATTR
        )


def _full_fsync(fd: int) -> None:
    """Flush to stable storage. On macOS plain fsync stops at the drive's cache."""
    full_fsync = getattr(fcntl, "F_FULLFSYNC", None)
    if full_fsync is not None:
        try:
            fcntl.fcntl(fd, full_fsync)
            return
        except OSError:
            pass
    os.fsync(fd)


def _fsync_directory(directory: Path) -> None:
    # Not every filesystem allows opening or syncing a directory; the rename has already
    # happened, so this can only improve durability, never fail the write.
    with suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def owned_tag_store(path: Path) -> OwnedTagStore:
    try:
        loaded_stat = path.stat()
    except OSError:
        loaded_stat = None
    try:
        audio = mutagen.File(path)
        if (
            isinstance(audio, (MP3, AIFF, WAVE))
            and isinstance(audio.tags, ID3)
            and audio.tags.version[:2] == (2, 3)
        ):
            # Reload so v2.3 frames are kept as-is instead of translated to v2.4.
            audio = type(audio)(path, v2_version=3)
    except mutagen.MutagenError as error:
        raise UnsupportedTagFormatError(f"Could not read metadata container: {path}") from error

    if audio is None:
        raise UnsupportedTagFormatError(f"Unrecognized metadata container: {path}")
    if isinstance(audio, (MP3, AIFF, WAVE)):
        return Id3OwnedTagStore(path, audio, loaded_stat)
    if isinstance(audio, FLAC):
        return VorbisOwnedTagStore(path, audio, loaded_stat)
    if isinstance(audio, MP4):
        return Mp4OwnedTagStore(path, audio, loaded_stat)

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


def track_identity(store: OwnedTagStore) -> TrackIdentity:
    tags = store.audio.tags

    def values(id3: str, vorbis: str, mp4: str) -> tuple[str, ...]:
        if tags is None:
            return ()
        if isinstance(tags, ID3):
            frame = tags.get(id3)
            return tuple(str(v).strip() for v in getattr(frame, "text", ()) if str(v).strip())
        key = mp4 if isinstance(store.audio, MP4) else vorbis
        return tuple(
            (v.decode("utf-8") if isinstance(v, bytes) else str(v)).strip()
            for v in tags.get(key, ())
        )

    def first(id3: str, vorbis: str, mp4: str) -> str:
        return next(iter(values(id3, vorbis, mp4)), "")

    return TrackIdentity(
        first("TIT2", "title", "\xa9nam"),
        values("TPE1", "artist", "\xa9ART"),
        getattr(store.audio.info, "length", None),
        first("TSRC", "isrc", "----:com.apple.iTunes:ISRC"),
        first(
            "TXXX:BEATPORT_TRACK_ID", "BEATPORT_TRACK_ID", "----:com.apple.iTunes:BEATPORT_TRACK_ID"
        ),
    )


def track_identity_sha256(store: OwnedTagStore) -> str:
    return sha256_json(asdict(track_identity(store)))


def read_track_metadata(path: Path) -> tuple[GenreState, OwnedValues, float | None, str]:
    """Read startup metadata from a single parsed container."""
    store = owned_tag_store(path)
    owned = {description: store.read_value(description) for description in OWNED_DESCRIPTIONS}
    return store.genre_state(), owned, _duration_seconds(store.audio), track_identity_sha256(store)


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
    return _duration_seconds(parsed)


def _duration_seconds(parsed: Any) -> float | None:
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
