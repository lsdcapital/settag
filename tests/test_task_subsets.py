"""Re-analysis runs only the tasks a file is missing or holds out of date."""

from __future__ import annotations

import wave
from collections.abc import Sequence
from pathlib import Path

import pytest

from settag.analysis_worker import AnalysisWorkerError, SubprocessAnalysisLoader
from settag.enrichment import EnrichmentLoader
from settag.policy import Prediction
from settag.records import config_record
from settag.state import WorkbenchStore
from settag.tags import (
    GenreState,
    apply_metadata_tags,
    read_owned_values,
    read_task_provenance,
)
from settag.tasks import AnalysisTask
from settag.tui.entries import TrackEntry
from settag.tui.table import RowContext, entry_analysis
from settag.workflow import (
    AnalysisBatch,
    MetadataTrack,
    inspect_paths,
    planned_write_for_track,
    prepare_track,
)

TASKS: tuple[AnalysisTask, ...] = ("genre", "mood-theme", "mood-aggressive")
CONFIG = config_record(top=5, threshold=0.10, tasks=TASKS)
RESULTS: dict[AnalysisTask, list[Prediction]] = {
    "genre": [Prediction("Electronic---House", 0.72)],
    "mood-theme": [Prediction("energetic", 0.4), Prediction("calm", 0.1)],
    "mood-aggressive": [Prediction("Aggressive", 0.75), Prediction("Not Aggressive", 0.25)],
}


class SubsetAnalyzer:
    """Returns only the tasks it is asked for and remembers each request."""

    backend_version = "test"

    def __init__(self, model_ids: dict[AnalysisTask, str] | None = None) -> None:
        self.model_ids = {task: f"model/{task}/v1" for task in TASKS} | (model_ids or {})
        self.calls: list[tuple[AnalysisTask, ...]] = []

    @property
    def model_manifests(self) -> dict[AnalysisTask, dict[str, object]]:
        return {
            task: {"schema": "settag.models/v1", "id": model_id, "files": {}}
            for task, model_id in self.model_ids.items()
        }

    def analyze_tasks(
        self, path: Path, tasks: Sequence[AnalysisTask] | None = None
    ) -> dict[AnalysisTask, list[Prediction]]:
        run = TASKS if tasks is None else tuple(tasks)
        self.calls.append(run)
        return {task: RESULTS[task] for task in run}


def _subset_analyzer_factory(
    _model_dir: Path, _tasks: tuple[AnalysisTask, ...], _sample: str
) -> SubsetAnalyzer:
    return SubsetAnalyzer()


def _track(path: Path) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 8_000 * 35)
    return path


def _analyze_and_write(
    path: Path, analyzer: SubsetAnalyzer, tasks: Sequence[AnalysisTask] | None = None
):
    track = prepare_track(path, analyzer=analyzer, top=5, threshold=0.10, tasks=tasks)
    apply_metadata_tags(path, track.desired)
    return track


def _scan(
    path: Path, store: WorkbenchStore, model_ids: dict[AnalysisTask, str] | None = None
) -> MetadataTrack:
    return inspect_paths(
        (path,),
        expected_model_ids=SubsetAnalyzer(model_ids).model_ids,
        expected_config_sha256=str(CONFIG["sha256"]),
        expected_config=CONFIG,
        state_store=store,
    ).tracks[0]


def test_a_newly_configured_task_is_the_only_one_run_and_the_rest_are_kept(
    tmp_path: Path,
) -> None:
    path = _track(tmp_path / "track.wav")
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    _analyze_and_write(path, SubsetAnalyzer(), ("genre", "mood-theme"))
    before = read_owned_values(path)

    stale = _scan(path, store)
    assert stale.status == "stale"
    assert stale.tasks_to_analyze == ("mood-aggressive",)

    analyzer = SubsetAnalyzer()
    track = _analyze_and_write(path, analyzer, stale.tasks_to_analyze)

    assert analyzer.calls == [("mood-aggressive",)]
    after = read_owned_values(path)
    for field in ("SETTAG_GENRE", "SETTAG_GENRE_SCORES", "SETTAG_MOOD_THEME"):
        assert after[field] == before[field]
    assert after["SETTAG_MOOD_AGGRESSIVE"] == ["Aggressive", "Not Aggressive"]
    assert set(read_task_provenance(after)) == set(TASKS)
    # Genre did not run, but the file still gets a genre suggestion from its stored evidence.
    assert track.selected == [Prediction("Electronic---House", 0.72)]
    assert _scan(path, store).status == "current"


def test_a_model_change_reruns_only_that_task(tmp_path: Path) -> None:
    path = _track(tmp_path / "track.wav")
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    _analyze_and_write(path, SubsetAnalyzer())

    stale = _scan(path, store, {"mood-theme": "model/mood-theme/v2"})

    assert stale.status == "stale"
    assert stale.tasks_to_analyze == ("mood-theme",)


def test_a_file_never_analyzed_runs_every_task(tmp_path: Path) -> None:
    path = _track(tmp_path / "track.wav")

    track = _scan(path, WorkbenchStore(tmp_path / "state.sqlite3"))

    assert track.status == "not_analyzed"
    assert track.tasks_to_analyze is None


class _NoCatalog:
    requests = 0
    cache_hits = 0

    def candidates(self, track):
        return ()

    def details(self, candidate):
        return [candidate]


def test_enrichment_asks_the_analyzer_for_only_the_stale_tasks(tmp_path: Path) -> None:
    path = _track(tmp_path / "track.wav")
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    _analyze_and_write(path, SubsetAnalyzer(), ("genre", "mood-theme"))
    requested: list[Sequence[AnalysisTask] | None] = []
    analyzer = SubsetAnalyzer()

    def audio_loader(paths, _on_progress, _should_cancel, tasks=None) -> AnalysisBatch:
        requested.append(tasks)
        track = prepare_track(paths[0], analyzer=analyzer, top=5, threshold=0.10, tasks=tasks)
        return AnalysisBatch((planned_write_for_track(track),), ())

    batch = EnrichmentLoader(
        audio_loader,
        provider=_NoCatalog(),
        expected_model_ids=SubsetAnalyzer().model_ids,
        expected_config=CONFIG,
        state_store=store,
    )((path,), lambda *_args: None, lambda: False)

    assert requested == [("mood-aggressive",)]
    assert len(batch.planned) == 1
    assert batch.planned[0].desired["SETTAG_MOOD_THEME"] == ["energetic", "calm"]


def test_the_worker_runs_the_requested_tasks_and_refuses_unloaded_ones(tmp_path: Path) -> None:
    path = _track(tmp_path / "track.wav")
    loader = SubprocessAnalysisLoader(
        tmp_path,
        TASKS,
        top=5,
        threshold=0.10,
        analyzer_factory=_subset_analyzer_factory,
        poll_interval=0.01,
    )
    try:
        batch = loader((path,), lambda *_args: None, lambda: False, ("mood-aggressive",))
        with pytest.raises(AnalysisWorkerError, match="cannot run instrument"):
            loader((path,), lambda *_args: None, lambda: False, ("instrument",))
    finally:
        loader.close()

    assert batch.failures == ()
    assert set(read_task_provenance(batch.planned[0].desired)) == {"mood-aggressive"}


@pytest.mark.parametrize(
    ("stale_tasks", "label"),
    [(("mood-aggressive",), "Out of date · Intensity"), (TASKS, "Out of date")],
)
def test_the_library_names_the_tasks_a_partial_reanalysis_runs(
    stale_tasks: tuple[AnalysisTask, ...], label: str
) -> None:
    path = Path("/music/track.wav")
    metadata = MetadataTrack(
        path=path,
        genre_state=GenreState(standard=(), settag=()),
        owned={},
        stored_predictions=(),
        status="stale",
        analyzed_at=None,
        stale_tasks=stale_tasks,
    )
    context = RowContext(tasks=TASKS, review_top=5, score_cutoff=0.10)

    assert entry_analysis(TrackEntry(path=path, metadata=metadata), context) == label
