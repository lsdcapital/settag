from __future__ import annotations

import json
from dataclasses import replace
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

from settag.beatport import (
    Candidate,
    LookupStopped,
    PublicPageProvider,
    TrackIdentity,
    decide,
    identity_conflicts,
    parse_page,
)

SOURCE = TrackIdentity("Opus", ("Eric Prydz",), 543.0)
RELEASE = Candidate(
    "123",
    "Opus",
    ("Eric Prydz",),
    "Original Mix",
    543.0,
    ("Progressive House",),
    detail_page=True,
)


def page(data: object, key: str = "search-tracks") -> str:
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {"queries": [{"queryKey": [key], "state": {"data": data}}]}
            }
        }
    }
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"


def search_page() -> str:
    return page(
        {
            "data": [
                {
                    "track_id": 123,
                    "track_name": "Opus",
                    "mix_name": "Original Mix",
                    "artists": [{"artist_name": "Eric Prydz"}],
                    "length": 543000,
                    "genre": [{"genre_name": "Progressive House"}],
                }
            ]
        }
    )


def detail_page(track_id: int = 123) -> str:
    return page(
        {
            "track_id": track_id,
            "track_name": "Opus",
            "mix_name": "Original Mix",
            "artists": [{"name": "Eric Prydz"}],
            "track_length_ms": 543000,
            "genre": {"name": "Progressive House"},
        },
        key=f"track-details-{track_id}",
    )


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (replace(RELEASE, artists=("Unrelated Artist",)), "artists differ"),
        (replace(RELEASE, mix="Four Tet Remix"), "mix/version differs"),
        (replace(RELEASE, mix="Extended Mix"), "mix/version differs"),
        (replace(RELEASE, title="Opus Two"), "title differs"),
        (replace(RELEASE, duration_seconds=550), "duration differs"),
        (replace(RELEASE, duration_seconds=None), "duration is missing"),
        (replace(RELEASE, duration_seconds=float("nan")), "duration is missing"),
    ],
)
def test_wrong_identity_is_rejected(candidate: Candidate, reason: str) -> None:
    assert any(reason in r for r in identity_conflicts(SOURCE, candidate))
    assert decide(SOURCE, [candidate]).candidate is None


def test_no_substring_title_matching() -> None:
    assert decide(replace(SOURCE, title="Art"), [replace(RELEASE, title="Party")]).candidate is None


def test_named_remixer_conflict_is_a_hard_rejection() -> None:
    track = replace(SOURCE, title="Opus (Four Tet Remix)")
    assert decide(track, [replace(RELEASE, mix="Four Tet Remix")]).status == "proposed"
    assert decide(track, [replace(RELEASE, mix="Someone Else Remix")]).status == "unresolved"


def test_unicode_and_artist_order_are_presentation_differences() -> None:
    track = TrackIdentity("Éternity (Extended Mix)", ("Anyma (ofc), Chris Avantgarde",), 320)
    candidate = replace(
        RELEASE,
        title="Eternity",
        artists=("Chris Avantgarde", "Anyma"),
        mix="Extended Mix",
        duration_seconds=320,
    )
    assert not identity_conflicts(track, candidate)


def test_conflicting_ids_do_not_override_other_checks() -> None:
    assert identity_conflicts(replace(SOURCE, beatport_id="999"), RELEASE) == [
        "Beatport ID conflicts"
    ]
    assert identity_conflicts(
        replace(SOURCE, isrc="USAAA0000001"), replace(RELEASE, isrc="USAAA0000002")
    ) == ["ISRC conflicts"]


def test_ambiguous_releases_are_not_ranked_arbitrarily() -> None:
    assert decide(SOURCE, [RELEASE, replace(RELEASE, track_id="124")]).status == "ambiguous"


def test_search_alone_cannot_propose_a_genre() -> None:
    assert decide(SOURCE, [replace(RELEASE, detail_page=False)]).status == "unresolved"


def test_structured_parser_preserves_genre_and_duration() -> None:
    assert parse_page(detail_page(), track_id="123") == (RELEASE,)
    assert parse_page(search_page()) == (replace(RELEASE, detail_page=False),)


def test_search_roles_separate_remixers_from_primary_artists() -> None:
    body = page(
        {
            "data": [
                {
                    "track_id": 11649874,
                    "track_name": "Return to Oz",
                    "mix_name": "ARTBAT Remix",
                    "artists": [
                        {"artist_name": "Monolink", "artist_type_name": "Artist"},
                        {"artist_name": "ARTBAT", "artist_type_name": "Remixer"},
                    ],
                    "length": 480000,
                    "genre": [{"genre_name": "Melodic House & Techno"}],
                }
            ]
        }
    )
    candidate = parse_page(body)[0]
    assert candidate.artists == ("Monolink",)
    assert not identity_conflicts(
        TrackIdentity("Return to Oz (ARTBAT Remix)", ("Monolink",), 480), candidate
    )
    assert identity_conflicts(
        TrackIdentity("Return to Oz (Other Remix)", ("Monolink",), 480), candidate
    ) == ["mix/version differs"]


def test_same_isrc_does_not_resolve_conflicting_release_genres() -> None:
    track = replace(SOURCE, isrc="USAAA0000001")
    candidates = [
        replace(RELEASE, isrc=track.isrc),
        replace(RELEASE, track_id="124", isrc=track.isrc, genres=("Dance / Pop",)),
    ]
    assert decide(track, candidates).status == "ambiguous"


def test_detail_parser_never_substitutes_another_track() -> None:
    with pytest.raises(LookupStopped, match="recognized track"):
        parse_page(detail_page(999), track_id="123")


def test_page_format_failure_is_not_no_results() -> None:
    with pytest.raises(LookupStopped, match="page format"):
        parse_page("<html>Just a moment</html>")
    assert parse_page(page({"count": 0, "data": []})) == ()
    assert parse_page(page({"data": []})) == ()
    with pytest.raises(LookupStopped):
        parse_page(page({"data": []}, key="unrelated-search"))
    with pytest.raises(LookupStopped):
        parse_page(page({"unexpected": []}))


def test_provider_reads_detail_and_reuses_cache_offline(tmp_path: Path) -> None:
    requests = []

    def fetch(url: str) -> bytes:
        requests.append(url)
        return (search_page() if "/search/" in url else detail_page()).encode()

    provider = PublicPageProvider(tmp_path, fetch=fetch, sleep=lambda _: None)
    assert decide(SOURCE, provider.candidates(SOURCE)).candidate == RELEASE
    assert len(requests) == 2
    offline = PublicPageProvider(
        tmp_path, offline=True, fetch=lambda _: pytest.fail("Network used")
    )
    assert decide(SOURCE, offline.candidates(SOURCE)).candidate == RELEASE
    assert offline.requests == 0
    assert offline.cache_hits == 2


def test_stored_beatport_id_uses_only_the_requested_detail_page(tmp_path: Path) -> None:
    urls = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return detail_page().encode()

    provider = PublicPageProvider(tmp_path, fetch=fetch, sleep=lambda _: None)
    track = replace(SOURCE, beatport_id="123")
    assert decide(track, provider.candidates(track)).candidate == RELEASE
    assert urls == [RELEASE.url]


def test_empty_isrc_search_falls_back_to_title(tmp_path: Path) -> None:
    urls = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        if "q=USAAA0000001" in url:
            return page({"data": []}).encode()
        return (search_page() if "/search/" in url else detail_page()).encode()

    provider = PublicPageProvider(tmp_path, fetch=fetch, sleep=lambda _: None)
    assert (
        decide(SOURCE, provider.candidates(replace(SOURCE, isrc="USAAA0000001"))).candidate
        == RELEASE
    )
    assert len(urls) == 3


@pytest.mark.parametrize("status", [401, 403, 429])
def test_access_failures_stop_without_retry_or_negative_cache(tmp_path: Path, status: int) -> None:
    def fetch(url: str) -> bytes:
        raise HTTPError(url, status, "Unavailable", Message(), None)

    provider = PublicPageProvider(tmp_path, fetch=fetch, sleep=lambda _: None)
    with pytest.raises(LookupStopped):
        provider.candidates(SOURCE)
    assert provider.requests == 1
    assert not list(tmp_path.iterdir())


def test_server_errors_have_bounded_retries(tmp_path: Path) -> None:
    def fetch(url: str) -> bytes:
        raise HTTPError(url, 503, "Unavailable", Message(), None)

    provider = PublicPageProvider(tmp_path, fetch=fetch, sleep=lambda _: None)
    with pytest.raises(LookupStopped, match="HTTP 503"):
        provider.candidates(SOURCE)
    assert provider.requests == 3


def test_request_budget_counts_retries(tmp_path: Path) -> None:
    def fetch(url: str) -> bytes:
        raise HTTPError(url, 503, "Unavailable", Message(), None)

    provider = PublicPageProvider(tmp_path, max_requests=1, fetch=fetch, sleep=lambda _: None)
    with pytest.raises(LookupStopped, match="budget"):
        provider.candidates(SOURCE)
    assert provider.requests == 1
