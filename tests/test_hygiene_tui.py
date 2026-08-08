import asyncio
import wave
from pathlib import Path

from mutagen.id3 import COMM, ID3
from mutagen.wave import WAVE
from textual.widgets import Button, DataTable, Static

from settag.hygiene import inspect_hygiene_paths
from settag.journal import WriteJournal
from settag.tui import ConfirmWriteScreen, HygieneApp


def _hygiene_wav(path: Path) -> None:
    rate = 8_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * rate)
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(COMM(encoding=3, lang="eng", desc="download", text=["electronicfresh.com"]))
    audio.save()


def _two_finding_wav(path: Path) -> None:
    _hygiene_wav(path)
    audio = WAVE(path)
    assert isinstance(audio.tags, ID3)
    audio.tags.add(COMM(encoding=3, lang="eng", desc="source", text=["another-downloads.com"]))
    audio.save()


def test_hygiene_app_reviews_field_level_findings_and_toggles_details(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _hygiene_wav(path)
    batch = inspect_hygiene_paths((path,))
    app = HygieneApp(
        source=path,
        paths=(path,),
        batch=batch,
        journal=WriteJournal(tmp_path / "journal.sqlite3"),
    )

    async def exercise() -> None:
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            table = app.query_one("#hygiene-table", DataTable)
            assert table.row_count == 1
            assert table.get_row_at(0) == [
                "✓",
                "track.wav",
                "Comment (download)",
                "electronicfresh.com",
                "contains a web address",
            ]
            assert app.selected == {0}
            await pilot.press("space")
            assert app.selected == set()
            await pilot.press("space")
            assert app.selected == {0}
            await pilot.press("i")
            assert app.has_class("details-open")
            inspector = str(app.query_one("#inspector", Static).render())
            assert "Current value" in inspector
            assert "electronicfresh.com" in inspector
            assert "After cleanup\nRemove this tag" in inspector

    asyncio.run(exercise())


def test_hygiene_app_confirms_writes_verifies_and_journals(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _hygiene_wav(path)
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    app = HygieneApp(
        source=path,
        paths=(path,),
        batch=inspect_hygiene_paths((path,)),
        journal=journal,
    )

    async def exercise() -> None:
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            await pilot.press("w")
            for _ in range(30):
                await pilot.pause(0.05)
                if isinstance(app.screen, ConfirmWriteScreen):
                    break
            assert isinstance(app.screen, ConfirmWriteScreen)
            summary = str(app.screen.query_one("#confirm-summary", Static).render())
            assert "track.wav" in summary
            assert "Comment (download): Remove tag — contains a web address" in summary
            assert "Batch total: 1 tag cleanup" in summary
            assert app.screen.query_one("#confirm", Button).has_focus

            await pilot.press("enter")
            for _ in range(40):
                await pilot.pause(0.05)
                if not app.busy:
                    break
            assert app.busy is False
            assert app.query_one("#hygiene-table", DataTable).row_count == 0
            status = str(app.query_one("#status", Static).render())
            assert "Cleaned and verified 1 file." in status
            assert "Undo with: settag undo" in status

    asyncio.run(exercise())

    tags = WAVE(path).tags
    assert tags is not None
    assert tags.get("COMM:download:eng") is None
    batch = journal.latest()
    assert batch is not None
    assert batch.hygiene_count == 1


def test_hygiene_app_scans_inside_the_interactive_loading_state(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _hygiene_wav(path)
    app = HygieneApp(
        source=path,
        paths=(path,),
        batch=None,
        journal=WriteJournal(tmp_path / "journal.sqlite3"),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(30):
                await pilot.pause(0.05)
                if app.batch is not None and not app.busy:
                    break
            assert app.batch is not None
            assert app.batch.finding_count == 1
            assert app.sub_title == "Review suspicious metadata"
            assert app.query_one("#hygiene-table", DataTable).row_count == 1

    asyncio.run(exercise())


def test_hygiene_app_shows_scan_failures_as_nonselectable_rows(tmp_path: Path) -> None:
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not an audio container")
    batch = inspect_hygiene_paths((path,))
    app = HygieneApp(
        source=path,
        paths=(path,),
        batch=batch,
        journal=WriteJournal(tmp_path / "journal.sqlite3"),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.query_one("#hygiene-table", DataTable)
            assert table.get_row_at(0)[0:4] == [
                "!",
                "broken.wav",
                "Inspection error",
                "Could not inspect",
            ]
            assert app.selected == set()
            inspector = str(app.query_one("#inspector", Static).render())
            assert "Inspection error" in inspector
            assert "not classified as clean" in inspector
            await pilot.press("space")
            assert app.selected == set()

    asyncio.run(exercise())


def test_hygiene_app_does_not_reselect_an_opted_out_finding_after_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _two_finding_wav(path)
    app = HygieneApp(
        source=path,
        paths=(path,),
        batch=inspect_hygiene_paths((path,)),
        journal=WriteJournal(tmp_path / "journal.sqlite3"),
    )

    async def exercise() -> None:
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            assert app.selected == {0, 1}
            await pilot.press("space")
            assert app.selected == {1}
            await pilot.press("w")
            for _ in range(30):
                await pilot.pause(0.05)
                if isinstance(app.screen, ConfirmWriteScreen):
                    break
            assert isinstance(app.screen, ConfirmWriteScreen)
            await pilot.press("enter")
            for _ in range(40):
                await pilot.pause(0.05)
                if not app.busy:
                    break
            assert app.query_one("#hygiene-table", DataTable).row_count == 1
            assert app.selected == set()
            assert app.query_one("#hygiene-table", DataTable).get_row_at(0)[0] == ""

    asyncio.run(exercise())
