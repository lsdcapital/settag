from __future__ import annotations

import json
import time
from dataclasses import replace
from unittest.mock import patch

import pytest
from mutagen.id3 import TIT2
from mutagen.wave import WAVE
from test_enrichment import RELEASE, Provider, audio_file
from test_freshness import model_result, scan
from test_review_evidence import example

from settag.enrichment import EnrichmentLoader
from settag.freshness import CATALOG_TTL_SECONDS, record_values
from settag.hashing import sha256_audio
from settag.plans import stage_default_file_genre, suggested_file_genre
from settag.policy import Prediction
from settag.review_evidence import describe_evidence
from settag.state import WorkbenchStore
from settag.tags import apply_metadata_tags, read_genre_state, read_owned_values
from settag.tui.entries import TrackEntry
from settag.tui.table import RowContext, genre_check
from settag.workflow import apply_prepared, apply_undo, preflight_plan, preflight_undo

CONTEXT = RowContext(tasks=("genre",), review_top=5, score_cutoff=0.1)


@pytest.mark.parametrize(
    ("status", "checked"),
    [
        ("matched", time.time() - CATALOG_TTL_SECONDS - 60),
        ("unavailable", time.time()),
        ("no_match", time.time()),
    ],
)
def test_retained_catalog_does_not_stage_or_endorse_a_genre(tmp_path, status, checked):
    plan = example(tmp_path)
    plan = replace(
        plan,
        file_genre=(),
        selected=(),
        desired={
            **plan.desired,
            "SETTAG_ENRICHMENT": record_values(
                audio_complete=True, catalog={"status": status, "checked_at": checked}
            ),
        },
    )
    assert suggested_file_genre(plan) is None
    assert stage_default_file_genre(plan).target_file_genre is None
    entry = TrackEntry(plan.path, plan=plan)
    assert genre_check(entry, CONTEXT).suggested_genre is None
    review = describe_evidence(replace(plan, file_genre=("Melodic House & Techno",)))
    assert not any("Supports keeping" in line for line in review.catalog_details)
    assert review.recommendation != "Keep Melodic House & Techno"


def test_no_match_uses_audio_fallback_even_when_old_catalog_evidence_is_retained(tmp_path):
    plan = example(tmp_path)
    plan = replace(
        plan,
        file_genre=(),
        selected=(Prediction("Electronic---Tech House", 0.8),),
        desired={
            **plan.desired,
            "SETTAG_ENRICHMENT": record_values(
                audio_complete=True, catalog={"status": "no_match", "checked_at": time.time()}
            ),
        },
    )
    assert suggested_file_genre(plan) == "Tech House"
    assert stage_default_file_genre(plan).target_file_genre == ("Tech House",)
    assert "no verified match" in describe_evidence(plan).catalog_title


def test_partial_result_with_old_audio_survives_reopening_and_is_writeable(tmp_path):
    path = audio_file(tmp_path / "track.wav", seconds=35)
    old, config = model_result(path)
    apply_metadata_tags(path, old.desired)

    def unavailable(*_args):
        raise RuntimeError("new model not installed")

    loader = EnrichmentLoader(
        unavailable,
        provider=Provider((replace(RELEASE, duration_seconds=35),)),
        expected_model_ids={"genre": "new-model"},
        expected_config=config,
    )
    partial = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert partial.enrichment_status == "partial"
    store = WorkbenchStore(tmp_path / "workbench.sqlite3")
    store.save(partial)
    restored = store.load(
        (path,),
        expected_model_ids={"genre": "new-model"},
        expected_config_sha256=str(config["sha256"]),
        expected_config=config,
    )[path]
    assert restored.status == "ready"
    assert restored.plan.enrichment_status == "partial"
    assert preflight_plan((restored.plan,))


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "consensus", "agreed_genres": [None], "sources": ["bad"]},
        {"status": "conflicting_genres", "alternative_genres": 12, "sources": True},
    ],
)
def test_corrupt_evidence_does_not_crash_review(tmp_path, payload):
    plan = example(tmp_path)
    plan = replace(plan, desired={**plan.desired, "SETTAG_BEATPORT": [json.dumps(payload)]})
    assert suggested_file_genre(plan) is None
    assert describe_evidence(plan).catalog_title == "Beatport · unreadable evidence"
    assert genre_check(TrackEntry(plan.path, plan=plan), CONTEXT).relation != "match"


def test_combined_enrichment_reopen_write_expire_retry_and_undo(tmp_path):

    path = audio_file(tmp_path / "lifecycle.wav", seconds=35)
    old, config = model_result(path)
    apply_metadata_tags(path, old.desired)
    original = path.read_bytes()
    original_tags = read_owned_values(path)
    original_genre = read_genre_state(path).standard
    audio_digest = sha256_audio(path)

    def unexpected(*_args):
        pytest.fail("Valid audio must be reused throughout catalog enrichment")

    loader = EnrichmentLoader(
        unexpected,
        provider=Provider((replace(RELEASE, duration_seconds=35),)),
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    pending = loader((path,), lambda *_a: None, lambda: False).planned[0]
    store = WorkbenchStore(tmp_path / "lifecycle.sqlite3")
    store.save(pending)
    restored = store.load(
        (path,),
        expected_model_ids={"genre": "test-model"},
        expected_config_sha256=str(config["sha256"]),
        expected_config=config,
    )[path]
    assert restored.status == "ready"
    assert (
        "Save Beatport catalog evidence and source links"
        in describe_evidence(restored.plan).changes
    )
    assert path.read_bytes() == original
    writes = []
    assert apply_prepared(preflight_plan((restored.plan,)), on_write=writes.append) == 1
    assert read_owned_values(path) == pending.desired
    assert read_genre_state(path).standard == original_genre
    assert sha256_audio(path) == audio_digest
    assert scan(path, config).enrichment_status == "current"
    assert loader((path,), lambda *_a: None, lambda: False).planned[0].readable_changes == ()

    # Advance the freshness clock without touching the source file or defeating undo checks.

    future = time.time() + CATALOG_TTL_SECONDS + 60
    with patch("settag.freshness.time.time", return_value=future):
        assert scan(path, config).enrichment_status == "needs_enrichment"
        retry = loader((path,), lambda *_a: None, lambda: False).planned[0]
        assert retry.enrichment_status == "current"
        assert retry.desired["SETTAG_PROVENANCE"] == original_tags["SETTAG_PROVENANCE"]
    assert not preflight_undo(writes).blocked
    assert apply_undo(writes) == 1
    assert read_owned_values(path) == original_tags
    assert read_genre_state(path).standard == original_genre
    assert sha256_audio(path) == audio_digest


@pytest.mark.parametrize("matched", [True, False])
def test_identity_edit_invalidates_cached_match_or_miss(tmp_path, matched):

    path = audio_file(tmp_path / "identity.wav", seconds=35)
    old, config = model_result(path)
    apply_metadata_tags(path, old.desired)
    provider = Provider((replace(RELEASE, duration_seconds=35),) if matched else ())
    loader = EnrichmentLoader(
        lambda *_a: pytest.fail("Identity edits must not rerun current audio"),
        provider=provider,
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    plan = loader((path,), lambda *_a: None, lambda: False).planned[0]
    plan = replace(plan, source_audio_sha256=sha256_audio(path))
    store = WorkbenchStore(tmp_path / "identity.sqlite3")
    store.save(plan)
    apply_metadata_tags(path, plan.desired)
    assert scan(path, config).enrichment_status == "current"
    audio = WAVE(path)
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=["Different Track"]))
    audio.save()
    metadata = scan(path, config)
    assert metadata.status == "current"
    assert metadata.enrichment_status == "needs_enrichment"
    assert genre_check(TrackEntry(path, metadata=metadata), CONTEXT).summary != "Matches catalog"
    restored = store.load(
        (path,),
        expected_model_ids={"genre": "test-model"},
        expected_config_sha256=str(config["sha256"]),
        expected_config=config,
    )[path]
    assert restored.status == "stale"
    assert restored.reason == "catalog track identity changed"
    with pytest.raises(Exception, match="changed"):
        preflight_plan((plan,))
    refreshed = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert "no verified match" in describe_evidence(refreshed).catalog_title
    apply_metadata_tags(path, refreshed.desired)
    assert scan(path, config).enrichment_status == "current"
