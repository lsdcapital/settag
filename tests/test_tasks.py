import pytest

from settag.tasks import TASK_FIELDS, TASK_ORDER, parse_tasks, task_name


def test_mood_aggressive_is_ordered_after_instrument() -> None:
    assert TASK_ORDER == ("genre", "mood-theme", "instrument", "mood-aggressive")
    assert parse_tasks("mood-aggressive, genre,instrument") == (
        "genre",
        "instrument",
        "mood-aggressive",
    )
    assert task_name("mood-aggressive") == "mood-aggressive"


def test_mood_aggressive_fields_follow_the_task_field_pattern() -> None:
    assert TASK_FIELDS["mood-aggressive"] == (
        "SETTAG_MOOD_AGGRESSIVE",
        "SETTAG_MOOD_AGGRESSIVE_SCORES",
    )


def test_unknown_task_error_lists_mood_aggressive() -> None:
    with pytest.raises(ValueError, match="mood-aggressive"):
        parse_tasks("tempo")
