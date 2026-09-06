from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_beatport import SOURCE, detail_page, search_page
from test_enrichment import RELEASE, Provider, audio_file

from settag.beatport import LookupStopped, PublicPageProvider
from settag.enrichment import EnrichmentLoader
from settag.freshness import (
    CATALOG_TTL_SECONDS,
    ENRICHMENT_SCHEMA,
    EnrichmentState,
    catalog_current,
    enrichment_status,
    record_values,
)
from settag.policy import Prediction
from settag.tags import apply_metadata_tags, owned_tag_store, read_owned_values
from settag.tui.entries import TrackEntry
from settag.tui.table import RowContext, entry_analysis
from settag.workflow import inspect_track, planned_write_for_track, prepare_track

NOW = 2_000_000_000.0


def record(status="no_match", checked_at=NOW, *, audio=True):
    return {
        "SETTAG_ENRICHMENT": record_values(
            audio_complete=audio,
            catalog={"status": status, "checked_at": checked_at},
        )
    }


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0, True),
        (CATALOG_TTL_SECONDS - 1, True),
        (CATALOG_TTL_SECONDS, False),
        (CATALOG_TTL_SECONDS + 1, False),
        (-1, False),
    ],
)
def test_catalog_expiry_boundary(age, expected):
    owned = record(checked_at=NOW - age)
    assert catalog_current(owned, now=NOW) is expected
    assert enrichment_status(owned, audio_current=True, now=NOW) == (
        "current" if expected else "needs_enrichment"
    )


@pytest.mark.parametrize("checked", [None, True, "today", float("nan"), float("inf")])
def test_invalid_dates_are_not_current(checked):
    assert not catalog_current(record(checked_at=checked), now=NOW)


@pytest.mark.parametrize(
    "raw", [None, [], ["broken"], ["[]"], ["null"], ['{"schema":"settag.enrichment/v1"}']]
)
def test_missing_old_and_corrupt_contracts_require_enrichment(raw):
    assert (
        enrichment_status({"SETTAG_ENRICHMENT": raw}, audio_current=True, now=NOW)
        == "needs_enrichment"
    )


def test_partial_is_not_a_catalog_miss_or_current():
    assert enrichment_status(record("unavailable"), audio_current=True, now=NOW) == "partial"
    assert enrichment_status(record(audio=False), audio_current=True, now=NOW) == "partial"
    assert enrichment_status(record(), audio_current=False, now=NOW) == "needs_enrichment"
    assert ENRICHMENT_SCHEMA == "settag.enrichment/v2"


class Analyzer:
    spec = SimpleNamespace(id="test-model")

    def analyze(self, path):
        return [Prediction("Electronic---Progressive House", 0.9)]


def model_result(path):
    prepared = prepare_track(path, analyzer=Analyzer(), top=5, threshold=0.1)
    return planned_write_for_track(prepared), prepared.config


def scan(path, config):
    return inspect_track(
        path,
        expected_model_ids={"genre": "test-model"},
        expected_config_sha256=str(config["sha256"]),
        expected_config=config,
    )


def test_old_audio_is_reused_then_combined_result_expires(tmp_path):
    path = audio_file(tmp_path / "track.wav", seconds=35)
    old, config = model_result(path)
    apply_metadata_tags(path, old.desired)
    initial = scan(path, config)
    assert initial.status == "current"  # Audio alone stays valid.
    assert initial.enrichment_status == "needs_enrichment"
    assert initial.needs_analysis
    assert "Needs enrichment" in entry_analysis(
        TrackEntry(path, metadata=initial),
        RowContext(tasks=("genre",), review_top=5, score_cutoff=0.1),
    )
    before = read_owned_values(path)

    def unexpected(*_args):
        pytest.fail("The contract upgrade must not rerun valid audio analysis")

    provider = Provider((replace(RELEASE, duration_seconds=35),))
    loader = EnrichmentLoader(
        unexpected,
        provider=provider,
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    item = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert item.desired["SETTAG_GENRE_SCORES"] == before["SETTAG_GENRE_SCORES"]
    assert item.desired["SETTAG_PROVENANCE"] == before["SETTAG_PROVENANCE"]
    apply_metadata_tags(path, item.desired)
    assert scan(path, config).enrichment_status == "current"

    class NoLookup(Provider):
        def candidates(self, track):
            pytest.fail("A fresh completed catalog check must be reused")

    loader.provider = NoLookup()
    repeated = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert not repeated.owned_changes

    assert item.desired["SETTAG_ENRICHMENT"] is None
    assert item.enrichment is not None
    manifest = {**item.enrichment, "catalog": dict(item.enrichment["catalog"])}
    manifest["catalog"]["checked_at"] = time.time() - CATALOG_TTL_SECONDS - 1
    loader.state_store.save_enrichment(
        path,
        manifest["catalog"]["identity_sha256"],
        EnrichmentState(manifest, item.desired["SETTAG_BEATPORT"]),
    )
    assert scan(path, config).enrichment_status == "needs_enrichment"
    loader.provider = provider
    refreshed = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert refreshed.enrichment_status == "current"
    assert refreshed.desired["SETTAG_PROVENANCE"] == before["SETTAG_PROVENANCE"]


def test_pending_model_result_is_reused_for_contract_upgrade(tmp_path):
    path = audio_file(tmp_path / "track.wav", seconds=35)
    pending, config = model_result(path)

    def unexpected(*_args):
        pytest.fail("Saved, unapplied audio evidence must be retained")

    loader = EnrichmentLoader(
        unexpected,
        provider=Provider(()),
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
        cached_audio=lambda _path: pending,
    )
    item = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert item.enrichment_status == "current"
    assert item.desired["SETTAG_GENRE_SCORES"] == pending.desired["SETTAG_GENRE_SCORES"]


@pytest.mark.parametrize("status", [[], {}, None, "unexpected", "matched"])
def test_invalid_or_missing_catalog_evidence_cannot_claim_current(status):
    assert not catalog_current(record(status), now=NOW)


def test_expired_catalog_failure_is_partial_and_retry_recovers(tmp_path):
    path = audio_file(tmp_path / "track.wav", seconds=35)
    old, config = model_result(path)
    apply_metadata_tags(path, old.desired)

    def unexpected(*_args):
        pytest.fail("Catalog retry must retain current audio evidence")

    class Blocked(Provider):
        def candidates(self, track):
            raise LookupStopped("HTTP 403")

    loader = EnrichmentLoader(
        unexpected,
        provider=Blocked(),
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    partial = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert partial.enrichment_status == "partial"
    apply_metadata_tags(path, partial.desired)
    metadata = scan(path, config)
    assert metadata.enrichment_status == "partial"
    assert metadata.needs_analysis
    assert "Partial" in entry_analysis(
        TrackEntry(path, metadata=metadata),
        RowContext(tasks=("genre",), review_top=5, score_cutoff=0.1),
    )
    loader.provider = Provider(())
    loader.begin_batch()
    recovered = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert recovered.enrichment_status == "current"
    assert recovered.desired["SETTAG_GENRE_SCORES"] == old.desired["SETTAG_GENRE_SCORES"]


def test_cached_pages_retain_original_observation_time(tmp_path):
    def fetch(url):
        return (detail_page() if "/track/" in url else search_page()).encode()

    provider = PublicPageProvider(tmp_path, fetch=fetch, sleep=lambda _seconds: None)
    provider.candidates(SOURCE)
    first = provider.oldest_observation
    assert first is not None
    # Artificially age the successfully parsed pages without touching their content.
    for path in tmp_path.glob("*.json"):
        cached = json.loads(path.read_text())
        cached["fetched_at"] = first - 86400
        path.write_text(json.dumps(cached))

    def unexpected(_url):
        pytest.fail("Valid pages should be replayed from cache")

    replay = PublicPageProvider(tmp_path, fetch=unexpected)
    replay.candidates(SOURCE)
    assert replay.requests == 0
    assert replay.oldest_observation == first - 86400


def test_fresh_catalog_survives_repeated_audio_unavailability(tmp_path):
    path = audio_file(tmp_path / "track.wav", seconds=35)

    def unavailable(*_args):
        raise RuntimeError("Model unavailable")

    loader = EnrichmentLoader(
        unavailable, provider=Provider((replace(RELEASE, duration_seconds=35),))
    )
    partial = loader((path,), lambda *_a: None, lambda: False).planned[0]
    apply_metadata_tags(path, partial.desired)

    class NoLookup(Provider):
        def candidates(self, track):
            pytest.fail("Fresh catalog evidence should survive repeated audio failure")

    loader.provider = NoLookup()
    repeated = loader((path,), lambda *_a: None, lambda: False)
    assert not repeated.failures
    assert repeated.planned[0].enrichment_status == "partial"
    assert not repeated.planned[0].owned_changes


def test_missing_identity_is_partial_not_a_completed_catalog_miss(tmp_path):
    path = audio_file(tmp_path / "track.wav", seconds=35)
    old, config = model_result(path)
    apply_metadata_tags(path, old.desired)
    store = owned_tag_store(path)
    del store.audio.tags["TPE1"]
    store.audio.save()

    class NoLookup(Provider):
        def candidates(self, track):
            pytest.fail("Missing artist must not issue a query")

    def unexpected(*_args):
        pytest.fail("Audio evidence remains reusable")

    loader = EnrichmentLoader(
        unexpected,
        provider=NoLookup(),
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
    )
    item = loader((path,), lambda *_a: None, lambda: False).planned[0]
    assert item.enrichment_status == "partial"
    assert "Artist/title missing" in item.notices[0]
