from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace

import pytest
from mutagen.id3 import TIT2
from test_enrichment import RELEASE, Provider, audio_file, enrichment_plan
from test_freshness import model_result

from settag.enrichment import EnrichmentLoader
from settag.freshness import CATALOG_TTL_SECONDS, EnrichmentState, record_values
from settag.plans import planned_write_from_record, planned_write_record
from settag.state import WorkbenchStore
from settag.tags import (
    apply_metadata_tags,
    owned_tag_store,
    read_owned_values,
    track_identity_sha256,
)
from settag.workflow import apply_prepared, inspect_track, preflight_plan


class NoLookup(Provider):
    def candidates(self, track):
        pytest.fail("Durable lookup history should avoid another request")


def setup_track(tmp_path):
    path = audio_file(tmp_path / "track.wav", seconds=35)
    audio, config = model_result(path)
    apply_metadata_tags(path, audio.desired)
    return path, config, WorkbenchStore(tmp_path / "local.sqlite3")


def loader(config, store, provider):
    return EnrichmentLoader(
        lambda *_args: pytest.fail("Compatible audio must not be analyzed again"),
        provider=provider,
        expected_model_ids={"genre": "test-model"},
        expected_config=config,
        state_store=store,
    )


def run(operation, path):
    batch = operation((path,), lambda *_args: None, lambda: False)
    assert not batch.failures
    return batch.planned[0]


def scan(path, config, store):
    return inspect_track(
        path,
        expected_model_ids={"genre": "test-model"},
        expected_config_sha256=config["sha256"],
        expected_config=config,
        state_store=store,
    )


def test_miss_survives_restart_plan_deletion_and_rename_without_tag_writes(tmp_path):
    path, config, store = setup_track(tmp_path)
    original = path.read_bytes()
    item = run(loader(config, store, Provider(())), path)
    assert item.enrichment["catalog"]["status"] == "no_match"
    assert item.owned_changes == ()
    assert item.desired["SETTAG_ENRICHMENT"] is None
    assert path.read_bytes() == original
    store.save(item)
    store.delete((path,))
    moved = path.rename(tmp_path / "renamed.wav")
    reopened = WorkbenchStore(store.path)
    assert scan(moved, config, reopened).enrichment_status == "current"
    repeated = run(loader(config, reopened, NoLookup()), moved)
    assert repeated.owned_changes == ()
    assert moved.read_bytes() == original


def test_match_survives_unwritten_plan_and_verified_write_without_enrichment_tag(tmp_path):
    path, config, store = setup_track(tmp_path)
    original = path.read_bytes()
    provider = Provider((replace(RELEASE, duration_seconds=35, genres=("House",)),))
    first = run(loader(config, store, provider), path)
    assert first.target_file_genre == ("House",)
    assert path.read_bytes() == original
    # No pending plan is required to recover the lookup or its evidence.
    repeated = run(loader(config, WorkbenchStore(store.path), NoLookup()), path)
    assert repeated.target_file_genre == ("House",)
    assert repeated.desired == first.desired
    restored = planned_write_from_record(planned_write_record(repeated))
    assert restored.enrichment == repeated.enrichment
    assert restored.desired["SETTAG_ENRICHMENT"] is None
    assert "Enrichment status: update" not in restored.owned_changes
    assert apply_prepared(preflight_plan((restored,))) == 1
    assert read_owned_values(path)["SETTAG_ENRICHMENT"] is None
    assert scan(path, config, store).enrichment_status == "current"
    assert not run(loader(config, store, NoLookup()), path).readable_changes


def test_legacy_tag_is_imported_and_preserved_while_local_miss_takes_precedence(tmp_path):
    path, config, store = setup_track(tmp_path)
    catalog, _ = enrichment_plan(
        path, Provider((replace(RELEASE, duration_seconds=35, genres=("House",)),))
    )
    assert catalog is not None
    identity = track_identity_sha256(owned_tag_store(path))
    legacy = record_values(
        audio_complete=True,
        catalog={
            "status": "matched",
            "identity_sha256": identity,
            "checked_at": time.time() - CATALOG_TTL_SECONDS - 1,
        },
    )
    apply_metadata_tags(path, {**catalog.desired, "SETTAG_ENRICHMENT": legacy})
    original = path.read_bytes()
    assert scan(path, config, store).enrichment.record == json.loads(legacy[0])
    assert path.read_bytes() == original
    refreshed = run(loader(config, store, Provider(())), path)
    assert refreshed.enrichment["catalog"]["status"] == "no_match"
    assert refreshed.target_file_genre is None
    assert refreshed.desired["SETTAG_ENRICHMENT"] == legacy
    assert not refreshed.readable_changes
    # A real genre write must preserve the old tag, not update or delete it.
    edited = replace(refreshed, target_file_genre=("Techno",), genre_edit_source="manual")
    assert apply_prepared(preflight_plan((edited,))) == 1
    assert read_owned_values(path)["SETTAG_ENRICHMENT"] == legacy
    current = scan(path, config, WorkbenchStore(store.path))
    assert current.enrichment.record["catalog"]["status"] == "no_match"
    assert current.enrichment_status == "current"
    assert not run(loader(config, store, NoLookup()), path).readable_changes


@pytest.mark.parametrize("change", ["identity", "audio"])
def test_changed_lookup_identity_or_audio_does_not_reuse_history(tmp_path, change):
    path, config, store = setup_track(tmp_path)
    run(loader(config, store, Provider(())), path)
    if change == "identity":
        tags = owned_tag_store(path)
        tags.audio.tags.add(TIT2(encoding=3, text=["Different track"]))
        tags.audio.save()
    else:
        # Change one PCM sample in this known WAV fixture, preserving its tags.
        raw = bytearray(path.read_bytes())
        raw[raw.index(b"data") + 8] ^= 1
        path.write_bytes(raw)
    metadata = scan(path, config, store)
    assert metadata.enrichment is None
    assert metadata.enrichment_status == "needs_enrichment"


def test_legacy_wrong_identity_is_not_imported(tmp_path):
    path, config, store = setup_track(tmp_path)
    legacy = record_values(
        audio_complete=True,
        catalog={"status": "no_match", "checked_at": time.time(), "identity_sha256": "wrong"},
    )
    apply_metadata_tags(path, {**read_owned_values(path), "SETTAG_ENRICHMENT": legacy})
    metadata = scan(path, config, store)
    assert metadata.enrichment is None
    assert not metadata.catalog_current


def test_legacy_working_plan_migrates_before_it_is_discarded(tmp_path):
    path, config, store = setup_track(tmp_path)
    plan, _ = model_result(path)
    identity = track_identity_sha256(owned_tag_store(path))
    legacy = record_values(
        audio_complete=True,
        catalog={"status": "no_match", "checked_at": time.time(), "identity_sha256": identity},
    )
    old = replace(
        plan,
        desired={**read_owned_values(path), "SETTAG_ENRICHMENT": legacy},
        owned_changes=("Enrichment status: update",),
    )
    store.save(old)
    # Emulate the previous database and compact plan schema.
    with sqlite3.connect(store.path) as connection:
        record = planned_write_record(old)
        record["schema"] = "settag.plan/v5"
        record.pop("enrichment")
        connection.execute("UPDATE workbench_plans SET plan_json = ?", (json.dumps(record),))
        connection.execute("PRAGMA user_version = 2")
    restored = store.load(
        (path,),
        expected_model_ids={"genre": "test-model"},
        expected_config_sha256=config["sha256"],
        expected_config=config,
    )[path].plan
    assert restored.desired["SETTAG_ENRICHMENT"] is None
    assert restored.enrichment == json.loads(legacy[0])
    assert not restored.readable_changes
    store.delete((path,))
    assert scan(path, config, store).enrichment_status == "current"
    assert run(loader(config, store, NoLookup()), path).enrichment == restored.enrichment


def test_local_failure_does_not_fall_back_to_a_legacy_success(tmp_path):
    path, config, store = setup_track(tmp_path)
    identity = track_identity_sha256(owned_tag_store(path))
    legacy = record_values(
        audio_complete=True,
        catalog={"status": "no_match", "checked_at": time.time(), "identity_sha256": identity},
    )
    apply_metadata_tags(path, {**read_owned_values(path), "SETTAG_ENRICHMENT": legacy})
    scan(path, config, store)
    failed = json.loads(record_values(audio_complete=True, catalog={"status": "unavailable"})[0])
    store.save_enrichment(path, identity, EnrichmentState(failed))
    assert scan(path, config, WorkbenchStore(store.path)).enrichment_status == "partial"


def test_failed_refresh_retains_unwritten_catalog_evidence_locally(tmp_path):
    path, config, store = setup_track(tmp_path)
    first = run(loader(config, store, Provider((replace(RELEASE, duration_seconds=35),))), path)
    record = {**first.enrichment, "catalog": dict(first.enrichment["catalog"])}
    record["catalog"]["checked_at"] = time.time() - CATALOG_TTL_SECONDS - 1
    store.save_enrichment(
        path,
        record["catalog"]["identity_sha256"],
        EnrichmentState(record, first.desired["SETTAG_BEATPORT"]),
    )

    class Unavailable(Provider):
        def candidates(self, track):
            raise RuntimeError("Catalog unavailable")

    failed = run(loader(config, store, Unavailable()), path)
    assert failed.enrichment_status == "partial"
    assert not failed.readable_changes
    metadata = scan(path, config, store)
    assert metadata.enrichment.evidence == first.desired["SETTAG_BEATPORT"]
    assert not metadata.catalog_current
    assert read_owned_values(path)["SETTAG_BEATPORT"] is None
