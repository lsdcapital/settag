from pathlib import Path

from settag.model_store import default_model_dir


def test_default_model_dir_respects_xdg_cache_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    assert default_model_dir() == tmp_path / "settag" / "models"
