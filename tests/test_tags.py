from pathlib import Path

from mutagen.id3 import ID3, TCON, TIT2

from settag.policy import Prediction
from settag.tags import apply_owned_tags, build_owned_values, plan_owned_tags


def _tagged_mp3(path: Path) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Original title"]))
    tags.add(TCON(encoding=3, text=["Existing genre"]))
    tags.save(path)


def test_write_preserves_standard_and_unowned_tags(tmp_path: Path) -> None:
    path = tmp_path / "track.mp3"
    _tagged_mp3(path)
    desired = build_owned_values(
        [Prediction("Electronic---Deep House", 0.72)],
        model_id="model/v1",
        analyzed_at="2026-07-23T12:00:00Z",
        config_sha256="abc123",
    )

    planned = plan_owned_tags(path, desired)
    applied = apply_owned_tags(path, desired)
    tags = ID3(path)

    assert applied == planned
    assert tags["TIT2"].text == ["Original title"]
    assert tags["TCON"].text == ["Existing genre"]
    assert tags["TXXX:SETTAG_GENRE"].text == ["Electronic---Deep House"]
    assert tags["TXXX:SETTAG_MODEL"].text == ["model/v1"]


def test_empty_result_removes_only_stale_owned_genre(tmp_path: Path) -> None:
    path = tmp_path / "track.mp3"
    _tagged_mp3(path)
    initial = build_owned_values(
        [Prediction("Electronic---House", 0.6)],
        model_id="model/v1",
        analyzed_at="2026-07-23T12:00:00Z",
        config_sha256="old",
    )
    apply_owned_tags(path, initial)

    empty = build_owned_values(
        [],
        model_id="model/v1",
        analyzed_at="2026-07-23T13:00:00Z",
        config_sha256="new",
    )
    apply_owned_tags(path, empty)
    tags = ID3(path)

    assert tags.get("TXXX:SETTAG_GENRE") is None
    assert tags.get("TXXX:SETTAG_GENRE_SCORES") is None
    assert tags["TCON"].text == ["Existing genre"]
    assert tags["TXXX:SETTAG_CONFIG_SHA256"].text == ["new"]
