from settag.records import config_record


def test_evidence_hash_is_stable_across_review_policy_changes() -> None:
    first = config_record(top=5, threshold=0.1)
    same = config_record(top=5, threshold=0.1)
    different_policy = config_record(top=3, threshold=0.8)

    assert first["sha256"] == same["sha256"]
    assert first["sha256"] == different_policy["sha256"]
    assert first["selection"] != different_policy["selection"]
