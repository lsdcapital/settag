"""Source attribution, recommendations and write descriptions shared by review views."""

from __future__ import annotations

from dataclasses import dataclass

from settag.freshness import (
    catalog_current,
    catalog_evidence,
    current_catalog_evidence,
    enrichment_notes,
    enrichment_record,
    evidence_genres,
)
from settag.plans import PlannedWrite, genre_suggestion
from settag.policy import Prediction
from settag.tags import OwnedValues, read_task_provenance


@dataclass(frozen=True)
class StoredEvidence:
    """Read-only evidence for the library inspector; never a write plan."""

    desired: OwnedValues
    file_genre: tuple[str, ...]
    selected: tuple[Prediction, ...] = ()
    notices: tuple[str, ...] = ()
    owned_changes: tuple[str, ...] = ()
    target_file_genre: None = None
    standard_genre_change: None = None
    genre_edit_source: None = None

    @property
    def evidence_view(self) -> OwnedValues:
        return self.desired


@dataclass(frozen=True)
class EvidenceReview:
    recommendation: str
    recommendation_source: str
    current_genre: str
    catalog_title: str
    catalog_details: tuple[str, ...]
    model_details: tuple[str, ...]
    changes: tuple[str, ...]
    notices: tuple[str, ...]


def genre_outcome(plan: PlannedWrite, *, included: bool = True) -> str:
    """Show the actual end state of this batch, never an unstaged suggestion."""
    current = ", ".join(plan.file_genre) or "None"
    target = plan.target_file_genre if included else None
    end = current if target is None else ", ".join(target) or "None"
    suffix = " (unchanged)" if end == current else ""
    return f"Genre: {current} → {end}{suffix}"


def describe_evidence(plan: PlannedWrite | StoredEvidence) -> EvidenceReview:
    evidence = plan.evidence_view
    current = ", ".join(plan.file_genre) or "None"
    raw = plan.desired.get("SETTAG_BEATPORT")
    catalog = catalog_evidence(plan.desired)
    active = current_catalog_evidence(evidence)
    fresh = catalog_current(evidence)
    record = enrichment_record(evidence)
    check = record.get("catalog", {}) if record else {}
    if not isinstance(check, dict):
        check = {}
    details: list[str] = []
    supported = False
    multiple = False
    if raw and catalog is None:
        title = "Beatport · unreadable evidence"
        details.append("Stored catalog evidence is unreadable; enrich again.")
    elif check.get("status") == "no_match":
        title = "Beatport · no verified match" if fresh else "Beatport · previous lookup expired"
        details.append("No catalog genre recommendation is available.")
        if catalog:
            details.append(
                "Earlier catalog evidence is retained but does not support this recommendation."
            )
    elif catalog:
        genres = evidence_genres(catalog)
        supported = {g.casefold() for g in genres} == {g.casefold() for g in plan.file_genre}
        multiple = len(genres) > 1
        title = (
            "Beatport · verified track match"
            if fresh and check.get("status") == "matched"
            else "Beatport · stored evidence; refresh needed"
        )
        if multiple:
            details.append("Multiple Beatport genres; all verified release labels are retained.")
        if genres:
            details.append("Catalog genre: " + ", ".join(genres))
        if supported and active:
            details.append("Supports keeping your existing genre.")
        elif genres and plan.file_genre:
            details.append("Verified Beatport genres take priority over the existing file tag.")
        for source in catalog.get("sources", []):
            # Keep each release's labels beside its URL, especially for disagreements.
            labels = ", ".join(source.get("genres", []))
            details.append(f"{labels} · {source['url']}" if labels else source["url"])
    elif check.get("status") == "unavailable":
        title = "Beatport · unavailable"
        details.append(str(check.get("reason", "Enrich again to retry.")))
    else:
        title = "Beatport · not checked"
        details.append("No catalog evidence is available.")

    suggestion = genre_suggestion(evidence, plan.selected, plan.file_genre)
    if plan.standard_genre_change is not None:
        target = ", ".join(plan.target_file_genre or ())
        field = "genres" if len(plan.target_file_genre or ()) > 1 else "genre"
        recommendation = f"Set {field} to {target} (staged)" if target else "Remove genre (staged)"
    elif plan.genre_edit_source == "manual":
        recommendation = f"Keep {current} (your choice)"
    elif active and not supported:
        recommendation = f"Use Beatport genres: {suggestion}"
    elif supported and active:
        recommendation = f"Keep {current}"
    elif suggestion:
        if any(g.casefold() == suggestion.casefold() for g in plan.file_genre):
            recommendation = f"Keep {current}"
        else:
            recommendation = f"Review {suggestion}; existing genre stays unchanged"
    else:
        recommendation = (
            f"Keep {current} pending source review"
            if plan.file_genre
            else "Leave genre blank; no supported suggestion"
        )

    if plan.genre_edit_source == "manual" or (
        plan.standard_genre_change is not None and plan.genre_edit_source is None
    ):
        basis = "Your staged genre choice"
    elif active:
        basis = "Beatport verified matches"
    elif suggestion:
        basis = "Audio model; no current Beatport genre recommendation"
    else:
        basis = "Existing file tag; no supported genre suggestion"

    provenance = read_task_provenance(plan.desired)
    model = provenance.get("genre", {}).get("model", {})
    model_details: list[str] = []
    if record and record.get("audio") != "complete":
        model_details.append("Audio analysis incomplete; retained predictions need refreshing.")
    if isinstance(model, dict):
        if model.get("id"):
            model_details.append(f"Genre model: {model['id']}")
        if (
            model.get("vocabulary") == "discogs519"
            or model.get("id") == "essentia/genre-discogs519-maest/v1"
        ) and any(
            g.casefold() == "melodic house & techno"
            for g in (*plan.file_genre, *(evidence_genres(catalog) if catalog else ()))
        ):
            model_details.append('This genre model cannot predict "Melodic House & Techno".')
    model_details.append("Ranked predictions; these do not replace your file genre automatically.")

    changes: list[str] = []
    prefixes = tuple(change.split(":", 1)[0] for change in plan.owned_changes)
    if "Beatport genre evidence" in prefixes:
        changes.append("Save Beatport catalog evidence and source links")
    if "Enrichment status" in prefixes:
        changes.append("Save enrichment status and catalog check date")
    prediction_fields = {
        "Genre labels",
        "Ranked score data",
        "Mood/theme labels",
        "Mood/theme ranked score data",
        "Instrument labels",
        "Instrument ranked score data",
    }
    if prediction_fields.intersection(prefixes):
        changes.append("Save audio predictions and analysis provenance")
    elif any(
        p in prefixes
        for p in ("Analysis time", "Task provenance", "Analysis model", "Evidence configuration")
    ):
        changes.append("Refresh audio analysis provenance")
    if "SetTag version" in prefixes:
        changes.append("Update SetTag writer version")
    if plan.standard_genre_change is not None:
        changes.append(f"Genre: {current} → {', '.join(plan.target_file_genre or ()) or 'None'}")
    elif changes:
        changes.append("Genre stays unchanged")
    if not changes:
        changes.append("Nothing to save")
    return EvidenceReview(
        recommendation,
        basis,
        current,
        title,
        tuple(details),
        tuple(model_details),
        tuple(changes),
        tuple(dict.fromkeys((*plan.notices, *enrichment_notes(evidence)))),
    )
