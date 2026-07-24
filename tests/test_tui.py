import asyncio
import wave
from pathlib import Path
from types import SimpleNamespace

from mutagen.id3 import ID3
from mutagen.wave import WAVE
from textual.widgets import DataTable, Static

from settag.policy import Prediction
from settag.tui import ConfirmWriteScreen, GenreEditScreen, SetTagApp
from settag.workflow import AnalysisBatch, planned_write_for_track, prepare_track


class FakeAnalyzer:
    spec = SimpleNamespace(id="model/v1")
    model_manifest = {"id": "model/v1", "files": {}}
    backend_version = "test"

    def analyze(self, path: Path) -> list[Prediction]:
        return [
            Prediction("Electronic---Progressive House", 0.664),
            Prediction("Electronic---Techno", 0.269),
        ]


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 80)


def _batch(path: Path) -> AnalysisBatch:
    track = prepare_track(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
    )
    return AnalysisBatch(
        planned=(planned_write_for_track(track),),
        failures=(),
    )


def test_tui_stages_selection_and_direct_genre_suggestion(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    app = SetTagApp(source=path, initial_batch=_batch(path))

    async def exercise() -> None:
        async with app.run_test(size=(90, 42)) as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", DataTable)
            inspector = app.query_one("#inspector", Static)

            assert app.has_class("narrow")
            assert table.row_count == 1
            assert app.selected == {0}
            assert "Electronic---Progressive House" in str(inspector.render())

            await pilot.press("n")
            assert app.selected == set()
            await pilot.press("a")
            assert app.selected == {0}

            await pilot.press("g")
            assert app.items[0].target_file_genre == ("Progressive House",)
            assert app.items[0].standard_genre_change is not None
            assert app.selected == {0}

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, GenreEditScreen)
            await pilot.press("escape")

    asyncio.run(exercise())
    assert WAVE(path).tags is None


def test_tui_requires_confirmation_then_writes_and_verifies(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    app = SetTagApp(source=path, initial_batch=_batch(path))

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("g")
            await pilot.press("w")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmWriteScreen)
            await pilot.press("y")
            await pilot.pause(0.3)

    asyncio.run(exercise())
    tags = WAVE(path).tags
    assert isinstance(tags, ID3)
    assert tags["TCON"].text == ["Progressive House"]
    assert tags["TXXX:SETTAG_GENRE"].text == [
        "Electronic---Progressive House",
        "Electronic---Techno",
    ]
