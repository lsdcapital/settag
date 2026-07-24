from pathlib import Path

import pytest

from settag.config import ConfigError, SetTagConfig, load_config


def test_missing_config_uses_genre_default(tmp_path: Path) -> None:
    assert load_config(tmp_path / "missing.toml") == SetTagConfig(tasks=("genre",))


def test_config_loads_analysis_tasks_in_canonical_order(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[analysis]\ntasks = ["instrument", "genre", "mood-theme"]\n',
        encoding="utf-8",
    )

    assert load_config(path).tasks == ("genre", "mood-theme", "instrument")


@pytest.mark.parametrize(
    "contents",
    [
        "[analysis]\ntasks = []\n",
        '[analysis]\ntasks = "genre"\n',
        '[analysis]\ntasks = ["genre", "tempo"]\n',
        "analysis = 1\n",
        "[analysis\n",
    ],
)
def test_invalid_config_is_rejected_with_its_path(
    tmp_path: Path,
    contents: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigError, match=str(path)):
        load_config(path)
