import json

import pytest

from settag.records import (
    ProvenanceStatus,
    config_record,
    configs_match_for_task,
    orphaned_tasks,
    read_task_provenance_status,
)
from settag.tags import OWNED_DESCRIPTIONS, PROVENANCE_SCHEMA


def _recorded(
    *,
    model_id: object = "model/v1",
    config_sha256: object = "config/current",
    analyzed_at: object = "2026-07-23T12:00:00Z",
) -> dict[str, object]:
    return {
        "model": {"id": model_id},
        "config": {"sha256": config_sha256},
        "analyzed_at": analyzed_at,
    }


def _status(recorded: object) -> ProvenanceStatus:
    return read_task_provenance_status(
        recorded,
        task="genre",
        expected_model_id="model/v1",
        expected_config_sha256="config/current",
    ).status


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        (_recorded(), ProvenanceStatus.CURRENT),
        (None, ProvenanceStatus.MISSING),
        ("not a record", ProvenanceStatus.UNREADABLE),
        (_recorded(model_id=None), ProvenanceStatus.UNREADABLE),
        (_recorded(model_id=""), ProvenanceStatus.UNREADABLE),
        (_recorded(config_sha256=None), ProvenanceStatus.UNREADABLE),
        (_recorded(analyzed_at=None), ProvenanceStatus.UNREADABLE),
        (_recorded(model_id="model/v2"), ProvenanceStatus.MODEL_CHANGED),
        (_recorded(config_sha256="config/older"), ProvenanceStatus.CONFIG_CHANGED),
    ],
)
def test_provenance_status_covers_every_way_a_task_can_be_out_of_date(
    recorded: object,
    expected: ProvenanceStatus,
) -> None:
    assert _status(recorded) == expected


def test_provenance_status_reports_the_model_before_the_config() -> None:
    """One reading, one reason: the caller shows a cause, not a list of them."""
    both = _recorded(model_id="model/v2", config_sha256="config/older")

    assert _status(both) == ProvenanceStatus.MODEL_CHANGED


def test_provenance_status_carries_the_timestamp_of_a_readable_record() -> None:
    """A stale record still dates the analysis it describes."""
    reading = read_task_provenance_status(
        _recorded(config_sha256="config/older"),
        task="genre",
        expected_model_id="model/v1",
        expected_config_sha256="config/current",
    )

    assert reading.status == ProvenanceStatus.CONFIG_CHANGED
    assert reading.analyzed_at == "2026-07-23T12:00:00Z"


def test_evidence_hash_is_stable_across_review_policy_changes() -> None:
    first = config_record(top=5, threshold=0.1)
    same = config_record(top=5, threshold=0.1)
    different_policy = config_record(top=3, threshold=0.8)

    assert first["sha256"] == same["sha256"]
    assert first["sha256"] == different_policy["sha256"]
    assert first["selection"] != different_policy["selection"]


def test_task_config_match_ignores_other_selected_tasks() -> None:
    combined = config_record(
        top=5,
        threshold=0.1,
        tasks=("genre", "mood-theme", "instrument"),
    )
    instrument_only = config_record(
        top=1,
        threshold=0.9,
        tasks=("instrument",),
    )

    assert configs_match_for_task(combined, instrument_only, "instrument")
    assert not configs_match_for_task(combined, instrument_only, "genre")


def test_evidence_hash_changes_with_the_audio_sample() -> None:
    """Sampling changes which audio the model read, so stored analyses must go stale."""
    full = config_record(top=5, threshold=0.1, genre_sample="full")
    middle = config_record(top=5, threshold=0.1, genre_sample="middle")

    assert full["sha256"] != middle["sha256"]
    evidence = middle["evidence"]
    assert isinstance(evidence, dict)
    assert evidence.get("genre_sample") == "middle"


def test_task_config_match_rejects_a_different_audio_sample() -> None:
    full = config_record(top=5, threshold=0.1, genre_sample="full")
    middle = config_record(top=3, threshold=0.8, genre_sample="middle")

    assert configs_match_for_task(full, full, "genre") is True
    assert configs_match_for_task(full, middle, "genre") is False


def _owned(
    *,
    labels: dict[str, list[str]] | None = None,
    recorded: list[str] | None = None,
    schema: str = PROVENANCE_SCHEMA,
) -> dict[str, list[str] | None]:
    """A file's owned values: some tasks with labels, some named in provenance."""
    owned: dict[str, list[str] | None] = {**dict.fromkeys(OWNED_DESCRIPTIONS), **(labels or {})}
    if recorded is not None:
        tasks = {
            task: {
                "model": {"id": f"model/{task}/v1"},
                "analyzed_at": "2026-07-23T12:00:00Z",
                "config": {"sha256": "abc123"},
            }
            for task in recorded
        }
        owned["SETTAG_PROVENANCE"] = [json.dumps({"schema": schema, "tasks": tasks})]
    return owned


def test_orphaned_tasks_finds_labels_the_file_cannot_attribute() -> None:
    """The state a schema bump leaves behind, which no configured-task loop can see."""
    owned = _owned(
        labels={"SETTAG_GENRE": ["Electronic---House"], "SETTAG_INSTRUMENT": ["Bass"]},
        recorded=["genre"],
    )

    assert orphaned_tasks(owned) == ("instrument",)


def test_orphaned_tasks_ignores_a_task_whose_record_is_intact() -> None:
    owned = _owned(
        labels={"SETTAG_GENRE": ["Electronic---House"], "SETTAG_INSTRUMENT": ["Bass"]},
        recorded=["genre", "instrument"],
    )

    assert orphaned_tasks(owned) == ()


def test_orphaned_tasks_ignores_a_task_the_file_never_carried() -> None:
    """Absent evidence is not orphaned evidence; only labels without a record count."""
    owned = _owned(labels={"SETTAG_GENRE": ["Electronic---House"]}, recorded=["genre"])

    assert orphaned_tasks(owned) == ()


def test_orphaned_tasks_reports_every_task_an_unreadable_record_stranded() -> None:
    """A bump discards the envelope whole, so it can strand more than one task at once."""
    owned = _owned(
        labels={"SETTAG_MOOD_THEME": ["deep"], "SETTAG_INSTRUMENT": ["Bass"]},
        recorded=["mood-theme", "instrument"],
        schema="settag.provenance/v2",
    )

    assert orphaned_tasks(owned) == ("mood-theme", "instrument")


def test_orphaned_tasks_defers_to_the_caller_on_a_task_it_already_checks() -> None:
    """A configured task with no record is MISSING, which is a better answer than this.

    The field-level rule reports a cause and dates the analysis; this only knows that
    labels have no record. Restating it here would downgrade a well-classified stale
    track to merely incomplete, so anything the caller asks about is left to it.
    """
    owned = _owned(labels={"SETTAG_GENRE": ["Electronic---House"]}, recorded=[])

    assert orphaned_tasks(owned) == ("genre",)
    assert orphaned_tasks(owned, checked=("genre",)) == ()
