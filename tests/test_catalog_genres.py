from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from mutagen.id3 import TXXX
from mutagen.wave import WAVE
from test_enrichment import RELEASE, Provider, audio_file
from test_freshness import model_result
from test_review_evidence import example

from settag.enrichment import EnrichmentLoader
from settag.freshness import record_values
from settag.plans import (
    catalog_genres,
    planned_write_from_record,
    planned_write_record,
    stage_default_file_genre,
    stage_file_genre,
)
from settag.policy import Prediction
from settag.review_evidence import describe_evidence
from settag.tags import apply_metadata_tags, read_genre_state
from settag.workflow import apply_prepared, apply_undo, preflight_plan


def test_verified_catalog_replaces_existing_genre_but_manual_choice_survives(tmp_path):
    plan = replace(example(tmp_path), file_genre=("House",))
    proposed = stage_default_file_genre(plan)
    assert proposed.target_file_genre == ("Melodic House & Techno",)
    assert proposed.genre_edit_source == "beatport"
    assert describe_evidence(proposed).recommendation_source == "Beatport verified matches"
    kept = stage_file_genre(proposed, plan.file_genre)
    reopened = planned_write_from_record(planned_write_record(kept))
    assert stage_default_file_genre(reopened).target_file_genre is None
    assert reopened.genre_edit_source == "manual"


@pytest.mark.parametrize("old", [(), ("House",), ("Dance / Pop",)])
def test_all_release_labels_are_staged_with_stable_primary(tmp_path, old):
    plan = example(tmp_path)
    data = json.loads(plan.desired["SETTAG_BEATPORT"][0])
    data["sources"].append(
        {**data["sources"][0], "genres": ["Dance / Pop", "melodic house & techno"]}
    )
    data.update(
        status="conflicting_genres",
        agreed_genres=[],
        alternative_genres=["Melodic House & Techno", "Dance / Pop"],
    )
    plan = replace(
        plan, file_genre=old, desired={**plan.desired, "SETTAG_BEATPORT": [json.dumps(data)]}
    )
    proposed = stage_default_file_genre(plan)
    expected = (
        ("Dance / Pop", "Melodic House & Techno")
        if old == ("Dance / Pop",)
        else ("Melodic House & Techno", "Dance / Pop")
    )
    assert proposed.target_file_genre == expected
    assert catalog_genres(plan.desired, old) == expected
    review = describe_evidence(proposed)
    assert "Multiple Beatport genres" in "\n".join(review.catalog_details)
    assert "conflict" not in review.recommendation.lower()


def test_model_fallback_does_not_replace_existing_and_is_superseded_by_catalog(tmp_path):
    plan = example(tmp_path)
    pending = stage_file_genre(replace(plan, file_genre=()), ("House",), source="model")
    assert stage_default_file_genre(pending).target_file_genre == ("Melodic House & Techno",)
    missing = replace(
        plan,
        file_genre=("Techno",),
        selected=(Prediction("Electronic---House", 0.9),),
        desired={
            **plan.desired,
            "SETTAG_ENRICHMENT": record_values(
                audio_complete=True, catalog={"status": "no_match", "checked_at": time.time()}
            ),
        },
    )
    assert stage_default_file_genre(missing).target_file_genre is None


def test_multiple_genres_write_and_undo_through_normal_flow(tmp_path):
    path = audio_file(tmp_path / "multiple.wav", seconds=35)
    audio, config = model_result(path)
    apply_metadata_tags(path, audio.desired)
    provider = Provider(
        (
            replace(RELEASE, duration_seconds=35),
            replace(RELEASE, track_id="456", duration_seconds=35, genres=("Dance / Pop",)),
        )
    )
    loader = EnrichmentLoader(
        lambda *_a: pytest.fail("Current audio must be reused"),
        provider=provider,
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    before = path.read_bytes()
    plan = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert path.read_bytes() == before
    assert plan.target_file_genre == ("Progressive House", "Dance / Pop")
    manual = stage_file_genre(plan, ("Techno",))
    loader.cached_audio = lambda _path: manual
    assert loader((path,), lambda *_a: None, lambda: False).planned[0].target_file_genre == (
        "Techno",
    )
    writes = []
    assert apply_prepared(preflight_plan((plan,)), on_write=writes.append) == 1
    assert read_genre_state(path).standard == ("Progressive House", "Dance / Pop")
    assert apply_undo(writes) == 1
    assert read_genre_state(path).standard == ("Progressive House",)


def test_exact_beatport_id_excludes_other_release_labels(tmp_path):

    path = audio_file(tmp_path / "pinned.wav", seconds=35)
    audio, config = model_result(path)
    apply_metadata_tags(path, audio.desired)
    tags = WAVE(path)
    assert tags.tags is not None
    tags.tags.add(TXXX(encoding=3, desc="BEATPORT_TRACK_ID", text=["123"]))
    tags.save()
    provider = Provider(
        (
            replace(RELEASE, duration_seconds=35, genres=("Melodic House & Techno",)),
            replace(RELEASE, track_id="456", duration_seconds=35, genres=("Dance / Pop",)),
        )
    )
    loader = EnrichmentLoader(
        lambda *_a: pytest.fail("Reuse current audio"),
        provider=provider,
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    plan = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert plan.target_file_genre == ("Melodic House & Techno",)
    raw = plan.desired["SETTAG_BEATPORT"]
    assert raw
    evidence = json.loads(raw[0])
    assert [source["track_id"] for source in evidence["sources"]] == ["123"]


@pytest.mark.parametrize(
    "release",
    [
        replace(RELEASE, title="Different Track"),
        replace(RELEASE, mix="Different Remix"),
        replace(RELEASE, detail_page=False),
    ],
)
def test_unverified_release_never_replaces_existing_genre(tmp_path, release):
    path = audio_file(tmp_path / "unverified.wav", seconds=35)
    audio, config = model_result(path)
    apply_metadata_tags(path, audio.desired)
    loader = EnrichmentLoader(
        lambda *_a: pytest.fail("Reuse current audio"),
        provider=Provider((replace(release, duration_seconds=35),), details=[]),
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    plan = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert plan.target_file_genre is None
    assert plan.file_genre == ("Progressive House",)
