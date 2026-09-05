"""Versioned completeness of the whole enrichment, independent of audio provenance."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from typing import Any, Literal

ENRICHMENT_SCHEMA = "settag.enrichment/v2"
CATALOG_TTL_SECONDS = 7 * 24 * 3600
EnrichmentStatus = Literal["current", "needs_enrichment", "partial"]


def enrichment_record(owned: Mapping[str, list[str] | None]) -> dict[str, Any] | None:
    raw = owned.get("SETTAG_ENRICHMENT")
    try:
        value = json.loads(raw[0]) if raw else None
        if isinstance(value, dict) and value.get("schema") == ENRICHMENT_SCHEMA:
            return value
    except (ValueError, TypeError):
        pass
    return None


def catalog_evidence(owned: Mapping[str, list[str] | None]) -> dict[str, Any] | None:
    """Parse retained evidence defensively; freshness is checked separately."""
    raw = owned.get("SETTAG_BEATPORT")
    try:
        value = json.loads(raw[0]) if raw else None
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("schema") != "settag.beatport/v1":
        return None

    def labels(values: object) -> bool:
        return isinstance(values, list) and all(isinstance(v, str) and v.strip() for v in values)

    if value.get("status") not in ("consensus", "conflicting_genres"):
        return None
    if not all(labels(value.get(key)) for key in ("agreed_genres", "alternative_genres")):
        return None
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        return None
    if any(
        not isinstance(source, dict)
        or not isinstance(source.get("url"), str)
        or not source["url"].startswith("https://www.beatport.com/track/")
        or not labels(source.get("genres"))
        or not source["genres"]
        for source in sources
    ):
        return None
    if value["status"] == "consensus" and not value["agreed_genres"]:
        return None
    return value


def evidence_genres(catalog: Mapping[str, Any]) -> tuple[str, ...]:
    """All labels, in verified release order, without case-insensitive duplicates."""
    labels: dict[str, str] = {}
    for source in catalog["sources"]:
        for label in source["genres"]:
            clean = label.strip()
            labels.setdefault(clean.casefold(), clean)
    return tuple(labels.values())


def current_catalog_evidence(owned: Mapping[str, list[str] | None]) -> dict[str, Any] | None:
    record = enrichment_record(owned)
    if not catalog_current(owned) or record is None or record["catalog"]["status"] != "matched":
        return None
    return catalog_evidence(owned)


def catalog_current(
    owned: Mapping[str, list[str] | None],
    *,
    now: float | None = None,
    identity_sha256: str | None = None,
) -> bool:
    record = enrichment_record(owned)
    if record is None:
        return False
    catalog = record.get("catalog")
    if not isinstance(catalog, dict) or catalog.get("status") not in ("matched", "no_match"):
        return False
    if identity_sha256 is not None and catalog.get("identity_sha256") != identity_sha256:
        return False
    if catalog["status"] == "matched" and catalog_evidence(owned) is None:
        return False
    checked = catalog.get("checked_at")
    return (
        isinstance(checked, (int, float))
        and not isinstance(checked, bool)
        and math.isfinite(checked)
        and 0 <= (time.time() if now is None else now) - checked < CATALOG_TTL_SECONDS
    )


def enrichment_status(
    owned: Mapping[str, list[str] | None], *, audio_current: bool, now: float | None = None
) -> EnrichmentStatus:
    record = enrichment_record(owned)
    if record is None:
        return "needs_enrichment"
    catalog = record.get("catalog")
    if (
        record.get("audio") != "complete"
        or not isinstance(catalog, dict)
        or catalog.get("status") == "unavailable"
    ):
        return "partial"
    return "current" if audio_current and catalog_current(owned, now=now) else "needs_enrichment"


def record_values(*, audio_complete: bool, catalog: dict[str, Any]) -> list[str]:
    return [
        json.dumps(
            {
                "schema": ENRICHMENT_SCHEMA,
                "audio": "complete" if audio_complete else "unavailable",
                "catalog": catalog,
            },
            sort_keys=True,
        )
    ]


def enrichment_notes(owned: Mapping[str, list[str] | None]) -> tuple[str, ...]:
    record = enrichment_record(owned)
    if record is None:
        return (
            "Enrichment contract is missing or outdated; compatible audio evidence can be reused.",
        )
    notes: list[str] = []
    if record.get("audio") != "complete":
        notes.append("Audio analysis is incomplete; enrich again to retry.")
    catalog = record.get("catalog")
    if isinstance(catalog, dict) and catalog.get("status") == "unavailable":
        notes.append(f"Catalog check unavailable: {catalog.get('reason', 'retry enrichment')}")
    elif not catalog_current(owned):
        notes.append("Catalog check is expired or incomplete; enrich again to refresh it.")
    return tuple(notes)
