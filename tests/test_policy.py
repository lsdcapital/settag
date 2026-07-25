from settag.policy import (
    EVIDENCE_LIMIT,
    Prediction,
    collect_evidence,
    rank_predictions,
    select_predictions,
)


def test_rank_predictions_is_descending_and_deterministic() -> None:
    ranked = rank_predictions(["z", "b", "a"], [0.9, 0.5, 0.5])

    assert ranked == [
        Prediction("z", 0.9),
        Prediction("a", 0.5),
        Prediction("b", 0.5),
    ]


def test_selection_never_forces_a_prediction_below_threshold() -> None:
    selected = select_predictions(
        [Prediction("Electronic---House", 0.09)],
        threshold=0.10,
        top=5,
    )

    assert selected == []


def test_selection_applies_threshold_then_top_limit() -> None:
    predictions = [
        Prediction("a", 0.9),
        Prediction("b", 0.8),
        Prediction("c", 0.7),
    ]

    assert select_predictions(predictions, threshold=0.75, top=1) == [Prediction("a", 0.9)]


def test_evidence_is_ranked_bounded_and_never_score_filtered() -> None:
    predictions = [
        Prediction(f"label-{index:02}", index / 100) for index in range(EVIDENCE_LIMIT + 5)
    ]

    evidence = collect_evidence(predictions)

    # Expressed relative to the limit so raising it does not require editing expectations:
    # the five lowest-scoring predictions fall away and the rest survive in rank order.
    highest = EVIDENCE_LIMIT + 4
    assert len(evidence) == EVIDENCE_LIMIT
    assert evidence[0] == Prediction(f"label-{highest:02}", highest / 100)
    assert evidence[-1] == Prediction("label-05", 0.05)
