from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from mutagen.id3 import TIT2
from test_enrichment import RELEASE, Provider, audio_file
from test_freshness import model_result, scan
from textual.widgets import Static

from settag.enrichment import EnrichmentLoader
from settag.tags import apply_metadata_tags, owned_tag_store
from settag.tui import SetTagApp
from settag.tui.review import ReviewTree
from settag.workflow import MetadataBatch


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
@pytest.mark.parametrize("saved", [False, True])
def test_library_explains_catalog_and_model_for_pending_and_saved_results(tmp_path, size, saved):
    path = audio_file(tmp_path / "track.wav", seconds=35)
    audio, config = model_result(path)
    apply_metadata_tags(path, audio.desired)
    loader = EnrichmentLoader(
        lambda *_a: pytest.fail("Current audio must be reused"),
        provider=Provider((replace(RELEASE, duration_seconds=35),)),
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    plan = loader((path,), lambda *_a: None, lambda: False).planned[0]
    if saved:
        apply_metadata_tags(path, plan.desired)
        metadata = scan(path, config)
    else:
        metadata = replace(scan(path, config), cached_plan=plan, cache_status="ready")
    before = path.read_bytes()
    app = SetTagApp(
        source=path,
        initial_metadata=MetadataBatch((metadata,), ()),
        analysis_loader=lambda *_a: pytest.fail("Viewing sources must not run enrichment"),
        analysis_tasks=("genre",),
    )

    async def exercise():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.press("i")
            details = str(app.query_one("#inspector", Static).render())
            assert "Recommendation: Keep Progressive House" in details
            assert "Based on: Beatport verified matches" in details
            assert "Beatport · verified track match" in details
            assert RELEASE.url in details
            assert "Audio models · predictions" in details
            assert "Audio last analyzed:" in details
            assert "has not been loaded" not in details
            if not saved:
                await pilot.press("i", "v")
                tree = app.query_one(ReviewTree)
                assert "Beatport verified matches" in str(
                    tree.nodes[(0, "recommendation-source")].label
                )
            assert path.read_bytes() == before

    asyncio.run(exercise())


def test_background_completion_reports_partial_catalog_results(tmp_path):
    paths = [audio_file(tmp_path / name, seconds=35) for name in ("a.wav", "b.wav")]
    second = owned_tag_store(paths[1])
    second.audio.tags.add(TIT2(encoding=3, text=["Another track"]))
    second.audio.save()
    metadata = []
    for path in paths:
        audio, config = model_result(path)
        apply_metadata_tags(path, audio.desired)
        metadata.append(scan(path, config))

    class SometimesUnavailable(Provider):
        calls = 0

        def candidates(self, track):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("Catalog service unavailable")
            return super().candidates(track)

    loader = EnrichmentLoader(
        lambda *_a: pytest.fail("Current audio must be reused"),
        provider=SometimesUnavailable((replace(RELEASE, duration_seconds=35),)),
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    app = SetTagApp(
        source=tmp_path,
        initial_metadata=MetadataBatch(tuple(metadata), ()),
        analysis_loader=loader,
        analysis_tasks=("genre",),
    )

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "1 current  ·  1 partial  ·  0 failed" in str(
                app.query_one("#status", Static).render()
            )
            assert str(app.query_one("#context", Static).render()) == "1 ready to review"
            tree = app.query_one(ReviewTree)
            assert (1, "track") not in tree.nodes
            assert app.write_selected == {0}
            assert tuple(item.path for item in app._selected_items()) == (paths[0],)
            assert app.entries[1].plan is not None
            details = "\n".join(app._metadata_inspector(app.entries[1], 1))
            assert "unavailable" in details
            assert "Audio model" in details

    asyncio.run(exercise())
