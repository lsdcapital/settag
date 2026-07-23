from pathlib import Path

import pytest

from settag.scanner import SUPPORTED_EXTENSIONS, UnsupportedInputError, scan_audio


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
