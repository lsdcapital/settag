from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release import ReleaseError, bump_version, parse_version


@pytest.mark.parametrize(
    ("current", "bump", "expected"),
    [
        ("0.1.1", "patch", "0.1.2"),
        ("0.1.9", "minor", "0.2.0"),
        ("0.9.9", "major", "1.0.0"),
    ],
)
def test_bump_version(current: str, bump: str, expected: str) -> None:
    assert bump_version(current, bump) == expected


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3rc1", "01.2.3"])
def test_parse_version_rejects_non_release_versions(version: str) -> None:
    with pytest.raises(ReleaseError, match="Expected a version"):
        parse_version(version)


def test_bump_version_rejects_unknown_bump() -> None:
    with pytest.raises(ReleaseError, match="Unknown bump"):
        bump_version("1.2.3", "banana")
