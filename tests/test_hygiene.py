import shutil
import wave
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import APIC, COMM, ID3, TCON, TIT2, TXXX
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

from settag import hygiene
from settag.cli.render import _print_hygiene_batch
from settag.hygiene import (
    _WEB_ADDRESS,
    apply_hygiene,
    hygiene_finding,
    inspect_hygiene_path,
    inspect_hygiene_paths,
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


@pytest.mark.parametrize(
    "value",
    ["cue to.me at 2:30", "fade to.co", "send it cc.me", "drop at 1.to"],
)
def test_short_top_level_domains_are_not_flagged_without_a_scheme(value: str) -> None:
    """A DJ note that happens to contain word.short-tld is not a web address."""
    assert _WEB_ADDRESS.search(value) is None


@pytest.mark.parametrize(
    "value",
    ["https://promo.to/x", "www.label.me", "electronicfresh.com", "ripped-by.download"],
)
def test_web_addresses_are_still_flagged(value: str) -> None:
    assert _WEB_ADDRESS.search(value) is not None


def test_duplicate_audio_ignores_tags_but_distinguishes_samples(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    distinct = tmp_path / "distinct.wav"
    _silent_wav(first)
    _tagged_wav(second)
    with wave.open(str(distinct), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x01\x00" * 8000)
    before = {path: path.read_bytes() for path in (first, second, distinct)}
    progress = []
    batch = inspect_hygiene_paths(
        (first, second, distinct, first),
        scan="all",
        on_progress=lambda done, total, path: progress.append(done),
    )
    assert batch.track_count == 3
    assert batch.failure_count == 0
    assert len(batch.duplicate_groups) == 1
    assert batch.duplicate_groups[0].paths == (first, second)
    assert progress == [1, 2, 3, 4]
    assert {path: path.read_bytes() for path in before} == before


def test_duplicate_hash_failure_does_not_mark_file_clean(tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _silent_wav(first)
    _silent_wav(second)
    original = hygiene.sha256_audio

    def fail_one(path):
        if path == first:
            raise OSError("unreadable audio")
        return original(path)

    monkeypatch.setattr(hygiene, "sha256_audio", fail_one)
    batch = inspect_hygiene_paths((first, second), scan="duplicates")
    assert batch.track_count == 1
    assert batch.failure_count == 1
    assert batch.failures[0].path == first
    assert batch.duplicate_groups == ()


def test_plain_hygiene_reports_duplicate_paths(tmp_path: Path, capsys) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _silent_wav(first)
    _silent_wav(second)
    _print_hygiene_batch(tmp_path, inspect_hygiene_paths((first, second), scan="duplicates"))
    output = capsys.readouterr().err
    assert "Duplicate groups:     1" in output
    assert "Duplicate audio · 2 files (review only)" in output
    assert str(first) in output
    assert str(second) in output


def test_metadata_scan_never_hashes_audio(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "track.wav"
    _tagged_wav(path)

    def unexpected_hash(path):
        pytest.fail("metadata scan must not hash audio")

    monkeypatch.setattr(hygiene, "sha256_audio", unexpected_hash)
    batch = inspect_hygiene_paths((path,))
    assert batch.scan == "metadata"
    assert batch.finding_count == 1
    assert batch.duplicate_groups == ()


def test_duplicate_scan_never_inspects_metadata(tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _tagged_wav(first)
    _silent_wav(second)

    def unexpected_inspection(path):
        pytest.fail("duplicate scan must not inspect metadata")

    monkeypatch.setattr(hygiene, "inspect_hygiene_path", unexpected_inspection)
    batch = inspect_hygiene_paths((first, second), scan="duplicates")
    assert batch.scan == "duplicates"
    assert batch.finding_count == 0
    assert len(batch.duplicate_groups) == 1
