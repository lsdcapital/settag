import json
import shutil
import wave
from pathlib import Path
from typing import Any

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3, TCON, TIT2, TXXX
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.wave import WAVE

from settag import __version__
from settag.policy import Prediction
from settag.tags import (
    MP4_MEAN,
    OWNED_DESCRIPTIONS,
    GenreState,
    Mp4OwnedTagStore,
    OwnedTagStore,
    TagStateChangedError,
    UnsupportedTagFormatError,
    VorbisOwnedTagStore,
    apply_metadata_tags,
    build_task_owned_values,
    plan_owned_tags,
    plan_standard_genres,
    read_genre_state,
    read_owned_values,
)
from settag.tasks import AnalysisTask

FIXTURES = Path(__file__).parent / "fixtures"


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
    return build_task_owned_values(
        dict.fromkeys(OWNED_DESCRIPTIONS),
        {"genre": selected},
        {
            "genre": {
                "model": {"schema": "settag.models/v1", "id": "model/v1", "files": {}},
                "analyzed_at": "2026-07-23T12:00:00Z",
                "config": {"sha256": config_sha256},
            }
        },
    )


def _copy_fixture(name: str, tmp_path: Path) -> Path:
    path = tmp_path / name
    shutil.copyfile(FIXTURES / name, path)
    return path


def test_owned_genres_and_scores_have_identical_membership_and_order() -> None:
    selected = [
        Prediction("Electronic---Deep House", 0.72),
        Prediction("Electronic---House", 0.51),
    ]

    desired = _desired(selected)
    serialized_scores = desired["SETTAG_GENRE_SCORES"]
    assert serialized_scores is not None
    scores = json.loads(serialized_scores[0])

    assert desired["SETTAG_GENRE"] == [item.label for item in selected]
    assert [item["label"] for item in scores] == desired["SETTAG_GENRE"]
    assert [item["score"] for item in scores] == [item.score for item in selected]
    assert desired["SETTAG_VERSION"] == [__version__]


def test_real_flac_round_trip_preserves_standard_tags_and_value_order(tmp_path: Path) -> None:
    path = _copy_fixture("tagged.flac", tmp_path)
    selected = [
        Prediction("Electronic---Deep House", 0.72),
        Prediction("Electronic---House", 0.51),
    ]
    desired = _desired(selected)
    before = FLAC(path)
    before_stream = (before.info.sample_rate, before.info.channels, before.info.total_samples)

    assert read_genre_state(path) == GenreState(
        standard=("Existing genre",),
        settag=(),
    )
    planned = plan_owned_tags(path, desired)
    applied = apply_metadata_tags(path, desired)
    audio = FLAC(path)

    assert applied == planned
    assert planned.format == "vorbis-comments"
    assert len(planned.changes) == 7
    assert audio["title"] == ["Original title"]
    assert audio["genre"] == ["Existing genre"]
    assert audio["SETTAG_GENRE"] == [item.label for item in selected]
    assert audio["SETTAG_VERSION"] == [__version__]
    assert audio["SETTAG_MODEL"] == ["model/v1"]
    scores = json.loads(audio["SETTAG_GENRE_SCORES"][0])
    assert [item["label"] for item in scores] == audio["SETTAG_GENRE"]
    assert (audio.info.sample_rate, audio.info.channels, audio.info.total_samples) == before_stream


def test_real_mp4_round_trip_preserves_standard_tags_and_value_order(tmp_path: Path) -> None:
    path = _copy_fixture("tagged.m4a", tmp_path)
    selected = [
        Prediction("Electronic---Deep House", 0.72),
        Prediction("Electronic---House", 0.51),
    ]
    desired = _desired(selected)
    before = MP4(path)
    assert before.info is not None
    before_stream = (before.info.sample_rate, before.info.channels, before.info.length)

    assert read_genre_state(path) == GenreState(
        standard=("Existing genre",),
        settag=(),
    )
    planned = plan_owned_tags(path, desired)
    applied = apply_metadata_tags(path, desired)
    audio = MP4(path)
    assert audio.tags is not None
    assert audio.info is not None
    genre_key = f"----:{MP4_MEAN}:GENRE"
    scores_key = f"----:{MP4_MEAN}:GENRE_SCORES"
    version_key = f"----:{MP4_MEAN}:VERSION"
    model_key = f"----:{MP4_MEAN}:MODEL"

    assert applied == planned
    assert planned.format == "mp4-freeform"
    assert len(planned.changes) == 7
    assert audio.tags["\xa9nam"] == ["Original title"]
    assert audio.tags["\xa9gen"] == ["Existing genre"]
    genres = [bytes(item).decode() for item in audio.tags[genre_key]]
    assert genres == [item.label for item in selected]
    assert [bytes(item).decode() for item in audio.tags[version_key]] == [__version__]
    assert [bytes(item).decode() for item in audio.tags[model_key]] == ["model/v1"]
    scores = json.loads(bytes(audio.tags[scores_key][0]).decode())
    assert [item["label"] for item in scores] == genres
    assert (audio.info.sample_rate, audio.info.channels, audio.info.length) == before_stream


def test_id3_write_preserves_standard_and_unowned_tags(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _tagged_wav(path)
    desired = _desired()

    before = read_genre_state(path)
    planned = plan_owned_tags(path, desired)
    applied = apply_metadata_tags(path, desired)
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


@pytest.mark.parametrize("fixture", [None, "tagged.flac", "tagged.m4a"])
def test_task_bundle_round_trips_across_supported_containers(
    tmp_path: Path,
    fixture: str | None,
) -> None:
    if fixture is None:
        path = tmp_path / "track.wav"
        _tagged_wav(path)
    else:
        path = _copy_fixture(fixture, tmp_path)
    current = read_owned_values(path)
    evidence: dict[AnalysisTask, list[Prediction]] = {
        "mood-theme": [
            Prediction("dark", 0.82),
            Prediction("deep", 0.61),
        ],
        "instrument": [
            Prediction("synthesizer", 0.91),
            Prediction("drummachine", 0.74),
        ],
    }
    provenance: dict[AnalysisTask, dict[str, object]] = {
        task: {
            "model": {
                "schema": "settag.models/v1",
                "id": f"model/{task}/v1",
                "files": {
                    "embedding": {"name": "effnet.pb", "sha256": "a" * 64},
                    "classifier": {"name": f"{task}.pb", "sha256": "b" * 64},
                },
            },
            "analyzed_at": "2026-07-24T12:00:00Z",
            "config": {
                "evidence": {"limit": 20, "tasks": [task]},
                "selection": {"top": 5, "score_cutoff": 0.1},
                "sha256": "c" * 64,
            },
        }
        for task in evidence
    }

    desired = build_task_owned_values(current, evidence, provenance)
    apply_metadata_tags(path, desired)
    stored = read_owned_values(path)

    assert stored["SETTAG_MOOD_THEME"] == ["dark", "deep"]
    assert stored["SETTAG_INSTRUMENT"] == ["synthesizer", "drummachine"]
    assert stored["SETTAG_GENRE"] is None
    serialized_provenance = stored["SETTAG_PROVENANCE"]
    assert serialized_provenance is not None
    task_provenance = json.loads(serialized_provenance[0])
    assert set(task_provenance["tasks"]) == {"mood-theme", "instrument"}
    assert read_genre_state(path).standard == ("Existing genre",)


def test_explicit_id3_genre_edit_is_planned_written_and_verified(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _tagged_wav(path)
    desired = _desired()
    owned_plan = plan_owned_tags(path, desired)
    standard_plan = plan_standard_genres(path, ("Deep House",))

    applied = apply_metadata_tags(
        path,
        desired,
        standard_genres=("Deep House",),
        expected_plan=owned_plan,
        expected_standard=("Existing genre",),
        expected_standard_change=standard_plan,
    )
    tags = WAVE(path).tags

    assert applied == owned_plan
    assert read_genre_state(path) == GenreState(
        standard=("Deep House",),
        settag=("Electronic---Deep House",),
    )
    assert standard_plan is not None
    assert standard_plan.field == "TCON"
    assert tags is not None
    assert tags["TIT2"].text == ["Original title"]
    assert tags["TXXX:OTHER_TOOL"].text == ["keep me"]
    assert tags.getall("APIC:")[0].data == b"original artwork"


@pytest.mark.parametrize(
    ("fixture", "expected_field"),
    [
        ("tagged.flac", "GENRE"),
        ("tagged.m4a", "\xa9gen"),
    ],
)
def test_explicit_genre_edit_uses_the_native_container_field(
    tmp_path: Path,
    fixture: str,
    expected_field: str,
) -> None:
    path = _copy_fixture(fixture, tmp_path)
    desired = _desired()
    owned_plan = plan_owned_tags(path, desired)
    standard_plan = plan_standard_genres(path, ("Deep House",))

    apply_metadata_tags(
        path,
        desired,
        standard_genres=("Deep House",),
        expected_plan=owned_plan,
        expected_standard=("Existing genre",),
        expected_standard_change=standard_plan,
    )

    assert standard_plan is not None
    assert standard_plan.field == expected_field
    assert read_genre_state(path).standard == ("Deep House",)


def test_empty_result_removes_only_stale_owned_genre(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _tagged_wav(path)
    apply_metadata_tags(path, _desired())

    empty = _desired([], config_sha256="new")
    apply_metadata_tags(path, empty)
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


def test_expected_state_is_checked_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    desired = _desired()
    planned = plan_owned_tags(path, desired)

    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TCON(encoding=3, text=["Changed elsewhere"]))
    audio.save()

    with pytest.raises(TagStateChangedError, match="File genre tag changed"):
        apply_metadata_tags(
            path,
            desired,
            expected_plan=planned,
            expected_standard=(),
        )

    tags = WAVE(path).tags
    assert tags is not None
    assert tags.get("TXXX:SETTAG_GENRE") is None


def test_unrecognized_container_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "track.mp3"
    path.write_text("not audio", encoding="utf-8")

    with pytest.raises(UnsupportedTagFormatError, match="metadata container"):
        plan_owned_tags(path, _desired())


def test_successful_write_leaves_no_temporary_beside_the_original(tmp_path: Path) -> None:
    path = _copy_fixture("tagged.flac", tmp_path)

    apply_metadata_tags(path, _desired())

    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize("fixture", ["tagged.flac", "tagged.m4a"])
def test_failed_write_leaves_the_original_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
) -> None:
    """The whole point of committing through a copy: a failure must not touch the original."""
    path = _copy_fixture(fixture, tmp_path)
    before = path.read_bytes()

    def explode(self: OwnedTagStore, *args: object, **kwargs: object) -> None:
        raise RuntimeError("interrupted mid-write")

    monkeypatch.setattr(OwnedTagStore, "_verify_candidate", explode)

    with pytest.raises(RuntimeError, match="interrupted mid-write"):
        apply_metadata_tags(path, _desired())

    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]
