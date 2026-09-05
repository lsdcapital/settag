"""Read-only Beatport experiment: explicit identity checks, never tag writes.

The public-page provider is deliberately replaceable. HTTP challenges are a batch
stop, not a missing track. Nothing in this module infers genre from audio.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

CACHE_VERSION = 1
CACHE_SECONDS = 7 * 24 * 3600
MAX_PAGE_BYTES = 8 * 1024 * 1024


class LookupStopped(RuntimeError):
    """Access, response format, or request budget prevents reliable lookup."""


@dataclass(frozen=True)
class TrackIdentity:
    title: str
    artists: tuple[str, ...]
    duration_seconds: float | None
    isrc: str = ""
    beatport_id: str = ""


@dataclass(frozen=True)
class Candidate:
    track_id: str
    title: str
    artists: tuple[str, ...]
    mix: str
    duration_seconds: float | None
    genres: tuple[str, ...]
    isrc: str = ""
    detail_page: bool = False

    @property
    def url(self) -> str:
        return f"https://www.beatport.com/track/track/{self.track_id}"


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str
    candidate: Candidate | None = None


class LookupProvider(Protocol):
    def candidates(self, track: TrackIdentity) -> Sequence[Candidate]: ...


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def _artists(values: Sequence[str]) -> frozenset[str]:
    # Only presentation aliases, not arbitrary parenthetical text or remixer names.
    clean = (re.sub(r"\s*\((?:ofc|oz)\)", "", s, flags=re.IGNORECASE) for s in values)
    return frozenset(
        normalized(part)
        for value in clean
        for part in re.split(r"\s*(?:,|;|&|\band\b)\s*", value, flags=re.IGNORECASE)
        if normalized(part)
    )


def title_version(title: str, explicit_mix: str = "") -> tuple[str, str]:
    """Separate named versions without guessing away remix identities.

    Unmarked and Original Mix are equivalent only with matching duration; Extended,
    Radio, Club, named remixes, VIPs and edits remain distinct.
    """
    versions = []

    def section(match: re.Match[str]) -> str:
        value = match.group(1)
        if re.search(r"\b(mix|remix|edit|dub|rework|bootleg|vip|version)\b", value, re.IGNORECASE):
            versions.append(normalized(value))
            return " "
        return match.group(0)

    base = re.sub(r"[\[(]([^\])]+)[\])]", section, title)
    if explicit_mix:
        versions.append(normalized(explicit_mix))
    versions = [v for v in versions if v not in {"original", "original mix"}]
    return normalized(base), " / ".join(sorted(set(versions)))


def identity_conflicts(
    track: TrackIdentity, candidate: Candidate, *, require_duration: bool = True
) -> list[str]:
    reasons = []
    source_title, source_version = title_version(track.title)
    target_title, target_version = title_version(candidate.title, candidate.mix)
    if not source_title or source_title != target_title:
        reasons.append("title differs or is missing")
    if not _artists(track.artists) or _artists(track.artists) != _artists(candidate.artists):
        reasons.append("artists differ or are missing")
    if source_version != target_version:
        reasons.append("mix/version differs")
    if track.beatport_id and track.beatport_id != candidate.track_id:
        reasons.append("Beatport ID conflicts")
    if track.isrc and candidate.isrc and normalized(track.isrc) != normalized(candidate.isrc):
        reasons.append("ISRC conflicts")
    if require_duration:
        durations = (track.duration_seconds, candidate.duration_seconds)
        if any(d is None or not math.isfinite(d) or d <= 0 for d in durations):
            reasons.append("duration is missing or invalid")
        else:
            source_duration, target_duration = durations
            assert source_duration is not None
            assert target_duration is not None
            if abs(source_duration - target_duration) > 5:
                reasons.append("duration differs by more than 5 seconds")
    return reasons


def decide(track: TrackIdentity, candidates: Sequence[Candidate]) -> Decision:
    accepted = [c for c in candidates if not identity_conflicts(track, c) and c.genres]
    if len(accepted) == 1:
        if not accepted[0].detail_page:
            return Decision("unresolved", "Identity checks passed, but release page is unverified")
        return Decision(
            "proposed",
            "One candidate passed title, artist, version, duration and available ID checks; "
            "manual verification still required",
            accepted[0],
        )
    if len(accepted) > 1:
        return Decision("ambiguous", "Multiple releases passed identity checks; no genre chosen")
    if candidates:
        return Decision("unresolved", "No candidate passed every required identity check")
    return Decision("unresolved", "No candidates returned; this does not establish catalog absence")


class _PageData(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.reading = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.reading = dict(attrs).get("id") == "__NEXT_DATA__"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.reading = False

    def handle_data(self, data: str) -> None:
        if self.reading:
            self.chunks.append(data)


def _names(value: Any, key: str) -> tuple[str, ...]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(
        item[key].strip()
        for item in value
        if isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip()
    )


def _candidate(value: dict[str, Any], *, detail: bool = False) -> Candidate:
    search = not detail
    track_id = str(value.get("track_id", ""))
    title = value.get("track_name")
    if not track_id.isdigit() or not isinstance(title, str) or not title.strip():
        raise LookupStopped("Unrecognized Beatport track identity format")
    length = value.get("length" if search else "track_length_ms")
    duration = (
        float(length) / 1000
        if isinstance(length, (int, float)) and not isinstance(length, bool)
        else None
    )
    genres = _names(value.get("genre"), "genre_name" if search else "name")
    artists = value.get("artists")
    if isinstance(artists, list):
        # Search credits artists and remixers together, with an explicit role.
        # Named remix identity is checked separately through mix_name.
        artists = [
            a
            for a in artists
            if not isinstance(a, dict)
            or a.get("artist_type_name" if search else "type") != "Remixer"
        ]
    return Candidate(
        track_id=track_id,
        title=title,
        artists=_names(artists, "artist_name" if search else "name"),
        mix=str(value.get("mix_name") or ""),
        duration_seconds=duration,
        genres=genres,
        isrc=str(value.get("isrc") or ""),
        detail_page=not search,
    )


def parse_page(body: str, *, track_id: str = "") -> tuple[Candidate, ...]:
    """Read structured page data only; never interpret loose search snippets as tracks."""
    parser = _PageData()
    parser.feed(body)
    try:
        payload = json.loads("".join(parser.chunks))
        queries = payload["props"]["pageProps"]["dehydratedState"]["queries"]
    except (ValueError, KeyError, TypeError) as error:
        raise LookupStopped(
            "Beatport page format unavailable; no usable structured data"
        ) from error
    candidates: dict[str, Candidate] = {}
    recognized = False
    for query in queries:
        data = query.get("state", {}).get("data") if isinstance(query, dict) else None
        if not isinstance(data, dict):
            continue
        # Detail requests only accept the requested track, never recommendations.
        if track_id:
            if (
                query.get("queryKey") == [f"track-details-{track_id}"]
                and str(data.get("track_id")) == track_id
                and "mix_name" in data
            ):
                candidate = _candidate(data, detail=True)
                candidates[candidate.track_id] = candidate
                recognized = True
        elif (
            isinstance(query.get("queryKey"), list)
            and query["queryKey"][:1] == ["search-tracks"]
            and isinstance(data.get("data"), list)
        ):
            rows = data["data"]
            if rows and all(isinstance(r, dict) and "track_id" in r for r in rows):
                recognized = True
                for row in rows:
                    candidate = _candidate(row)
                    candidates[candidate.track_id] = candidate
            elif rows == []:
                recognized = True
    if not recognized:
        raise LookupStopped("Beatport response did not contain a recognized track result")
    return tuple(candidates.values())


class PublicPageProvider:
    """Bounded, single-threaded public lookup with local caching and no challenge bypass."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        offline: bool = False,
        max_requests: int = 60,
        fetch: Callable[[str], bytes] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_dir = cache_dir
        self.offline = offline
        self.max_requests = max_requests
        self.requests = 0
        self.cache_hits = 0
        self.oldest_observation: float | None = None
        self._fetch = fetch or self._http
        self._sleep = sleep

    @staticmethod
    def _http(url: str) -> bytes:
        request = Request(url, headers={"User-Agent": "SetTag/Beatport-read-only-prototype"})
        with urlopen(request, timeout=15) as response:
            if urlparse(response.url).hostname not in {"beatport.com", "www.beatport.com"}:
                raise LookupStopped("Beatport redirected outside its public website")
            body = response.read(MAX_PAGE_BYTES + 1)
            if len(body) > MAX_PAGE_BYTES:
                raise LookupStopped("Beatport response exceeds page size limit")
            return body

    def _observe(self, timestamp: float) -> None:
        self.oldest_observation = (
            timestamp
            if self.oldest_observation is None
            else min(self.oldest_observation, timestamp)
        )

    def _page(self, url: str, *, track_id: str = "") -> tuple[Candidate, ...]:
        cache_file = self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                age = time.time() - cached["fetched_at"]
                if cached["version"] == CACHE_VERSION and 0 <= age < CACHE_SECONDS:
                    result = parse_page(cached["body"], track_id=track_id)
                    self._observe(cached["fetched_at"])
                    self.cache_hits += 1
                    return result
            except (ValueError, KeyError, TypeError, LookupStopped):
                pass
        if self.offline:
            raise LookupStopped("Offline: no valid cached page available")
        for attempt in range(3):
            if self.requests >= self.max_requests:
                raise LookupStopped("Request budget exhausted")
            self._sleep(1.0 if attempt == 0 else 2.0**attempt)
            self.requests += 1
            try:
                body = self._fetch(url).decode("utf-8")
                try:
                    result = parse_page(body, track_id=track_id)
                except LookupStopped:
                    # Diagnostic evidence only; never replay a failed parse as a cache hit.
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    (self.cache_dir / "unparsed-page.html").write_text(body)
                    raise
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                observed = time.time()
                self._observe(observed)
                cache_file.write_text(
                    json.dumps({"version": CACHE_VERSION, "fetched_at": observed, "body": body})
                )
                return result
            except HTTPError as error:
                if error.code in {401, 403}:
                    raise LookupStopped(
                        f"Beatport access blocked (HTTP {error.code}); batch stopped"
                    ) from error
                if error.code == 429:
                    # Do not guess at a retry interval or keep issuing requests while limited.
                    raise LookupStopped("Beatport rate limited (HTTP 429); retry later") from error
                if error.code < 500 or attempt == 2:
                    raise LookupStopped(f"Beatport HTTP {error.code}; batch stopped") from error
            except (URLError, TimeoutError, UnicodeError) as error:
                raise LookupStopped(f"Beatport transport failed: {type(error).__name__}") from error
        raise LookupStopped("Beatport unavailable after bounded retries")

    def details(self, candidate: Candidate) -> Sequence[Candidate]:
        """Fetch only the exact release, excluding recommendations."""
        return self._page(candidate.url, track_id=candidate.track_id)

    def candidates(self, track: TrackIdentity) -> Sequence[Candidate]:
        if track.beatport_id:
            if not track.beatport_id.isdigit():
                raise LookupStopped("Invalid local Beatport ID")
            return self._page(
                f"https://www.beatport.com/track/track/{track.beatport_id}",
                track_id=track.beatport_id,
            )
        text_query = " ".join((*track.artists, track.title))
        queries = list(dict.fromkeys(q for q in (track.isrc, text_query) if q))
        results: tuple[Candidate, ...] = ()
        for query in queries:
            results = self._page(
                "https://www.beatport.com/search/tracks?" + urlencode({"q": query})
            )
            plausible = [
                c for c in results if not identity_conflicts(track, c, require_duration=False)
            ]
            if len(plausible) == 1:
                # Read the exact release page before proposing its genre.
                return self._page(plausible[0].url, track_id=plausible[0].track_id)
            if len(plausible) > 1:
                return plausible
        return results
