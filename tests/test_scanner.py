from pathlib import Path

import pytest

from settag.scanner import (
    SUPPORTED_EXTENSIONS,
    WRITE_TEMPORARY_MARKER,
    UnsupportedInputError,
    scan_audio,
)


def test_scan_recurses_and_returns_only_sorted_supported_audio(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    second = nested / "b.MP3"
    second.touch()
    first = tmp_path / "a.flac"
    first.touch()
    third = tmp_path / "c.M4A"
    third.touch()
    (tmp_path / "ignored.aac").touch()
    (tmp_path / "notes.txt").touch()

    assert scan_audio(tmp_path) == sorted([first.resolve(), second.resolve(), third.resolve()])


def test_scan_rejects_an_unsupported_file(tmp_path: Path) -> None:
    path = tmp_path / "track.aac"
    path.touch()

    with pytest.raises(UnsupportedInputError, match="Unsupported audio extension"):
        scan_audio(path)


def test_supported_extensions_cover_first_multi_format_slice() -> None:
    assert {".mp3", ".flac", ".m4a", ".mp4", ".aiff", ".wav"} <= SUPPORTED_EXTENSIONS


def test_scan_skips_an_abandoned_write_candidate(tmp_path: Path) -> None:
    """A hard kill mid-write leaves the temporary copy behind with the audio suffix."""
    track = tmp_path / "track.mp3"
    track.touch()
    (tmp_path / f"track{WRITE_TEMPORARY_MARKER}.mp3").touch()

    assert scan_audio(tmp_path) == [track.resolve()]


def test_scan_skips_macos_appledouble_files(tmp_path: Path) -> None:
    (tmp_path / "track.mp3").write_bytes(b"audio")
    (tmp_path / "._track.mp3").write_bytes(b"finder metadata")

    assert scan_audio(tmp_path) == [(tmp_path / "track.mp3").resolve()]


def test_scan_lists_a_file_reached_through_two_symlinks_once(tmp_path: Path) -> None:
    real = tmp_path / "real.flac"
    real.write_bytes(b"audio")
    (tmp_path / "link-a.flac").symlink_to(real)
    (tmp_path / "link-b.flac").symlink_to(real)

    assert scan_audio(tmp_path) == [real.resolve()]
