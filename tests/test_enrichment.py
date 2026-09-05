from __future__ import annotations

import asyncio
import json
import shutil
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.id3 import ID3, TCON, TIT2, TPE1
from mutagen.wave import WAVE
from textual.widgets import Static

from settag.beatport import Candidate, LookupStopped, TrackIdentity
from settag.cli import main
from settag.cli.args import build_parser
from settag.enrichment import EnrichmentLoader, enrichment_plan, genre_evidence, track_identity
from settag.plans import load_plan, planned_write_record
from settag.state import WorkbenchStore
from settag.tags import owned_tag_store, read_genre_state, read_owned_values
from settag.tui import SetTagApp
from settag.workflow import AnalysisBatch, MetadataBatch, MetadataTrack

TRACK = TrackIdentity("Opus", ("Eric Prydz",), 2.0)
RELEASE = Candidate(
    "123", "Opus", ("Eric Prydz",), "Original Mix", 2.0, ("Progressive House",), detail_page=True
)


class Provider:
    def __init__(self, candidates=(RELEASE,), details=None):
        self.results = candidates
        self.detail_results = details
        self.requests = 0
        self.cache_hits = 0

    def candidates(self, track):
        return self.results

    def details(self, candidate):
        return (
            self.detail_results
            if self.detail_results is not None
            else [replace(candidate, detail_page=True)]
        )


def audio_file(path: Path, *, seconds: int = 2) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * (8000 * seconds))
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TIT2(encoding=3, text=["Opus"]))
    audio.tags.add(TPE1(encoding=3, text=["Eric Prydz"]))
    audio.tags.add(TCON(encoding=3, text=["Progressive House"]))
    audio.save()
    return path


def test_multi_release_consensus_and_conflict():
    other = replace(RELEASE, track_id="456", detail_page=False)
    result = genre_evidence(TRACK, Provider((RELEASE, other)))
    assert result is not None
    assert result["status"] == "consensus"
    assert result["agreed_genres"] == ["Progressive House"]
    other = replace(other, genres=("Dance / Pop",))
    result = genre_evidence(TRACK, Provider((RELEASE, other)))
    assert result is not None
    assert result["status"] == "conflicting_genres"
    assert result["agreed_genres"] == []
    assert result["alternative_genres"] == ["Dance / Pop", "Progressive House"]
    assert isinstance(result["sources"], list)
    assert len(result["sources"]) == 2


def test_failed_detail_never_becomes_consensus():
    with pytest.raises(LookupStopped):
        genre_evidence(
            TRACK, Provider((RELEASE, replace(RELEASE, track_id="456", detail_page=False)), [])
        )
    assert genre_evidence(TRACK, Provider((replace(RELEASE, mix="Four Tet Remix"),))) is None


def test_enrichment_apply_undo_and_idempotence(tmp_path):
    path = audio_file(tmp_path / "track.wav")
    original = path.read_bytes()
    item, row = enrichment_plan(path, Provider())
    assert path.read_bytes() == original
    assert row["status"] == "consensus"
    assert item is not None
    assert item.target_file_genre is None
    plan = tmp_path / "plan.jsonl"
    plan.write_text(json.dumps(planned_write_record(item)) + "\n")
    assert load_plan(plan)[0].desired == item.desired
    assert main(["apply", str(plan), "--yes"]) == 0
    assert read_genre_state(path).standard == ("Progressive House",)
    stored = read_owned_values(path)["SETTAG_BEATPORT"]
    assert stored
    assert json.loads(stored[0])["agreed_genres"] == ["Progressive House"]
    second, _ = enrichment_plan(path, Provider())
    assert second is not None
    assert second.owned_changes == ()
    assert main(["undo", "--yes"]) == 0
    assert read_owned_values(path)["SETTAG_BEATPORT"] is None
    assert read_genre_state(path).standard == ("Progressive House",)


def test_unified_loader_retains_audio_on_blocked_lookup(tmp_path):

    path = audio_file(tmp_path / "a.wav")
    audio_plan, _ = enrichment_plan(path, Provider())
    assert audio_plan is not None
    audio_plan = replace(audio_plan, desired={**audio_plan.desired, "SETTAG_BEATPORT": None})

    class Blocked(Provider):
        def candidates(self, track):
            raise LookupStopped("HTTP 429")

    loader = EnrichmentLoader(lambda *_args: AnalysisBatch((audio_plan,), ()), provider=Blocked())
    batch = loader((path,), lambda *_args: None, lambda: False)
    assert not batch.failures
    assert batch.planned[0].desired["SETTAG_BEATPORT"] is None
    assert "HTTP 429" in batch.planned[0].notices[0]
    assert batch.planned[0].file_genre == ("Progressive House",)


def test_unified_loader_retains_catalog_if_model_unavailable(tmp_path):

    path = audio_file(tmp_path / "a.wav")

    def unavailable(*_args):
        raise RuntimeError("Model not installed")

    batch = EnrichmentLoader(unavailable, provider=Provider())(
        (path,), lambda *_args: None, lambda: False
    )
    assert not batch.failures
    assert batch.planned[0].desired["SETTAG_BEATPORT"]
    assert "Model not installed" in batch.planned[0].notices[0]


def test_unified_loader_reuses_current_audio(tmp_path, monkeypatch):

    path = audio_file(tmp_path / "a.wav")
    monkeypatch.setattr(
        "settag.enrichment.inspect_track",
        lambda *_args, **_kw: SimpleNamespace(
            status="current", owned=read_owned_values(path), genre_state=read_genre_state(path)
        ),
    )

    def unexpected(*_args):
        pytest.fail("Current audio evidence must not run the model again")

    batch = EnrichmentLoader(
        unexpected, provider=Provider(), expected_model_ids={}, expected_config={"sha256": "test"}
    )((path,), lambda *_args: None, lambda: False)
    assert batch.planned[0].desired["SETTAG_BEATPORT"]
    assert not batch.planned[0].notices


def test_enrich_is_same_app_entry_point():

    parser = build_parser()
    regular = vars(parser.parse_args(["run", "/music", "--no-tui"]))
    enriched = vars(parser.parse_args(["enrich", "/music", "--no-tui"]))
    regular.pop("command")
    enriched.pop("command")
    assert regular == enriched


@pytest.mark.parametrize("filename", ["tagged.flac", "tagged.m4a"])
def test_enrichment_other_containers(tmp_path, filename):
    path = tmp_path / filename
    shutil.copyfile(Path(__file__).parent / "fixtures" / filename, path)
    store = owned_tag_store(path)
    store.audio.tags["artist" if filename.endswith(".flac") else "\xa9ART"] = ["Eric Prydz"]
    store.audio.save()
    identity = track_identity(store)
    assert identity.title
    assert identity.artists
    release = replace(
        RELEASE,
        title=identity.title,
        artists=identity.artists,
        duration_seconds=identity.duration_seconds,
        mix="",
    )
    before = store.genre_state().standard
    item, _ = enrichment_plan(path, Provider((release,)))
    assert item is not None
    plan = tmp_path / "plan.jsonl"
    plan.write_text(json.dumps(planned_write_record(item)) + "\n")
    assert main(["apply", str(plan), "--yes"]) == 0
    assert read_owned_values(path)["SETTAG_BEATPORT"]
    assert read_genre_state(path).standard == before


def test_cancellation_stops_before_release_details(tmp_path):
    path = audio_file(tmp_path / "track.wav")
    candidate = replace(RELEASE, detail_page=False)
    stopped = False

    class Cancelling(Provider):
        def candidates(self, track):
            nonlocal stopped
            stopped = True
            return (candidate,)

        def details(self, candidate):
            pytest.fail("Cancellation must prevent the next detail request")

    item, _ = enrichment_plan(path, Provider())
    assert item is not None
    loader = EnrichmentLoader(lambda *_args: AnalysisBatch((item,), ()), provider=Cancelling())
    batch = loader((path,), lambda *_args: None, lambda: stopped)
    assert batch.cancelled
    assert batch.planned
    assert "cancelled" in batch.planned[0].notices[0]


def test_catalog_only_result_survives_workbench_reload(tmp_path):
    path = audio_file(tmp_path / "track.wav")
    item, _ = enrichment_plan(path, Provider())
    assert item is not None
    store = WorkbenchStore(tmp_path / "state.sqlite3")
    store.save(replace(item, notices=("Audio model unavailable",)))
    cached = store.load(
        (path,), expected_model_ids={"genre": "test"}, expected_config_sha256="test"
    )
    assert cached[path].status == "ready"
    assert cached[path].plan.notices == ("Audio model unavailable",)


@pytest.mark.parametrize("size", [(100, 24), (140, 42)])
def test_tui_one_action_reviews_catalog_when_audio_unavailable(tmp_path, size):
    path = audio_file(tmp_path / "track.wav")

    def unavailable(*_args):
        raise RuntimeError("Model not installed")

    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch(
            (
                MetadataTrack(
                    path=path,
                    genre_state=read_genre_state(path),
                    owned=read_owned_values(path),
                    stored_predictions=(),
                    status="not_analyzed",
                    analyzed_at=None,
                ),
            ),
            (),
        ),
        analysis_loader=EnrichmentLoader(unavailable, provider=Provider()),
    )

    async def exercise():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.press("r")
            for _ in range(30):
                await pilot.pause(0.05)
                if not app.analysis_running:
                    break
            assert app.phase == "review"
            assert app.entries[0].plan is not None
            assert app.entries[0].plan.target_file_genre is None
            await pilot.press("i")
            details = str(app.query_one("#inspector", Static).render())
            assert "Recommendation: Keep Progressive House" in details
            assert "Catalog genre: Progressive House" in details
            assert "Model not installed" in details
            assert RELEASE.url in details
            assert app.query_one("#analysis-activity").display is False
            assert not read_owned_values(path)["SETTAG_BEATPORT"]

    asyncio.run(exercise())
