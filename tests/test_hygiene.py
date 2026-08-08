import shutil
import wave
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import APIC, COMM, ID3, TCON, TIT2, TXXX
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

from settag.hygiene import (
    apply_hygiene,
    hygiene_finding,
    inspect_hygiene_path,
    plan_hygiene_track,
    preflight_hygiene,
)
from settag.journal import BatchRecorder, WriteJournal
from settag.tags import HygieneTag, read_hygiene_tags
from settag.workflow import apply_undo, preflight_undo

FIXTURES = Path(__file__).parent / "fixtures"


def _silent_wav(path: Path) -> None:
    rate = 8_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * rate)


def _tagged_wav(path: Path) -> None:
    _silent_wav(path)
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TIT2(encoding=3, text=["Original title"]))
    audio.tags.add(TCON(encoding=3, text=["Existing genre"]))
    audio.tags.add(TXXX(encoding=3, desc="OTHER_TOOL", text=["keep me"]))
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, data=b"artwork"))
    audio.tags.add(COMM(encoding=3, lang="eng", desc="download", text=["electronicfresh.com"]))
    audio.tags.add(
        COMM(
            encoding=3,
            lang="eng",
            desc="note",
            text=["Long intro; first clean downbeat at 01:12"],
        )
    )
    audio.save()


def _copy_fixture(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copyfile(FIXTURES / name, target)
    return target


def test_hygiene_flags_a_domain_comment_but_not_a_dj_note(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _tagged_wav(path)

    track = inspect_hygiene_path(path)

    assert [(item.label, item.before, item.after, item.reasons) for item in track.findings] == [
        (
            "Comment (download)",
            ("electronicfresh.com",),
            (),
            ("contains a web address",),
        )
    ]


def test_hygiene_removes_empty_and_duplicate_values_but_keeps_the_first() -> None:
    finding = hygiene_finding(
        HygieneTag(
            field="VORBIS:comment",
            label="comment",
            category="comment",
            values=("Useful note", "", " useful note "),
        )
    )

    assert finding is not None
    assert finding.after == ("Useful note",)
    assert finding.reasons == ("empty value", "duplicate value")


def test_an_empty_present_tag_has_a_reversible_before_state() -> None:
    finding = hygiene_finding(
        HygieneTag(
            field="VORBIS:comment",
            label="comment",
            category="comment",
            values=(),
        )
    )

    assert finding is not None
    assert finding.current_text == "(empty tag)"
    assert finding.change.before == []
    assert finding.change.after is None


@pytest.mark.parametrize(
    ("fixture", "expected_label", "expected_value"),
    [
        ("tagged.flac", "encoder", "Lavf62.12.102"),
        ("tagged.m4a", "Encoder", "Lavf62.12.102"),
    ],
)
def test_hygiene_flags_generated_encoder_markers_in_real_containers(
    tmp_path: Path,
    fixture: str,
    expected_label: str,
    expected_value: str,
) -> None:
    path = _copy_fixture(fixture, tmp_path)

    track = inspect_hygiene_path(path)

    assert len(track.findings) == 1
    finding = track.findings[0]
    assert finding.label == expected_label
    assert finding.before == (expected_value,)
    assert finding.after == ()
    assert finding.reasons == ("generated encoder marker",)


def test_hygiene_write_preserves_unselected_metadata_and_is_undoable(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _tagged_wav(path)
    track = inspect_hygiene_path(path)
    plan = plan_hygiene_track(track)
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    recorder = BatchRecorder(journal)

    written = apply_hygiene(preflight_hygiene((plan,)), on_write=recorder)

    assert written == 1
    tags = WAVE(path).tags
    assert tags is not None
    assert tags.get("COMM:download:eng") is None
    assert tags["COMM:note:eng"].text == ["Long intro; first clean downbeat at 01:12"]
    assert tags["TIT2"].text == ["Original title"]
    assert tags["TCON"].text == ["Existing genre"]
    assert tags["TXXX:OTHER_TOOL"].text == ["keep me"]
    assert tags.getall("APIC:")[0].data == b"artwork"

    batch = journal.latest()
    assert batch is not None
    assert batch.hygiene_count == 1
    assert "tag cleanup" in batch.summary
    assert batch.entries[0].readable_changes[-1] == (
        "Tag hygiene Comment (download): electronicfresh.com → not set"
    )
    assert apply_undo(preflight_undo(batch.entries).restorable) == 1

    restored = WAVE(path).tags
    assert restored is not None
    assert restored["COMM:download:eng"].text == ["electronicfresh.com"]
    assert restored["COMM:note:eng"].text == ["Long intro; first clean downbeat at 01:12"]


@pytest.mark.parametrize("fixture", ["tagged.flac", "tagged.m4a"])
def test_real_container_hygiene_write_and_undo(
    tmp_path: Path,
    fixture: str,
) -> None:
    path = _copy_fixture(fixture, tmp_path)
    before = inspect_hygiene_path(path)
    journal = WriteJournal(tmp_path / f"{fixture}.sqlite3")
    recorder = BatchRecorder(journal)

    apply_hygiene(
        preflight_hygiene((plan_hygiene_track(before),)),
        on_write=recorder,
    )

    assert inspect_hygiene_path(path).findings == ()
    batch = journal.latest()
    assert batch is not None
    apply_undo(preflight_undo(batch.entries).restorable)
    restored = read_hygiene_tags(path)
    assert any(tag.category == "encoder" for tag in restored)

    if fixture.endswith(".flac"):
        assert FLAC(path)["encoder"] == ["Lavf62.12.102"]
    else:
        assert MP4(path)["\xa9too"] == ["Lavf62.12.102"]


def test_hygiene_preflight_rejects_a_changed_comment(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _tagged_wav(path)
    plan = plan_hygiene_track(inspect_hygiene_path(path))
    audio = WAVE(path)
    assert audio.tags is not None
    audio.tags.delall("COMM:download:eng")
    audio.tags.add(COMM(encoding=3, lang="eng", desc="download", text=["changed.example.com"]))
    audio.save()

    with pytest.raises(RuntimeError, match="file changed since hygiene review"):
        preflight_hygiene((plan,))
