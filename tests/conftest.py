from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_settag_state(tmp_path_factory, monkeypatch) -> Path:
    """Keep every test off the real user state.

    The write journal and workbench live outside the repository, so without
    this a test that applies a plan would write into the developer's own music
    library state. The config file is isolated too: `run` reads it whenever
    --tasks or --genre-sample is unset, so a personal config would otherwise
    change what those tests observe.
    """
    base = tmp_path_factory.mktemp("settag-state")
    monkeypatch.setenv("SETTAG_JOURNAL_DB", str(base / "journal.sqlite3"))
    monkeypatch.setenv("SETTAG_STATE_DB", str(base / "state.sqlite3"))
    monkeypatch.setenv("SETTAG_CONFIG", str(base / "config.toml"))
    monkeypatch.setenv("SETTAG_BEATPORT_CACHE", str(base / "beatport-cache"))
    return base
