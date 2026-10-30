"""The details panel text for one track, independent of Textual.

Built from a track entry and the review settings alone, so the panel can be
rendered and tested without an app.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from settag.freshness import enrichment_record, record_values
from settag.policy import Prediction
from settag.review_evidence import StoredEvidence, catalog_checked_on, describe_evidence
from settag.tags import OwnedValues, read_task_provenance, task_evidence_from_owned
from settag.tasks import AnalysisTask
from settag.tui.entries import (
    NEXT_STEP_LABELS,
    TASK_LABELS,
    TrackEntry,
    latest_analyzed_at,
    suggested_label,
)
from settag.tui.review import review_track
from settag.tui.table import (
    RowContext,
    entry_analysis,
    genre_check,
    reanalysis_tasks,
    task_names,
)

#: Separates the library panel's summary from the evidence behind it.
MORE_DETAIL = "── More detail ──"

#: The intensity head's two labels, in the words the panel uses.
INTENSITY_WORDS = {"Aggressive": "high", "Not Aggressive": "low"}


def display_path(path: Path) -> str:
    """Keep paths recognizable without repeating the full home directory."""
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~" if relative == Path(".") else str(Path("~") / relative)


def metadata_inspector(entry: TrackEntry, *, context: RowContext) -> list[str]:
    """The Library details panel for one track: a short summary, then the evidence."""
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
    partial = reanalysis_tasks(entry, context) if entry.plan is None else ()
    out_of_date = (
        [f"Out of date: {task_names(partial)} · only these run again; the rest are kept"]
        if partial
        else []
    )
    lines.extend(_summary(entry, context, genre, evidence_owned))
    lines.extend(
        [
            "",
            MORE_DETAIL,
            "",
            f"Recommendation: {review.recommendation}",
            f"Based on: {review.recommendation_source}",
            "",
            review.catalog_title,
            *(f"  {detail}" for detail in review.catalog_details),
            *([""] if out_of_date or review.notices else []),
            *out_of_date,
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
                    "Last analysis failed",
                    f"  {entry.analysis_error.description}",
                    "",
                ]
                if entry.analysis_error is not None
                else []
            ),
            *_audio_failure_lines(entry),
            display_path(entry.path.parent),
        ]
    )
    return lines


def _summary(entry: TrackEntry, context: RowContext, genre: str, owned: OwnedValues) -> list[str]:
    """What SetTag thinks of the track, its state, and what Enter does next."""
    check = genre_check(entry, context)
    if check.suggested_genre is None:
        # Before analysis the status line already says so; after it, say nothing stood out.
        none = ["No genre suggestion"] if check.summary == "No suggestion" else []
        headline = [*none, f"Your tag: {genre}"]
    else:
        source = "audio model" if check.model_genre is not None else "Beatport"
        headline = (
            [f"Genre: {check.suggested_genre} · your tag matches ({source})"]
            if check.relation == "match"
            else [f"Suggested genre: {check.suggested_genre} ({source})", f"Your tag: {genre}"]
        )

    labels: list[str] = []
    evidence_by_task = task_evidence_from_owned(owned)
    for task in context.tasks:
        if task == "genre":
            continue
        chosen = context.select_for_review(evidence_by_task.get(task, ()), task)
        if task == "mood-aggressive":
            words = [INTENSITY_WORDS.get(chosen[0].label, chosen[0].label)] if chosen else []
        else:
            words = [suggested_label((p,)) or p.label for p in chosen[:3]]
        if words:
            labels.append(f"{TASK_LABELS[task]}: {' · '.join(words)}")

    # The same words as the Status column, so the two never disagree.
    sentence = entry_analysis(entry, context)
    if checked_on := catalog_checked_on(owned):
        sentence += f" · Beatport checked {checked_on}"

    step = entry.next_step
    return [
        entry.path.name,
        "",
        *headline,
        *labels,
        "",
        sentence,
        *([f"Enter  {NEXT_STEP_LABELS[step]}"] if step is not None else []),
    ]


def _audio_failure_lines(entry: TrackEntry) -> list[str]:
    """Why a file that keeps failing is no longer selected, and how to try again."""
    if not entry.audio_keeps_failing:
        return []
    assert entry.metadata is not None
    assert entry.metadata.audio_failure is not None
    failure = entry.metadata.audio_failure
    return [
        f"Audio analysis failed the last {failure.attempts} times",
        f"  {failure.error}",
        # One line: the panel wraps it, and a hand-broken line wraps again mid-phrase.
        (f"  Last tried {failure.failed_at[:10]}. Select it, or press Enter, to try again."),
        "",
    ]


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
            "Analysis failed",
            f"  {entry.analysis_error.description}",
            "",
            "Press Space to dismiss it so the other tracks can be written,",
            "or return to the library with B to retry it.",
            *identity,
        ]
    if entry.plan is None:
        return ["No result is available.", *identity]

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
            ranked = context.ranks_against_library(task)
            policy = " · top 3, library top 40%" if ranked else ""
            lines.append(f"{TASK_LABELS[task]} · {count} {noun}{policy}")
            lines.append(_candidate_line(context.select_for_review(evidence, task), ranked=ranked))
        elif task in provenance:
            lines.append(f"{TASK_LABELS[task]} · No ranked evidence")
        else:
            lines.append(f"{TASK_LABELS[task]} · Not analyzed")
    return lines


def _candidate_policy(context: RowContext) -> str:
    cutoff = f"{context.score_cutoff:.3f}".removesuffix("0")
    # Once mood/theme and instrument rank against the library, the cutoff is genre's alone.
    scope = "genre " if any(context.ranks_against_library(task) for task in context.tasks) else ""
    return f"{scope}cutoff ≥ {cutoff} · top {context.review_top}"


def _candidate_line(selected: Sequence[Prediction], *, ranked: bool = False) -> str:
    if selected:
        return "  " + " · ".join(
            f"{suggested_label((prediction,)) or prediction.label} {prediction.score:.3f}"
            for prediction in selected
        )
    return (
        "  No label stands out against the library" if ranked else "  No candidate met the cutoff"
    )
