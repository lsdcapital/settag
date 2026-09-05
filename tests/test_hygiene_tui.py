import asyncio
import wave
from pathlib import Path

import pytest
from mutagen.id3 import COMM, ID3
from mutagen.wave import WAVE
from textual.widgets import Button, Static, Tree

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
            tree = app.query_one("#hygiene-tree", Tree)
            track = tree.root.children[0]
            assert str(track.label) == "[x] track.wav · 1/1 checked"
            assert str(track.children[0].label) == (
                "[x] Comment (download): electronicfresh.com → Remove tag · contains a web address"
            )
            await pilot.press("down")
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
            assert "After cleanup: Remove this tag" in inspector

    asyncio.run(exercise())


@pytest.mark.parametrize("size", [(120, 36), (80, 24)])
def test_hygiene_tree_preserves_selection_and_position_when_collapsed(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _two_finding_wav(first)
    _hygiene_wav(second)
    app = HygieneApp(
        source=tmp_path,
        paths=(first, second),
        batch=inspect_hygiene_paths((first, second)),
        journal=WriteJournal(tmp_path / "journal.sqlite3"),
    )

    async def exercise() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            tree = app.query_one(Tree)
            track = tree.root.children[0]
            other = tree.root.children[1]
            assert len(track.children) == 2
            assert len(other.children) == 1
            await pilot.press("right", "space")
            assert app.selected == {1, 2}
            assert str(track.label).startswith("[-]")
            assert "1/2 checked" in str(track.label)
            assert "Excluded from cleanup" in str(app.query_one("#inspector", Static).render())

            # Collapsing a branch does not exclude its hidden, selected child.
            await pilot.press("left", "enter")
            assert not track.is_expanded
            assert tree.cursor_node is track
            plans = app._selected_plans()
            assert [(plan.path, len(plan.findings)) for plan in plans] == [(first, 1), (second, 1)]

            # A partially selected parent includes its remaining fixes, then clears them.
            await pilot.press("space")
            assert app.selected == {0, 1, 2}
            await pilot.press("space")
            assert app.selected == {2}
            assert not track.is_expanded

            await pilot.press("down")
            assert tree.cursor_node is other
            await pilot.press("a")
            assert app.selected == {0, 1, 2}
            assert tree.cursor_node is other
            assert not track.is_expanded
            await pilot.press("a")
            assert app.selected == set()
            assert tree.cursor_node is other

            # Left/right and Enter navigate and disclose without altering selection.
            await pilot.press("up", "right", "right")
            assert track.is_expanded
            assert tree.cursor_node is track.children[0]
            await pilot.press("enter")
            assert app.has_class("details-open")
            assert "Excluded from cleanup" in str(app.query_one("#inspector", Static).render())
            assert app.selected == set()

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
            assert "No cleanup needed" in str(app.query_one(Tree).root.children[0].label)
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
        scan="metadata",
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
            assert app.sub_title == "Metadata cleanup"
            assert len(app.query_one(Tree).root.children[0].children) == 1

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
            tree = app.query_one(Tree)
            assert str(tree.root.children[0].label) == "! broken.wav · Inspection error"
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
            await pilot.press("down", "space")
            assert app.selected == {1}
            await pilot.press("left", "enter")
            assert not app.query_one(Tree).root.children[0].is_expanded
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
            assert len(app.query_one(Tree).root.children[0].children) == 1
            assert app.selected == set()
            assert str(app.query_one(Tree).root.children[0].children[0].label).startswith("[ ]")

    asyncio.run(exercise())


@pytest.mark.parametrize("size", [(120, 36), (80, 24)])
def test_duplicate_groups_are_visible_but_never_selected(tmp_path: Path, size) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _hygiene_wav(first)
    _hygiene_wav(second)
    app = HygieneApp(
        source=tmp_path,
        paths=(first, second),
        batch=inspect_hygiene_paths((first, second), scan="all"),
        journal=WriteJournal(tmp_path / "journal.sqlite3"),
    )

    async def exercise() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            tree = app.query_one(Tree)
            node = tree.root.children[-1]
            assert "Duplicate audio" in str(node.label)
            tree.move_cursor(node)
            await pilot.pause()
            inspector = str(app.query_one("#inspector", Static).render())
            assert str(first) in inspector
            assert str(second) in inspector
            assert "does not delete" in inspector
            await pilot.press("space")
            assert node.data not in app.selected
            await pilot.press("a", "a")
            assert node.data not in app.selected
            assert len(app._selected_plans()) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("size", [(120, 36), (80, 24)])
def test_hygiene_tools_wait_for_choice_and_run_only_selected_scan(
    tmp_path: Path, monkeypatch, size
) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _hygiene_wav(first)
    _hygiene_wav(second)
    calls = []

    def inspect(paths, **kwargs):
        calls.append(kwargs["scan"])
        return inspect_hygiene_paths(paths, **kwargs)

    monkeypatch.setattr("settag.tui.hygiene.inspect_hygiene_paths", inspect)
    app = HygieneApp(
        source=tmp_path,
        paths=(first, second),
        journal=WriteJournal(tmp_path / "journal.sqlite3"),
    )

    async def exercise() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert calls == []
            assert app.choosing_tool
            app.query_one("#scan-duplicates", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert calls == ["duplicates"]
            assert app.batch is not None
            assert len(app.batch.duplicate_groups) == 1
            assert app.batch.finding_count == 0
            assert app.selected == set()
            assert not app.query_one("#hygiene-tools").display
            await pilot.press("t")
            assert app.choosing_tool
            assert calls == ["duplicates"]
            app.query_one("#scan-metadata", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert calls == ["duplicates", "metadata"]
            assert app.batch.scan == "metadata"
            assert app.batch.finding_count == 2
            assert app.batch.duplicate_groups == ()
            await pilot.press("t")
            app.query_one("#scan-all", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert calls == ["duplicates", "metadata", "all"]
            assert app.batch.finding_count == 2
            assert len(app.batch.duplicate_groups) == 1

    asyncio.run(exercise())
