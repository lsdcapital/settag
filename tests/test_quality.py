import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mutagen
import pytest
from mutagen.mp3 import MP3, BitrateMode
from mutagen.mp4 import MP4

from settag.quality import header_quality_notes

FIXTURES = Path(__file__).parent / "fixtures"


def _wav(path: Path, *, rate: int = 44_100, width: int = 2) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(b"\0" * width * rate)
    return path


def _header(kind: type, **info: object) -> Any:
    # Encoded MP3s need an encoder the tests do not depend on, so the header
    # values mutagen would report are supplied directly.
    audio: Any = object.__new__(kind)
    audio.info = SimpleNamespace(**info)
    return audio


def _notes(path: Path) -> tuple[str, ...]:
    return header_quality_notes(path, mutagen.File(path))


def test_a_cd_quality_wav_has_no_notes(tmp_path: Path) -> None:
    assert _notes(_wav(tmp_path / "track.wav")) == ()


def test_low_sample_rate_and_bit_depth_are_noted(tmp_path: Path) -> None:
    assert _notes(_wav(tmp_path / "low.wav", rate=22_050)) == ("low sample rate (22,050 Hz)",)
    assert _notes(_wav(tmp_path / "eight.wav", width=1)) == ("8-bit audio",)


def test_lossless_fixtures_are_not_judged_by_bitrate() -> None:
    assert _notes(FIXTURES / "tagged.flac") == ()


@pytest.mark.parametrize(
    ("kbps", "mode", "expected"),
    [
        (320, BitrateMode.CBR, ()),
        (256, BitrateMode.CBR, ()),
        (192, BitrateMode.CBR, ("low bitrate (192 kbps)",)),
        # No Xing header: mutagen cannot tell, and the file is judged as CBR.
        (224, BitrateMode.UNKNOWN, ("low bitrate (224 kbps)",)),
        (190, BitrateMode.VBR, ()),
        (128, BitrateMode.VBR, ("low bitrate (128 kbps average)",)),
    ],
)
def test_mp3_bitrate_floor_depends_on_the_mode(kbps, mode, expected) -> None:
    audio = _header(MP3, bitrate=kbps * 1000, bitrate_mode=mode, sample_rate=44_100)
    assert header_quality_notes(Path("track.mp3"), audio) == expected


def test_aac_is_judged_by_its_average_and_alac_not_at_all() -> None:
    aac = _header(MP4, bitrate=228_000, codec="mp4a.40.2", sample_rate=44_100)
    low = _header(MP4, bitrate=96_000, codec="mp4a.40.2", sample_rate=44_100)
    alac = _header(MP4, bitrate=900_000, codec="alac", sample_rate=44_100, bits_per_sample=16)
    assert header_quality_notes(Path("track.m4a"), aac) == ()
    assert header_quality_notes(Path("track.m4a"), low) == ("low bitrate (96 kbps average)",)
    assert header_quality_notes(Path("track.m4a"), alac) == ()


def test_contents_that_do_not_match_the_extension_are_noted_alone(tmp_path: Path) -> None:
    # The header belongs to another format, so its rates are not reported as well.
    path = _wav(tmp_path / "track.aiff", rate=22_050)
    assert _notes(path) == ("contents read as WAVE, not AIFF",)
