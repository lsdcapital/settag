from settag.records import config_record, configs_match_for_task


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
