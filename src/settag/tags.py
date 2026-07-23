from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mutagen.id3 import ID3, TXXX, ID3NoHeaderError

from settag.policy import Prediction

ENCODING_UTF8 = 3


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


def build_owned_values(
    selected: list[Prediction],
    *,
    model_id: str,
    analyzed_at: str,
    config_sha256: str,
) -> dict[str, list[str] | None]:
    genres = [item.label for item in selected]
    scores = json.dumps(
        [item.to_dict() for item in selected],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "SETTAG_GENRE": genres or None,
        "SETTAG_GENRE_SCORES": [scores] if genres else None,
        "SETTAG_MODEL": [model_id],
        "SETTAG_ANALYZED_AT": [analyzed_at],
        "SETTAG_CONFIG_SHA256": [config_sha256],
    }


def plan_owned_tags(
    path: Path,
    desired: dict[str, list[str] | None],
) -> list[TagChange]:
    tags = _load_id3(path)
    return _plan_with_tags(tags, desired)


def _plan_with_tags(
    tags: ID3,
    desired: dict[str, list[str] | None],
) -> list[TagChange]:
    changes: list[TagChange] = []
    for description, after in desired.items():
        before = _read_txxx(tags, description)
        if before != after:
            changes.append(
                TagChange(
                    field=f"TXXX:{description}",
                    before=before,
                    after=after,
                )
            )
    return changes


def apply_owned_tags(
    path: Path,
    desired: dict[str, list[str] | None],
) -> list[TagChange]:
    tags = _load_id3(path)
    changes = _plan_with_tags(tags, desired)
    if not changes:
        return []

    for description, values in desired.items():
        tags.delall(f"TXXX:{description}")
        if values is not None:
            tags.add(TXXX(encoding=ENCODING_UTF8, desc=description, text=values))
    tags.save(path)
    return changes


def _load_id3(path: Path) -> ID3:
    try:
        return ID3(path)
    except ID3NoHeaderError:
        return ID3()


def _read_txxx(tags: ID3, description: str) -> list[str] | None:
    frames = tags.getall(f"TXXX:{description}")
    if not frames:
        return None
    return [str(value) for frame in frames for value in frame.text]
