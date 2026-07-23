from settag.records import config_record


def test_config_hash_is_stable_and_sensitive_to_values() -> None:
    first = config_record(top=5, threshold=0.1)
    same = config_record(top=5, threshold=0.1)
    different = config_record(top=3, threshold=0.1)

    assert first["sha256"] == same["sha256"]
    assert first["sha256"] != different["sha256"]
