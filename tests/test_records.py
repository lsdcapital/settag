import pytest

from settag.records import (
    ProvenanceStatus,
    config_record,
    configs_match_for_task,
    read_task_provenance_status,
)


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
