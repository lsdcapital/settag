import asyncio
import os
import time
import wave
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from textual.widgets import Static

from settag.analysis_worker import SubprocessAnalysisLoader
from settag.policy import Prediction
from settag.tags import OWNED_DESCRIPTIONS, GenreState
from settag.tui import SetTagApp
from settag.workflow import MetadataBatch, MetadataTrack


class _FakeAnalyzer:
    spec = SimpleNamespace(id="model/v1")
    model_manifest = {"id": "model/v1", "files": {}}
    backend_version = "test"

    def analyze(self, _path: Path) -> list[Prediction]:
        return [
            Prediction("Electronic---Progressive House", 0.72),
            Prediction("Electronic---Techno", 0.18),
        ]


class _BusyAnalyzer(_FakeAnalyzer):
    def __init__(self, started_path: Path) -> None:
        self.started_path = started_path

    def analyze(self, path: Path) -> list[Prediction]:
        self.started_path.write_text(path.name, encoding="utf-8")
        finish_at = time.monotonic() + 1.2
        while time.monotonic() < finish_at:
            pass
        return super().analyze(path)


def _recording_analyzer_factory(
    model_dir: Path,
    tasks: tuple[str, ...],
) -> _FakeAnalyzer:
    assert tasks == ("genre",)
    with (model_dir / "factory.log").open("a", encoding="utf-8") as marker:
        marker.write(f"{os.getpid()}\n")
    return _FakeAnalyzer()


def _busy_analyzer_factory(
    model_dir: Path,
    tasks: tuple[str, ...],
) -> _BusyAnalyzer:
    assert tasks == ("genre",)
    return _BusyAnalyzer(model_dir / "analysis-started")


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 80)


def _metadata_track(path: Path) -> MetadataTrack:
    return MetadataTrack(
        path=path,
        genre_state=GenreState(standard=(), settag=()),
        owned={description: None for description in OWNED_DESCRIPTIONS},
        stored_predictions=(),
        status="not_analyzed",
        analyzed_at=None,
    )


def test_worker_reuses_analyzer_and_stops_between_tracks(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    third = tmp_path / "third.wav"
    for path in (first, second, third):
        _silent_wav(path)

    loader = SubprocessAnalysisLoader(
        tmp_path,
        ("genre",),
        top=5,
        threshold=0.10,
        analyzer_factory=_recording_analyzer_factory,
        poll_interval=0.01,
    )
    progress: list[tuple[int, int, Path]] = []
    cancel = Event()
    try:
        first_batch = loader(
            (first,),
            lambda completed, total, path: progress.append((completed, total, path)),
            cancel.is_set,
        )

        def cancel_after_first(completed: int, total: int, path: Path) -> None:
            progress.append((completed, total, path))
            cancel.set()

        cancelled_batch = loader(
            (second, third),
            cancel_after_first,
            cancel.is_set,
        )
    finally:
        loader.close()
        loader.close()

    assert [item.path for item in first_batch.planned] == [first]
    assert [item.path for item in cancelled_batch.planned] == [second]
    assert cancelled_batch.cancelled is True
    assert progress == [(1, 1, first), (1, 2, second)]
    worker_pids = (tmp_path / "factory.log").read_text(encoding="utf-8").splitlines()
    assert len(worker_pids) == 1
    assert int(worker_pids[0]) != os.getpid()


def test_tui_remains_responsive_during_gil_holding_analysis(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    started_path = tmp_path / "analysis-started"
    loader = SubprocessAnalysisLoader(
        tmp_path,
        ("genre",),
        top=5,
        threshold=0.10,
        analyzer_factory=_busy_analyzer_factory,
        poll_interval=0.01,
    )
    loader.start()
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            tracks=(_metadata_track(path),),
            failures=(),
        ),
        analysis_loader=loader,
    )

    async def exercise() -> None:
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(300):
                await pilot.pause(0.01)
                if started_path.exists():
                    break

            assert started_path.exists()
            assert app.analysis_running

            started = asyncio.get_running_loop().time()
            await pilot.press("i")
            assert asyncio.get_running_loop().time() - started < 0.5
            assert app.has_class("details-open")

            started = asyncio.get_running_loop().time()
            await pilot.press("escape")
            assert asyncio.get_running_loop().time() - started < 0.5
            assert app._analysis_cancel_requested.is_set()
            title = app.query_one("#analysis-activity-title", Static)
            assert "Cancel requested" in str(title.render())

            for _ in range(300):
                await pilot.pause(0.01)
                if not app.analysis_running:
                    break
            assert not app.analysis_running

    try:
        asyncio.run(exercise())
    finally:
        loader.close()
