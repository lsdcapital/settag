from pathlib import Path

import pytest

from settag.scanner import UnsupportedInputError, scan_mp3


def test_scan_recurses_and_returns_only_sorted_mp3_files(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    second = nested / "b.MP3"
    second.touch()
    first = tmp_path / "a.mp3"
    first.touch()
    (tmp_path / "ignored.flac").touch()

    assert scan_mp3(tmp_path) == sorted([first.resolve(), second.resolve()])


def test_scan_rejects_a_non_mp3_file(tmp_path: Path) -> None:
    path = tmp_path / "track.flac"
    path.touch()

    with pytest.raises(UnsupportedInputError, match="Only MP3"):
        scan_mp3(path)
