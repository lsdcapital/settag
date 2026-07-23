import json
import wave
from pathlib import Path
from typing import Any

import pytest
from mutagen.id3 import APIC, ID3, TCON, TIT2, TXXX
from mutagen.mp4 import MP4FreeForm
from mutagen.wave import WAVE

from settag import __version__
from settag.policy import Prediction
from settag.tags import (
    MP4_MEAN,
    GenreState,
    Mp4OwnedTagStore,
    UnsupportedTagFormatError,
    VorbisOwnedTagStore,
    apply_owned_tags,
    build_owned_values,
    plan_owned_tags,
    read_genre_state,
)


class FakeAudio:
    def __init__(self, tags: dict[str, Any] | None = None) -> None:
        self.tags = tags
        self.pictures = [b"original artwork"]
        self.save_count = 0

    def add_tags(self) -> None:
        self.tags = {}

    def save(self) -> None:
        self.save_count += 1


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 80)


def _tagged_wav(path: Path) -> None:
    _silent_wav(path)
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TIT2(encoding=3, text=["Original title"]))
    audio.tags.add(TCON(encoding=3, text=["Existing genre"]))
    audio.tags.add(TXXX(encoding=3, desc="OTHER_TOOL", text=["keep me"]))
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, data=b"original artwork"))
    audio.save()


def _desired(
    selected: list[Prediction] | None = None,
    *,
    config_sha256: str = "abc123",
) -> dict[str, list[str] | None]:
    if selected is None:
        selected = [Prediction("Electronic---Deep House", 0.72)]
    return build_owned_values(
        selected,
        model_id="model/v1",
        analyzed_at="2026-07-23T12:00:00Z",
        config_sha256=config_sha256,
    )


def test_owned_genres_and_scores_have_identical_membership_and_order() -> None:
    selected = [
        Prediction("Electronic---Deep House", 0.72),
        Prediction("Electronic---House", 0.51),
    ]

    desired = _desired(selected)
    scores = json.loads(desired["SETTAG_GENRE_SCORES"][0])

    assert desired["SETTAG_GENRE"] == [item.label for item in selected]
    assert [item["label"] for item in scores] == desired["SETTAG_GENRE"]
    assert [item["score"] for item in scores] == [item.score for item in selected]
    assert desired["SETTAG_VERSION"] == [__version__]


def test_id3_write_preserves_standard_and_unowned_tags(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _tagged_wav(path)
    desired = _desired()

    before = read_genre_state(path)
    planned = plan_owned_tags(path, desired)
    applied = apply_owned_tags(path, desired)
    after = read_genre_state(path)
    tags = WAVE(path).tags

    assert before == GenreState(standard=("Existing genre",), settag=())
    assert after == GenreState(
        standard=("Existing genre",),
        settag=("Electronic---Deep House",),
    )
    assert applied == planned
    assert planned.format == "id3"
    assert tags is not None
    assert tags["TIT2"].text == ["Original title"]
    assert tags["TCON"].text == ["Existing genre"]
    assert tags["TXXX:OTHER_TOOL"].text == ["keep me"]
    assert tags.getall("APIC:")[0].data == b"original artwork"
    assert tags["TXXX:SETTAG_GENRE"].text == ["Electronic---Deep House"]
    assert tags["TXXX:SETTAG_VERSION"].text == [__version__]
    assert tags["TXXX:SETTAG_MODEL"].text == ["model/v1"]


def test_empty_result_removes_only_stale_owned_genre(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _tagged_wav(path)
    apply_owned_tags(path, _desired())

    empty = _desired([], config_sha256="new")
    apply_owned_tags(path, empty)
    tags = WAVE(path).tags

    assert tags is not None
    assert tags.get("TXXX:SETTAG_GENRE") is None
    assert tags.get("TXXX:SETTAG_GENRE_SCORES") is None
    assert tags["TCON"].text == ["Existing genre"]
    assert tags["TXXX:SETTAG_CONFIG_SHA256"].text == ["new"]


def test_vorbis_store_uses_namespaced_comments_and_preserves_other_fields(
    tmp_path: Path,
) -> None:
    audio = FakeAudio(
        {
            "TITLE": ["Original title"],
            "GENRE": ["Existing genre"],
            "SETTAG_MODEL": ["old/model"],
        }
    )
    store = VorbisOwnedTagStore(tmp_path / "track.flac", audio)

    before = store.genre_state()
    plan = store.apply(_desired())

    assert before == GenreState(standard=("Existing genre",), settag=())
    assert plan.format == "vorbis-comments"
    assert audio.tags is not None
    assert audio.tags["TITLE"] == ["Original title"]
    assert audio.tags["GENRE"] == ["Existing genre"]
    assert audio.pictures == [b"original artwork"]
    assert audio.tags["SETTAG_GENRE"] == ["Electronic---Deep House"]
    assert audio.tags["SETTAG_VERSION"] == [__version__]
    assert audio.tags["SETTAG_MODEL"] == ["model/v1"]
    assert audio.save_count == 1


def test_mp4_store_uses_namespaced_freeform_atoms(tmp_path: Path) -> None:
    audio = FakeAudio(
        {
            "\xa9nam": ["Original title"],
            "\xa9gen": ["Existing genre"],
            "covr": [b"original artwork"],
        }
    )
    store = Mp4OwnedTagStore(tmp_path / "track.m4a", audio)

    before = store.genre_state()
    plan = store.apply(_desired())
    genre_key = f"----:{MP4_MEAN}:GENRE"
    version_key = f"----:{MP4_MEAN}:VERSION"
    model_key = f"----:{MP4_MEAN}:MODEL"

    assert before == GenreState(standard=("Existing genre",), settag=())
    assert plan.format == "mp4-freeform"
    assert audio.tags is not None
    assert audio.tags["\xa9nam"] == ["Original title"]
    assert audio.tags["\xa9gen"] == ["Existing genre"]
    assert audio.tags["covr"] == [b"original artwork"]
    assert all(isinstance(item, MP4FreeForm) for item in audio.tags[genre_key])
    assert [bytes(item).decode() for item in audio.tags[genre_key]] == ["Electronic---Deep House"]
    assert [bytes(item).decode() for item in audio.tags[version_key]] == [__version__]
    assert [bytes(item).decode() for item in audio.tags[model_key]] == ["model/v1"]
    assert audio.save_count == 1


def test_unchanged_owned_values_do_not_rewrite_file(tmp_path: Path) -> None:
    desired = _desired()
    audio = FakeAudio({key: value for key, value in desired.items() if value is not None})
    store = VorbisOwnedTagStore(tmp_path / "track.flac", audio)

    plan = store.apply(desired)

    assert plan.changes == ()
    assert audio.save_count == 0


def test_unrecognized_container_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "track.mp3"
    path.write_text("not audio", encoding="utf-8")

    with pytest.raises(UnsupportedTagFormatError, match="metadata container"):
        plan_owned_tags(path, _desired())
