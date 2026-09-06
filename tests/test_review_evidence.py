from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from test_enrichment import RELEASE, Provider, audio_file, enrichment_plan

from settag.freshness import record_values
from settag.review_evidence import describe_evidence, genre_outcome
from settag.tags import PROVENANCE_SCHEMA


def example(tmp_path):
    path = audio_file(tmp_path / "Sunrise.wav")
    release = replace(RELEASE, genres=("Melodic House & Techno",))
    item, _ = enrichment_plan(path, Provider((release,)))
    assert item is not None
    desired = {
        **item.desired,
        "SETTAG_ENRICHMENT": record_values(
            audio_complete=True, catalog={"status": "matched", "checked_at": time.time()}
        ),
        "SETTAG_PROVENANCE": [
            json.dumps(
                {
                    "schema": PROVENANCE_SCHEMA,
                    "tasks": {"genre": {"model": {"vocabulary": "discogs519"}}},
                }
            )
        ],
    }
    return replace(
        item,
        file_genre=("Melodic House & Techno",),
        desired=desired,
        owned_changes=("Beatport genre evidence: update", "Enrichment status: update"),
    )


def test_matching_sources_recommend_keep_and_only_show_actual_writes(tmp_path):
    review = describe_evidence(example(tmp_path))
    assert review.recommendation == "Keep Melodic House & Techno"
    assert review.catalog_title == "Beatport · verified track match"
    assert "Supports keeping your existing genre." in review.catalog_details
    assert 'This genre model cannot predict "Melodic House & Techno".' in review.model_details
    assert review.changes == (
        "Save Beatport catalog evidence and source links",
        "Save enrichment status and catalog check date",
        "Genre stays unchanged",
    )
    assert not any("audio predictions" in change for change in review.changes)


def test_multiple_labels_are_recommended_only_while_catalog_is_current(tmp_path):
    item = example(tmp_path)
    raw = item.desired["SETTAG_BEATPORT"]
    assert raw
    catalog = json.loads(raw[0])
    catalog.update(
        status="conflicting_genres",
        agreed_genres=[],
        alternative_genres=["Melodic House & Techno", "Dance / Pop"],
    )
    catalog["sources"].append({**catalog["sources"][0], "genres": ["Dance / Pop"]})
    split = replace(item, desired={**item.desired, "SETTAG_BEATPORT": [json.dumps(catalog)]})
    review = describe_evidence(split)
    assert review.recommendation == "Use Beatport genres: Melodic House & Techno, Dance / Pop"
    assert (
        "Multiple Beatport genres; all verified release labels are retained."
        in review.catalog_details
    )
    assert not any("Supports keeping" in line for line in review.catalog_details)
    stale = replace(item, desired={**item.desired, "SETTAG_ENRICHMENT": None})
    review = describe_evidence(stale)
    assert "pending source review" in review.recommendation
    assert "refresh needed" in review.catalog_title


def test_staged_choice_and_noop_are_explicit(tmp_path):
    item = example(tmp_path)
    staged = replace(item, target_file_genre=("Techno",))
    review = describe_evidence(staged)
    assert review.recommendation == "Set genre to Techno (staged)"
    assert review.changes[-1] == "Genre: Melodic House & Techno → Techno"
    assert describe_evidence(replace(item, owned_changes=())).changes == ("Nothing to save",)


@pytest.mark.parametrize(
    ("current", "target", "included", "expected"),
    [
        (("House",), ("Techno",), True, "Genre: House → Techno"),
        (("House",), ("Techno",), False, "Genre: House → House (unchanged)"),
        (("House",), None, True, "Genre: House → House (unchanged)"),
        (("House",), (), True, "Genre: House → None"),
        ((), ("Techno",), True, "Genre: None → Techno"),
        ((), None, True, "Genre: None → None (unchanged)"),
        (("House", "Disco"), ("House",), True, "Genre: House, Disco → House"),
    ],
)
def test_outcome_reflects_only_the_included_write(tmp_path, current, target, included, expected):
    item = replace(example(tmp_path), file_genre=current, target_file_genre=target)
    assert genre_outcome(item, included=included) == expected


@pytest.mark.parametrize(
    ("changes", "reviewable"),
    [
        (("Enrichment status: update",), False),
        (("Task provenance: update", "Analysis time: update", "SetTag version: update"), False),
        (("Enrichment status: update", "Ranked score data: update"), True),
        (("Beatport genre evidence: update",), True),
        (("Future metadata field: update",), True),
    ],
)
def test_bookkeeping_alone_does_not_need_a_reviewed_write(tmp_path, changes, reviewable):
    item = replace(example(tmp_path), owned_changes=changes, target_file_genre=None)
    assert item.needs_write_review is reviewable
    assert item.readable_changes == changes  # Explicit plans retain their exact writes.
    assert replace(item, target_file_genre=("Techno",)).needs_write_review
