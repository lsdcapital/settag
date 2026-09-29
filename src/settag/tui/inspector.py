"""The details panel text for one track, independent of Textual.

Built from a track entry and the review settings alone, so the panel can be
rendered and tested without an app.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from settag.freshness import enrichment_record, record_values
from settag.policy import Prediction
from settag.review_evidence import StoredEvidence, describe_evidence
from settag.tags import OwnedValues, read_task_provenance, task_evidence_from_owned
from settag.tasks import AnalysisTask
from settag.tui.entries import TASK_LABELS, TrackEntry, latest_analyzed_at, suggested_label
from settag.tui.review import review_track
from settag.tui.table import RowContext


def display_path(path: Path) -> str:
    """Keep paths recognizable without repeating the full home directory."""
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~" if relative == Path(".") else str(Path("~") / relative)


def metadata_inspector(
    entry: TrackEntry, *, selected_for_enrichment: bool, context: RowContext
) -> list[str]:
    """The Library details panel for one track."""
    identity = ["", entry.path.name, display_path(entry.path.parent)]
    lines: list[str] = []
    if entry.metadata_error is not None:
        return [
            *lines,
            "Metadata could not be read",
            f"  {entry.metadata_error.description}",
            "",
            "This track cannot be analyzed safely until its metadata is readable.",
            *identity,
        ]

    assert entry.metadata is not None
    metadata = entry.metadata
    genre = ", ".join(metadata.genre_state.standard) or "None"
    evidence_owned = entry.plan.desired if entry.plan is not None else metadata.owned
    if entry.plan is not None:
        review = describe_evidence(entry.plan)
    else:
        display_owned = dict(metadata.evidence_view)
        # Identity validation applies to display as well as lookup reuse.
        record = enrichment_record(display_owned)
        if (
            record
            and isinstance(record.get("catalog"), dict)
            and record["catalog"].get("status") in ("matched", "no_match")
            and not metadata.catalog_current
        ):
            display_owned["SETTAG_ENRICHMENT"] = record_values(
                audio_complete=record.get("audio") == "complete",
                catalog={"status": "unavailable", "reason": "Catalog check needs refreshing"},
            )
        selected = (
            tuple(context.select_for_review(metadata.stored_predictions))
            if metadata.status == "current"
            else ()
        )
        review = describe_evidence(
            StoredEvidence(display_owned, metadata.genre_state.standard, selected)
        )
    state = entry.plan.enrichment_status if entry.plan is not None else metadata.enrichment_status
    lines.extend(
        [
            f"Recommendation: {review.recommendation}",
            f"Based on: {review.recommendation_source}",
            f"Current file tag: {genre}",
            "",
            review.catalog_title,
            *(f"  {detail}" for detail in review.catalog_details),
            "",
            f"Enrichment: {state.replace('_', ' ').capitalize()}",
            *review.notices,
            "",
            "Audio models · predictions",
            *review.model_details,
            f"Audio last analyzed: {_full_analyzed_at(entry, context.tasks)}",
            f"Candidates · {_candidate_policy(context)}",
        ]
    )
    lines.extend(
        _task_candidate_sections(
            evidence_owned,
            context,
            fallback_genre=metadata.stored_predictions,
        )
    )

    lines.extend(
        [
            "",
            *(
                [
                    "Last enrichment attempt failed",
                    f"  {entry.analysis_error.description}",
                    "",
                ]
                if entry.analysis_error is not None
                else []
            ),
            (
                "Selected for enrichment."
                if selected_for_enrichment
                else "Not selected for enrichment."
            ),
            *(["Press V to review this saved result."] if entry.plan is not None else []),
            "Viewing evidence does not run enrichment or write tags.",
            *identity,
        ]
    )
    return lines


def _full_analyzed_at(entry: TrackEntry, tasks: Sequence[AnalysisTask]) -> str:
    if entry.plan is not None:
        return latest_analyzed_at(entry.plan.desired, tasks) or "Never"
    if entry.metadata is not None and entry.metadata.cached_plan is not None:
        return latest_analyzed_at(entry.metadata.cached_plan.desired, tasks) or "Never"
    if entry.metadata is not None:
        return entry.metadata.analyzed_at or "Never"
    return "Never"


def review_inspector(
    entry: TrackEntry, index: int, *, checked: bool, context: RowContext
) -> list[str]:
    """The Review details panel for one track."""
    identity = ["", entry.path.name, display_path(entry.path.parent)]
    lines: list[str] = []
    if entry.analysis_error is not None:
        return [
            *lines,
            "Enrichment failed",
            f"  {entry.analysis_error.description}",
            "",
            "Press Space to dismiss it so the other tracks can be written,",
            "or return to the library with B to retry it.",
            *identity,
        ]
    if entry.plan is None:
        return ["No enrichment result is available.", *identity]

    review = review_track(index, entry, checked, context)

    def describe(node, depth=0):
        result = ["  " * depth + node.label]
        for child in node.children:
            result.extend(describe(child, depth + 1))
        return result

    for section in review.children:
        lines.extend(describe(section))
        lines.append("")
    lines.extend(identity)
    return lines


def _task_candidate_sections(
    owned: OwnedValues,
    context: RowContext,
    *,
    fallback_genre: Sequence[Prediction] = (),
) -> list[str]:
    evidence_by_task = task_evidence_from_owned(owned)
    provenance = read_task_provenance(owned)
    lines: list[str] = []
    for task in context.tasks:
        evidence = evidence_by_task.get(task, ())
        if not evidence and task == "genre":
            evidence = fallback_genre
        if evidence:
            # ui-count: entries in this task's evidence list, shown only in this panel
            count = len(evidence)
            noun = "score" if count == 1 else "scores"
            lines.append(f"{TASK_LABELS[task]} · {count} {noun}")
            lines.append(
                _candidate_line(
                    context.select_for_review(evidence),
                )
            )
        elif task in provenance:
            lines.append(f"{TASK_LABELS[task]} · No ranked evidence")
        else:
            lines.append(f"{TASK_LABELS[task]} · Not analyzed")
    return lines


def _candidate_policy(context: RowContext) -> str:
    cutoff = f"{context.score_cutoff:.3f}".removesuffix("0")
    return f"cutoff ≥ {cutoff} · top {context.review_top}"


def _candidate_line(selected: Sequence[Prediction]) -> str:
    if selected:
        return "  " + " · ".join(
            f"{suggested_label((prediction,)) or prediction.label} {prediction.score:.3f}"
            for prediction in selected
        )
    return "  No candidate met the cutoff"
