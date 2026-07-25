import asyncio
import wave
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from mutagen.id3 import ID3, TCON
from mutagen.wave import WAVE
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, ProgressBar, Static

from settag.journal import WriteJournal
from settag.policy import Prediction
from settag.tags import OWNED_DESCRIPTIONS, GenreState, task_evidence_from_owned
from settag.tasks import AnalysisTask
from settag.tui import (
    ConfirmUndoScreen,
    ConfirmWriteScreen,
    GenreEditScreen,
    SetTagApp,
    UndoScreen,
)
from settag.workflow import (
    AnalysisBatch,
    MetadataBatch,
    MetadataStatus,
    MetadataTrack,
    analyze_paths,
    planned_write_for_track,
    prepare_track,
)


class FakeAnalyzer:
    spec = SimpleNamespace(id="model/v1")
    model_manifest = {"id": "model/v1", "files": {}}
    backend_version = "test"

    def analyze(self, path: Path) -> list[Prediction]:
        return [
            Prediction("Electronic---Progressive House", 0.664),
            Prediction("Electronic---Techno", 0.269),
        ]


class FakeTaskAnalyzer:
    backend_version = "test"
    model_manifests: dict[AnalysisTask, dict[str, object]] = {
        task: {
            "schema": "settag.models/v1",
            "id": f"model/{task}/v1",
            "files": {},
        }
        for task in ("genre", "mood-theme", "instrument")
    }

    def analyze_tasks(
        self,
        path: Path,
    ) -> dict[AnalysisTask, list[Prediction]]:
        return {
            "genre": [
                Prediction("Electronic---Progressive House", 0.664),
                Prediction("Electronic---Techno", 0.269),
            ],
            "mood-theme": [
                Prediction("energetic", 0.83),
                Prediction("party", 0.72),
            ],
            "instrument": [
                Prediction("synthesizer", 0.81),
                Prediction("drummachine", 0.62),
            ],
        }


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 80)


def _analysis_batch(
    paths: Sequence[Path],
    *,
    top: int = 5,
    threshold: float = 0.10,
) -> AnalysisBatch:
    planned = []
    for path in paths:
        track = prepare_track(
            path,
            analyzer=FakeAnalyzer(),
            top=top,
            threshold=threshold,
        )
        planned.append(planned_write_for_track(track))
    return AnalysisBatch(planned=tuple(planned), failures=())


def _task_analysis_batch(paths: Sequence[Path]) -> AnalysisBatch:
    planned = []
    analyzer = FakeTaskAnalyzer()
    for path in paths:
        track = prepare_track(
            path,
            analyzer=analyzer,
            top=5,
            threshold=0.10,
        )
        planned.append(planned_write_for_track(track))
    return AnalysisBatch(planned=tuple(planned), failures=())


def _metadata_track(
    path: Path,
    *,
    status: MetadataStatus = "not_analyzed",
    standard_genre: tuple[str, ...] = (),
) -> MetadataTrack:
    predictions = (Prediction("Electronic---House", 0.72),) if status == "current" else ()
    return MetadataTrack(
        path=path,
        genre_state=GenreState(
            standard=standard_genre,
            settag=tuple(prediction.label for prediction in predictions),
        ),
        owned=dict.fromkeys(OWNED_DESCRIPTIONS),
        stored_predictions=predictions,
        status=status,
        analyzed_at="2026-07-23T12:00:00Z" if status == "current" else None,
    )


def test_tui_reads_metadata_before_loading_model_and_analyzes_only_selection(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "a-fresh.wav"
    stale = tmp_path / "b-stale.wav"
    current = tmp_path / "c-current.wav"
    for path in (fresh, stale, current):
        _silent_wav(path)

    metadata_calls = 0
    analysis_calls: list[tuple[Path, ...]] = []

    def load_metadata(_on_progress) -> MetadataBatch:
        nonlocal metadata_calls
        metadata_calls += 1
        return MetadataBatch(
            tracks=(
                _metadata_track(current, status="current", standard_genre=("House",)),
                _metadata_track(
                    stale,
                    status="stale",
                    standard_genre=("House",),
                ),
                _metadata_track(fresh),
            ),
            failures=(),
        )

    def load_analysis(paths, _on_progress, _should_cancel) -> AnalysisBatch:
        analysis_calls.append(tuple(paths))
        return _analysis_batch(paths)

    app = SetTagApp(
        source=tmp_path,
        metadata_loader=load_metadata,
        analysis_loader=load_analysis,
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            for _ in range(20):
                await pilot.pause(0.05)
                if app.entries:
                    break

            table = app.query_one("#tracks", DataTable)
            inspector = app.query_one("#inspector", Static)
            inspector_pane = app.query_one("#inspector-pane")
            assert metadata_calls == 1
            assert analysis_calls == []
            assert table.row_count == 3
            assert app.phase == "choose"
            assert app.analysis_selected == {0, 1}
            assert table.get_row_at(0)[0] == "✓"
            assert table.get_row_at(0)[3] == "Never"
            assert table.get_row_at(2)[3] == "Up to date · 2026-07-23"
            assert inspector_pane.display is False
            assert "The audio model has not been loaded." in str(inspector.render())
            choose_actions = {
                active.binding.action for active in app.screen.active_bindings.values()
            }
            assert "analyze" in choose_actions
            assert "toggle_details" in choose_actions
            assert "review" not in choose_actions
            assert "write" not in choose_actions

            await pilot.press("i")
            assert app.has_class("details-open")
            assert inspector_pane.display is True
            await pilot.press("i")
            assert not app.has_class("details-open")
            assert inspector_pane.display is False

            await pilot.press("f")
            assert app.library_filter == "needs_analysis"
            assert table.row_count == 2

            await pilot.press("f")
            assert app.library_filter == "missing_genre"
            assert table.row_count == 1
            assert app.analysis_selected == {0, 1}
            assert table.get_row_at(0)[0] == "✓"
            await pilot.press("enter")
            for _ in range(30):
                await pilot.pause(0.05)
                if app.phase == "review":
                    break

            assert analysis_calls == [(fresh,)]
            assert app.phase == "review"
            assert app.review_indices == {0}
            assert app.write_selected == {0}
            assert table.get_row_at(0)[3].startswith("New · ")
            assert table.get_row_at(0)[0] == "✓"
            review_actions = {
                active.binding.action for active in app.screen.active_bindings.values()
            }
            assert "write" in review_actions
            assert "analyze" not in review_actions

    asyncio.run(exercise())
    assert WAVE(fresh).tags is None
    assert WAVE(stale).tags is None
    assert WAVE(current).tags is None


def test_tui_analyzes_and_reviews_all_configured_tasks(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path),),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _task_analysis_batch(paths),
        analysis_tasks=("genre", "mood-theme", "instrument"),
    )

    async def exercise() -> None:
        async with app.run_test(size=(150, 48)) as pilot:
            await pilot.pause()
            context = app.query_one("#context", Static)
            assert "Tasks: Genre, Mood/theme, Instrument" in str(context.render())

            await pilot.press("r")
            for _ in range(30):
                await pilot.pause(0.05)
                if app.phase == "review":
                    break

            assert app.phase == "review"
            plan = app.entries[0].plan
            assert plan is not None
            evidence = task_evidence_from_owned(plan.desired)
            assert set(evidence) == {"genre", "mood-theme", "instrument"}

            details = str(app.query_one("#inspector", Static).render())
            assert "Review candidates by task" in details
            assert "Electronic---Progressive House" in details
            assert "energetic" in details
            assert "synthesizer" in details
            assert "6 ranked scores across 3 tasks with provenance" in details
            assert "Analysis metadata: 11 internal field changes" in details
            assert "• Genre labels:" not in details

    asyncio.run(exercise())


def test_tui_details_scroll_and_return_focus_to_track_table(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    for path in (first, second):
        _silent_wav(path)
    app = SetTagApp(
        source=tmp_path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(first), _metadata_track(second)),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _task_analysis_batch(paths),
        analysis_tasks=("genre", "mood-theme", "instrument"),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 28)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(30):
                await pilot.pause(0.05)
                if app.phase == "review":
                    break

            assert app.phase == "review"
            table = app.query_one("#tracks", DataTable)
            inspector_pane = app.query_one("#inspector-pane")
            inspector_scroll = app.query_one("#inspector-scroll", VerticalScroll)

            await pilot.press("i")
            await pilot.pause()
            assert inspector_pane.display is True
            assert app.focused is inspector_scroll
            assert inspector_scroll.max_scroll_y > 0

            await pilot.press("pagedown")
            await pilot.pause()
            assert inspector_scroll.scroll_y > 0

            await pilot.press("end")
            await pilot.pause()
            assert inspector_scroll.scroll_y == inspector_scroll.max_scroll_y

            await pilot.press("home")
            await pilot.pause()
            assert inspector_scroll.scroll_y == 0

            inspector_scroll.scroll_end(animate=False, immediate=True)
            assert inspector_scroll.scroll_y == inspector_scroll.max_scroll_y
            await pilot.press("tab")
            assert app.focused is table
            await pilot.press("down")
            await pilot.pause()
            assert table.cursor_row == 1
            assert inspector_scroll.scroll_y == 0

            await pilot.press("i")
            await pilot.pause()
            assert inspector_pane.display is False
            assert app.focused is table

    asyncio.run(exercise())


def test_a_toggles_all_visible_tracks_and_n_is_unbound(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    for path in (first, second):
        _silent_wav(path)
    app = SetTagApp(
        source=tmp_path,
        initial_metadata=MetadataBatch(
            tracks=(
                _metadata_track(first),
                _metadata_track(second),
            ),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(paths),
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            assert app.analysis_selected == {0, 1}
            active_actions = {
                active.binding.action for active in app.screen.active_bindings.values()
            }
            assert "toggle_all" in active_actions
            assert "select_none" not in active_actions

            await pilot.press("a")
            assert app.analysis_selected == set()
            await pilot.press("n")
            assert app.analysis_selected == set()
            await pilot.press("a")
            assert app.analysis_selected == {0, 1}

    asyncio.run(exercise())


def test_escape_cancels_after_current_track_and_keeps_remaining_selected(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a-first.wav"
    second = tmp_path / "b-second.wav"
    for path in (first, second):
        _silent_wav(path)

    started = Event()
    release = Event()
    analyzed: list[Path] = []

    class BlockingAnalyzer(FakeAnalyzer):
        def analyze(self, path: Path) -> list[Prediction]:
            analyzed.append(path)
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test analysis was not released")
            return super().analyze(path)

    analyzer = BlockingAnalyzer()
    persisted = []

    def load_analysis(paths, on_progress, should_cancel) -> AnalysisBatch:
        return analyze_paths(
            paths,
            analyzer=analyzer,
            top=5,
            threshold=0.10,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )

    app = SetTagApp(
        source=tmp_path,
        initial_metadata=MetadataBatch(
            tracks=(
                _metadata_track(first),
                _metadata_track(second),
            ),
            failures=(),
        ),
        analysis_loader=load_analysis,
        persist_plan=persisted.append,
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(20):
                await pilot.pause(0.05)
                if started.is_set():
                    break

            active_actions = {
                active.binding.action for active in app.screen.active_bindings.values()
            }
            assert "cancel_analysis" in active_actions
            activity = app.query_one("#analysis-activity")
            activity_title = app.query_one("#analysis-activity-title", Static)
            activity_file = app.query_one("#analysis-activity-file", Static)
            activity_progress = app.query_one("#analysis-progress", ProgressBar)
            assert activity.display is True
            assert "Analyzing in background" in str(activity_title.render())
            assert "track 1 of 2" in str(activity_title.render())
            assert "0 complete" in str(activity_title.render())
            assert first.name in str(activity_file.render())
            assert activity_progress.progress == 0

            await pilot.press("escape")
            assert app._analysis_cancel_requested.is_set()
            assert "Cancel requested" in str(activity_title.render())
            assert first.name in str(activity_file.render())
            release.set()
            for _ in range(40):
                await pilot.pause(0.05)
                if not app.analysis_running:
                    break

            assert analyzed == [first]
            assert app.phase == "review"
            assert app.review_indices == {0}
            assert app.write_selected == {0}
            assert app.analysis_selected == {1}
            status = app.query_one("#status", Static)
            assert "Cancelled after 1 of 2 tracks" in str(status.render())
            assert activity.display is False

    asyncio.run(exercise())
    assert [plan.path for plan in persisted] == [first]
    assert WAVE(first).tags is None
    assert WAVE(second).tags is None


def test_analysis_progress_advances_to_the_next_filename(tmp_path: Path) -> None:
    first = tmp_path / "a-first.wav"
    second = tmp_path / "b-second.wav"
    for path in (first, second):
        _silent_wav(path)

    second_started = Event()
    release = Event()

    def load_analysis(paths, on_progress, _should_cancel) -> AnalysisBatch:
        assert len(paths) == 1
        if paths[0] == second:
            second_started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test analysis was not released")
        on_progress(1, 1, paths[0])
        return _analysis_batch(paths)

    app = SetTagApp(
        source=tmp_path,
        initial_metadata=MetadataBatch(
            tracks=(
                _metadata_track(first),
                _metadata_track(second),
            ),
            failures=(),
        ),
        analysis_loader=load_analysis,
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            try:
                for _ in range(20):
                    await pilot.pause(0.05)
                    if second_started.is_set():
                        break

                title = app.query_one("#analysis-activity-title", Static)
                current_file = app.query_one("#analysis-activity-file", Static)
                progress = app.query_one("#analysis-progress", ProgressBar)
                assert "Analyzing in background" in str(title.render())
                assert "track 2 of 2" in str(title.render())
                assert "1 complete" in str(title.render())
                assert second.name in str(current_file.render())
                assert progress.progress == 1
            finally:
                release.set()

            for _ in range(20):
                await pilot.pause(0.05)
                if not app.analysis_running:
                    break

    asyncio.run(exercise())


def test_completed_tracks_can_be_reviewed_and_written_during_analysis(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a-first.wav"
    second = tmp_path / "b-second.wav"
    for path in (first, second):
        _silent_wav(path)

    second_started = Event()
    release_second = Event()
    analysis_calls: list[Path] = []

    def load_analysis(paths, _on_progress, _should_cancel) -> AnalysisBatch:
        assert len(paths) == 1
        path = paths[0]
        analysis_calls.append(path)
        if path == second:
            second_started.set()
            if not release_second.wait(timeout=3):
                raise RuntimeError("test analysis was not released")
        return _analysis_batch(paths)

    app = SetTagApp(
        source=tmp_path,
        initial_metadata=MetadataBatch(
            tracks=(
                _metadata_track(first),
                _metadata_track(second),
            ),
            failures=(),
        ),
        analysis_loader=load_analysis,
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            try:
                for _ in range(40):
                    await pilot.pause(0.05)
                    if second_started.is_set() and app.review_indices == {0}:
                        break

                assert app.analysis_running
                assert app.phase == "choose"
                assert app.review_indices == {0}
                assert app.write_selected == {0}
                assert app.entries[0].plan is not None
                assert app.entries[1].plan is None
                status = app.query_one("#status", Static)
                assert "Analysis running in background" in str(status.render())
                assert "1 of 2 complete" in str(status.render())

                await pilot.press("i")
                assert app.has_class("details-open")
                await pilot.press("v")
                assert app.phase == "review"
                table = app.query_one("#tracks", DataTable)
                assert table.row_count == 1

                active_actions = {
                    active.binding.action for active in app.screen.active_bindings.values()
                }
                assert "write" in active_actions
                assert "cancel_analysis" in active_actions

                await pilot.press("enter")
                for _ in range(20):
                    await pilot.pause(0.05)
                    if isinstance(app.screen, ConfirmWriteScreen):
                        break
                assert isinstance(app.screen, ConfirmWriteScreen)
                await pilot.press("enter")
                for _ in range(30):
                    await pilot.pause(0.05)
                    if not app.busy and app.entries[0].plan is None:
                        break

                assert app.analysis_running
                assert app.entries[0].plan is None
                assert app.review_indices == set()
                assert app.phase == "choose"
            finally:
                release_second.set()

            for _ in range(40):
                await pilot.pause(0.05)
                if not app.analysis_running:
                    break

            assert analysis_calls == [first, second]
            assert app.entries[1].plan is not None
            assert app.review_indices == {1}
            assert app.phase == "choose"

    asyncio.run(exercise())
    assert isinstance(WAVE(first).tags, ID3)
    assert WAVE(second).tags is None


def test_tui_score_cutoff_filters_suggestion_not_stored_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    metadata = _metadata_track(path, status="current")
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(tracks=(metadata,), failures=()),
        analysis_loader=lambda _paths, _progress, _cancel: AnalysisBatch((), ()),
        score_cutoff=0.80,
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", DataTable)
            inspector = app.query_one("#inspector", Static)

            assert table.get_row_at(0)[4] == "—"
            details = str(inspector.render())
            assert "Score cutoff ≥ 0.80" in details
            assert "No candidate met the review cutoff." in details
            assert "1 additional ranked score stored for importing apps." in details
            assert "Electronic---House" not in details

    asyncio.run(exercise())


def test_tui_leaves_empty_genre_unchanged_without_review_candidate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path),),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(
            paths,
            threshold=0.80,
        ),
        score_cutoff=0.80,
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(30):
                await pilot.pause(0.05)
                if app.phase == "review":
                    break

            plan = app.entries[0].plan
            assert plan is not None
            assert plan.selected == ()
            assert plan.evidence
            assert plan.target_file_genre is None
            assert plan.standard_genre_change is None
            inspector = app.query_one("#inspector", Static)
            details = str(inspector.render())
            assert "No candidate met the review cutoff." in details
            assert "2 additional ranked scores stored for importing apps." in details

    asyncio.run(exercise())


def test_tui_defaults_empty_genre_to_suggestion_and_allows_opt_out(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    persisted = []
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path),),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(paths),
        persist_plan=persisted.append,
    )

    async def exercise() -> None:
        async with app.run_test(size=(90, 42)) as pilot:
            await pilot.pause()
            assert app.has_class("narrow")
            assert app.phase == "choose"

            await pilot.press("r")
            for _ in range(30):
                await pilot.pause(0.05)
                if app.phase == "review":
                    break

            inspector = app.query_one("#inspector", Static)
            details = str(inspector.render())
            assert "Electronic---Progressive House" in details
            assert "Suggested roll-up: Progressive House → House" in details

            plan = app.entries[0].plan
            assert plan is not None
            assert plan.target_file_genre == ("House",)
            assert plan.standard_genre_change is not None
            assert app.write_selected == {0}

            app._genre_edited(0, "")
            plan = app.entries[0].plan
            assert plan is not None
            assert plan.target_file_genre is None
            assert plan.standard_genre_change is None

            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, GenreEditScreen)
            await pilot.click("#use-suggestion")
            await pilot.pause()
            plan = app.entries[0].plan
            assert plan is not None
            assert plan.target_file_genre == ("House",)
            assert plan.standard_genre_change is not None
            assert app.write_selected == {0}

            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, GenreEditScreen)
            dialog_suggestion = app.screen.query_one("#dialog-suggestion", Static)
            assert "Standard genre suggestion: House (from model label Progressive House)" in str(
                dialog_suggestion.render()
            )
            await pilot.press("escape")

    asyncio.run(exercise())
    assert [plan.target_file_genre for plan in persisted] == [
        ("House",),
        None,
        ("House",),
    ]
    assert WAVE(path).tags is None


def test_tui_requires_genre_screen_before_replacing_an_existing_standard_genre(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TCON(encoding=3, text=["201705"]))
    audio.save()
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path, standard_genre=("201705",)),),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(paths),
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(30):
                await pilot.pause(0.05)
                if app.phase == "review":
                    break

            plan = app.entries[0].plan
            assert plan is not None
            assert plan.file_genre == ("201705",)
            assert plan.target_file_genre is None

            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, GenreEditScreen)
            await pilot.click("#use-suggestion")
            await pilot.pause()
            plan = app.entries[0].plan
            assert plan is not None
            assert plan.target_file_genre == ("House",)
            assert plan.standard_genre_change is not None
            assert plan.standard_genre_change.before == ["201705"]
            assert plan.standard_genre_change.after == ["House"]

    asyncio.run(exercise())
    tags = WAVE(path).tags
    assert isinstance(tags, ID3)
    assert tags["TCON"].text == ["201705"]


def test_tui_genre_screen_keeps_actions_visible_in_narrow_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _analysis_batch([path]).planned[0]
    metadata = replace(
        _metadata_track(path),
        cached_plan=plan,
        cache_status="ready",
    )
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(tracks=(metadata,), failures=()),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(paths),
    )

    async def exercise() -> None:
        async with app.run_test(size=(50, 32)) as pilot:
            await pilot.pause()
            await pilot.press("v")
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, GenreEditScreen)
            buttons = list(app.screen.query(Button))
            assert len({button.region.y for button in buttons}) == len(buttons)
            assert all(
                button.region.x + button.region.width <= app.size.width for button in buttons
            )

    asyncio.run(exercise())


def test_tui_track_columns_fit_terminal_and_expand_with_available_width(
    tmp_path: Path,
) -> None:
    path = tmp_path / f"{'very-long-track-name-' * 8}.wav"
    _silent_wav(path)
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path, standard_genre=("House",)),),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(paths),
    )

    async def exercise() -> None:
        async with app.run_test(size=(50, 32)) as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", DataTable)
            narrow_keys = [column.key.value for column in table.ordered_columns]
            assert narrow_keys == [
                "selected",
                "track",
                "file_genre",
                "analysis",
            ]
            assert table.get_row_at(0)[1] == path.name
            assert table.max_scroll_x == 0

            await pilot.resize_terminal(140, 32)
            await pilot.pause()
            wide_keys = [column.key.value for column in table.ordered_columns]
            assert wide_keys == [
                "selected",
                "track",
                "file_genre",
                "analysis",
                "suggested",
                "score",
                "changes",
            ]
            assert table.max_scroll_x == 0
            track_column = next(
                column for column in table.ordered_columns if column.key.value == "track"
            )
            assert track_column.width > 8

            await pilot.press("i")
            await pilot.resize_terminal(100, 32)
            await pilot.pause()
            detail_keys = [column.key.value for column in table.ordered_columns]
            assert len(detail_keys) < len(wide_keys)
            assert {"selected", "track", "analysis"} <= set(detail_keys)
            assert table.max_scroll_x == 0

    asyncio.run(exercise())


def test_tui_restores_cached_plan_in_library_and_opens_review_on_request(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    plan = _analysis_batch([path]).planned[0]
    metadata = replace(
        _metadata_track(path),
        cached_plan=plan,
        cache_status="ready",
    )
    persisted = []

    def unexpected_analysis(_paths, _progress, _cancel):
        raise AssertionError("restoring a ready plan must not run analysis")

    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(tracks=(metadata,), failures=()),
        analysis_loader=unexpected_analysis,
        persist_plan=persisted.append,
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", DataTable)
            status = app.query_one("#status", Static)
            inspector = app.query_one("#inspector", Static)
            assert app.phase == "choose"
            assert app.review_indices == {0}
            assert app.write_selected == {0}
            assert app.analysis_selected == set()
            assert table.get_row_at(0)[3].startswith("Ready · ")
            assert "Restored 1 ready-to-review track" in str(status.render())
            assert "Press V to review" in str(status.render())
            assert "Local result ready to review" in str(inspector.render())
            assert "Press V to review this saved result." in str(inspector.render())
            active_actions = {
                active.binding.action for active in app.screen.active_bindings.values()
            }
            assert "review" in active_actions
            assert "write" not in active_actions

            await pilot.press("v")
            assert app.phase == "review"
            app._genre_edited(0, "House, Techno")
            assert persisted[-1].target_file_genre == ("House", "Techno")

            await pilot.press("b")
            assert app.phase == "choose"
            assert app.analysis_selected == set()

    asyncio.run(exercise())


def test_tui_requires_confirmation_then_writes_and_verifies(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    discarded: list[tuple[Path, ...]] = []
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path),),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(paths),
        discard_plans=lambda paths: discarded.append(tuple(paths)),
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", DataTable)
            status = app.query_one("#status", Static)
            await pilot.press("r")
            for _ in range(30):
                await pilot.pause(0.05)
                if app.phase == "review":
                    break

            assert app.write_selected == {0}
            await pilot.press("a")
            assert app.write_selected == set()
            await pilot.press("a")
            assert app.write_selected == {0}
            await pilot.press("space")
            assert app.write_selected == set()
            await pilot.press("space")
            assert app.write_selected == {0}
            plan = app.entries[0].plan
            assert plan is not None
            assert plan.target_file_genre == ("House",)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmWriteScreen)
            confirm = app.screen.query_one("#confirm", Button)
            cancel = app.screen.query_one("#cancel", Button)
            assert confirm.has_focus
            assert confirm.styles.background.hex == "#D0794F"
            assert cancel.styles.background.hex == "#25302D"
            summary = app.screen.query_one("#confirm-summary", Static)
            assert "SetTag analysis bundle" in str(summary.render())
            assert "2 ranked scores" in str(summary.render())
            assert "SetTag field changes" not in str(summary.render())
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert app.phase == "choose"
            assert app.busy is False
            assert app.review_indices == set()
            assert app.write_selected == set()
            assert app.entries[0].plan is None
            assert app.entries[0].metadata is not None
            assert app.entries[0].metadata.status == "current"
            assert app.entries[0].metadata.genre_state.standard == ("House",)
            assert table.get_row_at(0)[3].startswith("Up to date · ")
            assert "Done. 1 file written and verified." in str(status.render())

    asyncio.run(exercise())
    assert discarded == [(path,)]
    tags = WAVE(path).tags
    assert isinstance(tags, ID3)
    assert tags["TCON"].text == ["House"]
    assert tags["TXXX:SETTAG_GENRE"].text == [
        "Electronic---Progressive House",
        "Electronic---Techno",
    ]


def test_tui_write_is_journaled_and_can_be_undone_in_app(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path),),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(paths),
        journal=journal,
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            status = app.query_one("#status", Static)
            await pilot.press("r")
            for _ in range(30):
                await pilot.pause(0.05)
                if app.phase == "review":
                    break
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmWriteScreen)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert app.entries[0].metadata is not None
            assert app.entries[0].metadata.status == "current"

            tags = WAVE(path).tags
            assert isinstance(tags, ID3)
            assert tags["TCON"].text == ["House"]

            await pilot.press("u")
            for _ in range(30):
                await pilot.pause(0.05)
                if isinstance(app.screen, UndoScreen):
                    break
            assert isinstance(app.screen, UndoScreen)
            undo_table = app.screen.query_one("#undo-table", DataTable)
            assert undo_table.row_count == 1
            assert "1 track, 1 file genre edit" in undo_table.get_row_at(0)[2]

            await pilot.press("enter")
            for _ in range(30):
                await pilot.pause(0.05)
                if isinstance(app.screen, ConfirmUndoScreen):
                    break
            assert isinstance(app.screen, ConfirmUndoScreen)
            await pilot.press("enter")
            for _ in range(40):
                await pilot.pause(0.05)
                if not app.busy and app.phase == "choose":
                    break

            assert app.busy is False
            assert app.entries[0].metadata is not None
            assert app.entries[0].metadata.status == "not_analyzed"
            assert app.entries[0].metadata.genre_state.standard == ()
            assert "Restored 1 file" in str(status.render())

    asyncio.run(exercise())
    restored = WAVE(path).tags
    assert restored is None or "TCON" not in restored
    assert restored is None or "TXXX:SETTAG_GENRE" not in restored

    batch = journal.latest()
    assert batch is not None
    assert batch.reverted_at is not None


def test_tui_undo_reports_an_empty_journal(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path),),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(paths),
        journal=WriteJournal(tmp_path / "journal.sqlite3"),
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            status = app.query_one("#status", Static)
            await pilot.press("u")
            for _ in range(30):
                await pilot.pause(0.05)
                if not app.busy:
                    break

            assert not isinstance(app.screen, UndoScreen)
            assert app.busy is False
            assert "nothing to undo" in str(status.render())

    asyncio.run(exercise())


def test_tui_without_a_journal_declines_to_undo(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path),),
            failures=(),
        ),
        analysis_loader=lambda paths, _progress, _cancel: _analysis_batch(paths),
    )

    async def exercise() -> None:
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause(0.2)

            assert not isinstance(app.screen, UndoScreen)
            assert app.busy is False

    asyncio.run(exercise())
