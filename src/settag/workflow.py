from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from settag.hashing import sha256_audio, sha256_file
from settag.journal import WriteRecord
from settag.plans import (
    PLAN_ERROR_SCHEMA,
    PlannedWrite,
    friendly_change,
    planned_write_record,
)
from settag.policy import (
    MIN_GENRE_SECONDS,
    Prediction,
    collect_evidence,
    select_predictions,
)
from settag.records import (
    ProvenanceStatus,
    SourceRecord,
    config_record,
    orphaned_tasks,
    read_task_provenance_status,
    source_record,
    utc_now,
)
from settag.tags import (
    GenreState,
    OwnedValues,
    TagChange,
    TagPlan,
    apply_metadata_tags,
    build_task_owned_values,
    plan_hygiene_tags,
    plan_owned_tags,
    plan_standard_genres,
    read_duration_seconds,
    read_genre_state,
    read_owned_values,
    read_task_provenance,
    task_evidence_from_owned,
)
from settag.tasks import (
    TASK_FIELDS,
    AnalysisTask,
    checked_expected_models,
    ordered_tasks,
)


class AnalyzerSpec(Protocol):
    @property
    def id(self) -> str: ...


class GenreAnalyzer(Protocol):
    @property
    def spec(self) -> AnalyzerSpec: ...

    def analyze(self, path: Path) -> list[Prediction]: ...


@runtime_checkable
class TaskAnalyzer(Protocol):
    @property
    def model_manifests(
        self,
    ) -> Mapping[AnalysisTask, Mapping[str, object]]: ...

    def analyze_tasks(
        self,
        path: Path,
    ) -> Mapping[AnalysisTask, Sequence[Prediction]]: ...


Analyzer = GenreAnalyzer | TaskAnalyzer


ProgressCallback = Callable[[int, int, Path], None]
WriteProgressCallback = Callable[[int, int, Path], None]
CancelCallback = Callable[[], bool]
WriteCallback = Callable[[WriteRecord], None]
MetadataStatus = Literal["not_analyzed", "current", "stale", "invalid", "sample"]
CacheStatus = Literal["ready", "stale"]


@dataclass(frozen=True)
class PreparedTrack:
    source: SourceRecord
    analyzed_at: str
    config: dict[str, object]
    predictions: list[Prediction]
    evidence: list[Prediction]
    selected: list[Prediction]
    desired: OwnedValues
    genre_state: GenreState
    tag_plan: TagPlan
    task_predictions: dict[AnalysisTask, list[Prediction]]
    task_evidence: dict[AnalysisTask, list[Prediction]]
    task_selected: dict[AnalysisTask, list[Prediction]]
    task_provenance: dict[AnalysisTask, dict[str, object]]

    @property
    def owned_change_count(self) -> int:
        return len(self.tag_plan.changes)


@dataclass(frozen=True)
class AnalysisFailure:
    path: Path
    error_type: str
    message: str

    @property
    def description(self) -> str:
        return f"{self.error_type}: {self.message}"

    def to_record(self) -> dict[str, object]:
        return {
            "schema": PLAN_ERROR_SCHEMA,
            "path": str(self.path.expanduser().resolve()),
            "error": self.description,
        }


@dataclass(frozen=True)
class AnalysisBatch:
    planned: tuple[PlannedWrite, ...]
    failures: tuple[AnalysisFailure, ...]
    cancelled: bool = False

    @property
    def write_count(self) -> int:
        return sum(bool(item.readable_changes) for item in self.planned)

    @property
    def failure_count(self) -> int:
        return len(self.failures)


@dataclass(frozen=True)
class MetadataTrack:
    path: Path
    genre_state: GenreState
    owned: OwnedValues
    stored_predictions: tuple[Prediction, ...]
    status: MetadataStatus
    analyzed_at: str | None
    cached_plan: PlannedWrite | None = None
    cache_status: CacheStatus | None = None
    cache_reason: str | None = None
    duration_seconds: float | None = None

    @property
    def is_sample(self) -> bool:
        """Too short for the genre model to read at all.

        Not a failure: a clip below one patch is a different kind of thing from
        a track, and no setting makes it analyzable. Classifying it during the
        metadata scan keeps it out of the analyzer entirely, so the run never
        has to report an error it cannot act on.
        """
        return self.status == "sample"

    @property
    def needs_analysis(self) -> bool:
        if self.is_sample:
            return False
        return self.status != "current" and self.cache_status != "ready"


@dataclass(frozen=True)
class MetadataBatch:
    tracks: tuple[MetadataTrack, ...]
    failures: tuple[AnalysisFailure, ...]


@dataclass(frozen=True)
class PreparedWrite:
    item: PlannedWrite
    genre_state: GenreState
    owned_plan: TagPlan
    standard_genre_change: TagChange | None
    owned_before: OwnedValues

    @property
    def has_changes(self) -> bool:
        return bool(self.owned_plan.changes or self.standard_genre_change)


@dataclass(frozen=True)
class WriteTrackSummary:
    """User-facing facts for one track in a pending write."""

    filename: str
    evidence: str
    standard_genre: str

    def render(self) -> str:
        return f"{self.filename}\n  {self.evidence}\n  {self.standard_genre}"


@dataclass(frozen=True)
class WriteSummary:
    """What a batch of prepared writes would do, counted once.

    Both UIs describe a pending write before asking for confirmation. Deriving
    these counts in each of them let the two drift: the confirm dialog counted
    ranked scores across every task while the plain CLI counted genre evidence
    only, so one batch had two different answers. The counts belong here.
    """

    track_count: int
    write_count: int
    bundle_changes: int
    field_changes: int
    standard_genre_edits: int
    evidence_scores: int
    empty_file_genres: int
    tracks: tuple[WriteTrackSummary, ...]

    @property
    def unchanged_count(self) -> int:
        return self.track_count - self.write_count

    @property
    def confirmation_title(self) -> str:
        return f"Ready to write {self.track_count} {_plural(self.track_count, 'track')}?"

    @property
    def confirmation_action(self) -> str:
        return f"Write {self.track_count} {_plural(self.track_count, 'track')}"

    @property
    def confirmation_help(self) -> str:
        return (
            "Preflight passed. Only SetTag evidence and staged standard genre edits "
            "will be written; unrelated metadata stays unchanged. SetTag will reopen "
            "and verify every file."
        )

    def confirmation_preview(self, *, limit: int = 3) -> str:
        visible = self.tracks[:limit]
        sections = [track.render() for track in visible]
        hidden = len(self.tracks) - len(visible)
        if hidden:
            sections.append(f"+ {hidden} more {_plural(hidden, 'track')}")

        evidence_noun = _plural(self.bundle_changes, "SetTag evidence write")
        genre_noun = _plural(self.standard_genre_edits, "standard genre edit")
        sections.append(
            f"Batch total: {self.bundle_changes} {evidence_noun}"
            f" · {self.standard_genre_edits} {genre_noun}"
        )
        return "\n\n".join(sections)


def summarize_writes(prepared: Sequence[PreparedWrite]) -> WriteSummary:
    """Summarize writes that have passed preflight."""
    return WriteSummary(
        track_count=len(prepared),
        write_count=sum(item.has_changes for item in prepared),
        bundle_changes=sum(bool(item.owned_plan.changes) for item in prepared),
        field_changes=sum(len(item.owned_plan.changes) for item in prepared),
        standard_genre_edits=sum(item.standard_genre_change is not None for item in prepared),
        evidence_scores=sum(item.item.evidence_score_count for item in prepared),
        empty_file_genres=sum(not item.item.file_genre for item in prepared),
        tracks=tuple(_summarize_track(item.item) for item in prepared),
    )


def summarize_planned(planned: Sequence[PlannedWrite]) -> WriteSummary:
    """Summarize plans that have not been preflighted against their files.

    Used by the dry run and saved-plan output, which describe a plan without
    opening the audio again. The counts match ``summarize_writes`` so a batch
    never reads differently before and after preflight.
    """
    return WriteSummary(
        track_count=len(planned),
        write_count=sum(bool(item.readable_changes) for item in planned),
        bundle_changes=sum(bool(item.owned_changes) for item in planned),
        field_changes=sum(len(item.owned_changes) for item in planned),
        standard_genre_edits=sum(item.standard_genre_change is not None for item in planned),
        evidence_scores=sum(item.evidence_score_count for item in planned),
        empty_file_genres=sum(not item.file_genre for item in planned),
        tracks=tuple(_summarize_track(item) for item in planned),
    )


def _summarize_track(item: PlannedWrite) -> WriteTrackSummary:
    score_count = item.evidence_score_count
    if item.evidence_write_kind != "unchanged":
        score_detail = (
            f" · {score_count} ranked {_plural(score_count, 'score')}" if score_count else ""
        )
        action = "refresh" if item.evidence_write_kind == "refreshed" else "update"
        evidence = f"SetTag evidence: {action}{score_detail}"
    else:
        evidence = "SetTag evidence: unchanged"

    standard_change = item.standard_genre_change
    if standard_change is None:
        current = _genres(item.file_genre)
        standard_genre = f"Standard genre: unchanged ({current})"
    else:
        standard_genre = (
            f"Standard genre: {_genres(standard_change.before)} → {_genres(standard_change.after)}"
        )

    return WriteTrackSummary(
        filename=item.path.name,
        evidence=evidence,
        standard_genre=standard_genre,
    )


def _genres(values: Sequence[str] | None) -> str:
    return ", ".join(values) if values else "None"


def _plural(count: int, singular: str) -> str:
    return singular if count == 1 else f"{singular}s"


@dataclass(frozen=True)
class BlockedUndo:
    entry: WriteRecord
    reason: str


@dataclass(frozen=True)
class UndoPreflight:
    """Journal entries split into what can be restored now and what cannot."""

    restorable: tuple[WriteRecord, ...]
    blocked: tuple[BlockedUndo, ...]

    @property
    def restore_count(self) -> int:
        return len(self.restorable)

    @property
    def blocked_count(self) -> int:
        return len(self.blocked)

    @property
    def standard_genre_edits(self) -> int:
        return sum(entry.standard_genre_change is not None for entry in self.restorable)

    @property
    def restores_everything(self) -> bool:
        """Whether a successful undo leaves nothing in the batch for a later, forced retry.

        Only then may the batch be marked reverted. Marking it after a partial restore
        would tell the user the write was undone while skipped files still carry it.
        """
        return not self.blocked


class TrackTooShortError(ValueError):
    """The audio is shorter than the model's one-patch minimum.

    The interactive scan classifies such clips as samples and never sends them to
    the analyzer. The plain `analyze` command and the worker have no scan step,
    so without this they reach Essentia and fail with its native message, which
    names neither the length nor the limit.
    """


class PartialWriteError(RuntimeError):
    def __init__(self, completed: int, total: int, cause: BaseException) -> None:
        self.completed = completed
        self.total = total
        self.cause = cause
        super().__init__(f"Stopped after {completed} of {total} planned writes: {cause}")


def prepare_track(
    path: Path,
    *,
    analyzer: Analyzer,
    top: int,
    threshold: float,
) -> PreparedTrack:
    source = source_record(path)
    # Declared by the analyzer rather than assumed, so a test double that ignores
    # audio, or a task set without the genre model, imposes no minimum.
    minimum = getattr(analyzer, "min_seconds", None)
    if minimum is not None:
        duration = read_duration_seconds(path)
        if duration is not None and duration < minimum:
            raise TrackTooShortError(
                f"{duration:.1f}s of audio is shorter than the {minimum:g}s the genre model "
                "reads; this is a sample rather than a track and no setting makes it analyzable"
            )
    analyzed_at = utc_now()
    current_owned = read_owned_values(path)
    task_predictions, manifests = _analyze_tasks(analyzer, path)
    config = config_record(
        top=top,
        threshold=threshold,
        tasks=ordered_tasks(task_predictions),
        # Read from the analyzer that produced these predictions rather than passed in
        # alongside it, so the recorded setting cannot disagree with the one actually
        # used. Analyzers that ignore audio entirely, such as test doubles, read whole.
        genre_sample=getattr(analyzer, "sample", "full"),
    )
    task_evidence = {
        task: collect_evidence(predictions) for task, predictions in task_predictions.items()
    }
    task_selected = {
        task: select_predictions(evidence, threshold=threshold, top=top)
        for task, evidence in task_evidence.items()
    }
    task_provenance: dict[AnalysisTask, dict[str, object]] = {
        task: {
            "model": manifests[task],
            "analyzed_at": analyzed_at,
            "config": config,
        }
        for task in ordered_tasks(task_predictions)
    }
    desired = build_task_owned_values(
        current_owned,
        task_evidence,
        task_provenance,
    )
    stored = task_evidence_from_owned(desired)
    evidence = list(stored.get("genre", ()))
    predictions = task_predictions.get("genre", evidence)
    selected = task_selected.get("genre", [])
    genre_state = read_genre_state(path)
    tag_plan = plan_owned_tags(path, desired)
    return PreparedTrack(
        source=source,
        analyzed_at=analyzed_at,
        config=config,
        predictions=predictions,
        evidence=evidence,
        selected=selected,
        desired=desired,
        genre_state=genre_state,
        tag_plan=tag_plan,
        task_predictions=task_predictions,
        task_evidence=task_evidence,
        task_selected=task_selected,
        task_provenance=task_provenance,
    )


def _analyze_tasks(
    analyzer: Analyzer,
    path: Path,
) -> tuple[
    dict[AnalysisTask, list[Prediction]],
    dict[AnalysisTask, dict[str, object]],
]:
    if isinstance(analyzer, TaskAnalyzer):
        raw = analyzer.analyze_tasks(path)
        if not isinstance(raw, Mapping):
            raise RuntimeError("Task analyzer returned an invalid result")
        results = {task: list(raw[task]) for task in ordered_tasks(raw)}
        selected_manifests: dict[AnalysisTask, dict[str, object]] = {}
        for task in results:
            manifest = analyzer.model_manifests.get(task)
            if manifest is None:
                raise RuntimeError(f"Task analyzer has no model manifest for {task}")
            selected_manifests[task] = dict(manifest)
        return results, selected_manifests

    predictions = analyzer.analyze(path)
    spec = analyzer.spec
    model_id = getattr(spec, "id", None)
    if not isinstance(model_id, str):
        raise RuntimeError("Analyzer model specification has no string id")
    raw_manifest = getattr(analyzer, "model_manifest", None)
    manifest: dict[str, object]
    if not isinstance(raw_manifest, dict):
        manifest = {"schema": "settag.models/v1", "id": model_id, "files": {}}
    else:
        if not all(isinstance(key, str) for key in raw_manifest):
            raise RuntimeError("Analyzer model manifest has non-string keys")
        manifest = {key: value for key, value in raw_manifest.items() if isinstance(key, str)}
        if manifest.get("id") != model_id:
            manifest["id"] = model_id
    return {"genre": list(predictions)}, {"genre": manifest}


def analyze_paths(
    paths: Sequence[Path],
    *,
    analyzer: Analyzer,
    top: int,
    threshold: float,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> AnalysisBatch:
    planned: list[PlannedWrite] = []
    failures: list[AnalysisFailure] = []
    total = len(paths)
    for index, path in enumerate(paths, start=1):
        if should_cancel is not None and should_cancel():
            break
        try:
            track = prepare_track(
                path,
                analyzer=analyzer,
                top=top,
                threshold=threshold,
            )
            planned.append(planned_write_for_track(track))
        except Exception as error:
            failures.append(
                AnalysisFailure(
                    path=path,
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )
        finally:
            if on_progress is not None:
                on_progress(index, total, path)
    cancelled = should_cancel is not None and should_cancel()
    return AnalysisBatch(
        planned=tuple(planned),
        failures=tuple(failures),
        cancelled=cancelled,
    )


def inspect_paths(
    paths: Sequence[Path],
    *,
    expected_config_sha256: str,
    expected_config: Mapping[str, object] | None = None,
    expected_model_ids: Mapping[AnalysisTask, str],
    on_progress: ProgressCallback | None = None,
) -> MetadataBatch:
    expected_models = checked_expected_models(expected_model_ids)
    tracks: list[MetadataTrack] = []
    failures: list[AnalysisFailure] = []
    total = len(paths)
    for index, path in enumerate(paths, start=1):
        try:
            tracks.append(
                inspect_track(
                    path,
                    expected_model_ids=expected_models,
                    expected_config_sha256=expected_config_sha256,
                    expected_config=expected_config,
                )
            )
        except Exception as error:
            failures.append(
                AnalysisFailure(
                    path=path,
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )
        finally:
            if on_progress is not None:
                on_progress(index, total, path)
    return MetadataBatch(tracks=tuple(tracks), failures=tuple(failures))


def inspect_track(
    path: Path,
    *,
    expected_config_sha256: str,
    expected_config: Mapping[str, object] | None = None,
    expected_model_ids: Mapping[AnalysisTask, str],
) -> MetadataTrack:
    expected_models = checked_expected_models(expected_model_ids)
    genre_state = read_genre_state(path)
    owned = read_owned_values(path)
    duration = read_duration_seconds(path)
    if duration is not None and duration < MIN_GENRE_SECONDS:
        # Checked before anything else: what the tags say does not matter when the
        # audio is too short for the model to read.
        return MetadataTrack(
            path=path,
            genre_state=genre_state,
            owned=owned,
            stored_predictions=task_evidence_from_owned(owned).get("genre", ()),
            status="sample",
            analyzed_at=None,
            duration_seconds=duration,
        )

    has_settag_metadata = any(values is not None for values in owned.values())
    if not has_settag_metadata:
        return MetadataTrack(
            path=path,
            genre_state=genre_state,
            owned=owned,
            stored_predictions=(),
            status="not_analyzed",
            analyzed_at=None,
            duration_seconds=duration,
        )

    stored_by_task = task_evidence_from_owned(owned)
    stored_predictions = stored_by_task.get("genre", ())
    evidence_valid = all(
        _task_evidence_valid(task, owned, stored_by_task) for task in expected_models
    )
    provenance = read_task_provenance(owned)
    version = _single_owned_value(owned, "SETTAG_VERSION")
    provenance_invalid = False
    provenance_missing = False
    provenance_stale = False
    analyzed_at_values: list[str] = []
    for task, expected_model_id in expected_models.items():
        reading = read_task_provenance_status(
            provenance.get(task),
            task=task,
            expected_model_id=expected_model_id,
            expected_config_sha256=expected_config_sha256,
            expected_config=expected_config,
        )
        if reading.analyzed_at is not None:
            analyzed_at_values.append(reading.analyzed_at)
        if reading.status is ProvenanceStatus.MISSING:
            provenance_missing = True
        elif reading.status is ProvenanceStatus.UNREADABLE:
            provenance_invalid = True
        elif reading.status is not ProvenanceStatus.CURRENT:
            provenance_stale = True

    # Labels belonging to a task this scan does not otherwise look at, with no record to
    # attribute them. Incomplete rather than stale: nothing here is out of date, the file
    # simply carries evidence it cannot explain. Re-analysis clears it either way, since
    # the write drops it.
    orphaned = orphaned_tasks(owned, checked=expected_models)

    if not evidence_valid or provenance_invalid or version is None or orphaned:
        status: MetadataStatus = "invalid"
    elif provenance_missing or provenance_stale:
        status = "stale"
    else:
        status = "current"
    analyzed_at = max(analyzed_at_values, default=None)

    return MetadataTrack(
        path=path,
        genre_state=genre_state,
        owned=owned,
        stored_predictions=stored_predictions,
        status=status,
        analyzed_at=analyzed_at,
        duration_seconds=duration,
    )


def _task_evidence_valid(
    task: AnalysisTask,
    owned: OwnedValues,
    stored_by_task: Mapping[AnalysisTask, tuple[Prediction, ...]],
) -> bool:
    label_field, score_field = TASK_FIELDS[task]
    labels = owned.get(label_field)
    scores = owned.get(score_field)
    if not labels:
        return scores is None
    predictions = stored_by_task.get(task)
    return predictions is not None and tuple(
        prediction.label for prediction in predictions
    ) == tuple(labels)


def _single_owned_value(owned: OwnedValues, field: str) -> str | None:
    values = owned[field]
    if values is None or len(values) != 1 or not values[0]:
        return None
    return values[0]


def plan_record_for_track(track: PreparedTrack) -> dict[str, object]:
    return planned_write_record(planned_write_for_track(track))


def planned_write_for_track(track: PreparedTrack) -> PlannedWrite:
    return PlannedWrite(
        path=Path(str(track.source["path"])),
        source_sha256=str(track.source["sha256"]),
        source_audio_sha256=str(track.source["audio_sha256"]),
        source_size=track.source["size"],
        source_mtime_ns=track.source["mtime_ns"],
        file_genre=track.genre_state.standard,
        evidence=tuple(track.evidence),
        selected=tuple(track.selected),
        desired=track.desired,
        metadata_format=track.tag_plan.format,
        owned_changes=tuple(friendly_change(change) for change in track.tag_plan.changes),
    )


def _source_audio_changed(item: PlannedWrite) -> bool:
    """Report whether the audio has changed since this plan was made.

    A plan carrying an audio digest is checked against that, so a tag write by
    another tool between analysis and write no longer reads as a changed
    source. A v4 plan predates the digest and falls back to the whole-file
    comparison it was written with, which is stricter but never wrong.
    """
    if item.source_audio_sha256 is None:
        return sha256_file(item.path) != item.source_sha256
    return sha256_audio(item.path) != item.source_audio_sha256


def preflight_plan(planned: Sequence[PlannedWrite]) -> list[PreparedWrite]:
    prepared: list[PreparedWrite] = []
    errors: list[str] = []
    for item in planned:
        try:
            if not item.path.is_file():
                raise RuntimeError(
                    f"file is missing: {item.path}"
                    " (if it was renamed or moved, rescan the library to find it again)"
                )
            if _source_audio_changed(item):
                raise RuntimeError(f"source audio changed: {item.path}")
            genre_state = read_genre_state(item.path)
            if genre_state.standard != item.file_genre:
                raise RuntimeError(f"file genre tag changed: {item.path}")
            owned_plan = plan_owned_tags(item.path, item.desired)
            if owned_plan.format != item.metadata_format:
                raise RuntimeError(f"metadata format changed: {item.path}")
            owned_changes = tuple(friendly_change(change) for change in owned_plan.changes)
            if owned_changes != item.owned_changes:
                raise RuntimeError(f"planned SetTag metadata changes do not match: {item.path}")
            standard_change = (
                plan_standard_genres(item.path, item.target_file_genre)
                if item.target_file_genre is not None
                else None
            )
            if standard_change != item.standard_genre_change:
                raise RuntimeError(f"planned file genre change does not match: {item.path}")
            prepared.append(
                PreparedWrite(
                    item=item,
                    genre_state=genre_state,
                    owned_plan=owned_plan,
                    standard_genre_change=standard_change,
                    owned_before=read_owned_values(item.path),
                )
            )
        except Exception as error:
            errors.append(str(error))
    if errors:
        details = "\n  ".join(errors)
        raise RuntimeError(f"{len(errors)} stale or invalid track(s):\n  {details}")
    return prepared


def apply_prepared(
    prepared: Sequence[PreparedWrite],
    *,
    on_progress: WriteProgressCallback | None = None,
    on_write: WriteCallback | None = None,
) -> int:
    """Apply verified writes, handing each completed one to ``on_write``.

    ``on_write`` is called only after a file is written and verified, so the
    journal never claims a change that did not land. It must not raise: the
    audio write has already succeeded by then, and a storage failure there is
    not a failed write.
    """
    changed = [item for item in prepared if item.has_changes]
    total = len(changed)
    completed = 0
    try:
        for prepared_item in changed:
            item = prepared_item.item
            if _source_audio_changed(item):
                raise RuntimeError(f"Source changed before its write: {item.path}")
            applied = apply_metadata_tags(
                item.path,
                item.desired,
                standard_genres=item.target_file_genre,
                expected_plan=prepared_item.owned_plan,
                expected_standard=prepared_item.genre_state.standard,
                expected_standard_change=prepared_item.standard_genre_change,
            )
            if applied != prepared_item.owned_plan:
                raise RuntimeError(f"Applied tag plan differed for {item.path}")
            completed += 1
            if on_write is not None:
                _record_write(prepared_item, on_write)
            if on_progress is not None:
                on_progress(completed, total, item.path)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        raise PartialWriteError(completed, total, error) from error
    return completed


def _record_write(prepared: PreparedWrite, on_write: WriteCallback) -> None:
    item = prepared.item
    stat = item.path.stat()
    record = WriteRecord(
        path=item.path,
        metadata_format=item.metadata_format,
        owned_before=dict(prepared.owned_before),
        owned_after=dict(item.desired),
        standard_before=prepared.genre_state.standard,
        standard_after=item.target_file_genre,
        sha256_before=item.source_sha256,
        size_after=stat.st_size,
        mtime_ns_after=stat.st_mtime_ns,
        written_at=utc_now(),
    )
    # The file was already written and verified. Losing the journal entry must
    # never be reported as a failed write; recorders surface their own storage
    # failures through BatchRecorder.error.
    with suppress(Exception):
        on_write(record)


def preflight_undo(
    entries: Sequence[WriteRecord],
    *,
    force: bool = False,
) -> UndoPreflight:
    """Split journal entries into what can be safely restored and what cannot.

    Unlike ``preflight_plan`` this reports per entry instead of raising, so a
    partly recoverable batch can still be shown and acted on.
    """
    restorable: list[WriteRecord] = []
    blocked: list[BlockedUndo] = []
    for entry in entries:
        reason = _undo_blocker(entry, force=force)
        if reason is None:
            restorable.append(entry)
        else:
            blocked.append(BlockedUndo(entry=entry, reason=reason))
    return UndoPreflight(restorable=tuple(restorable), blocked=tuple(blocked))


def _undo_blocker(entry: WriteRecord, *, force: bool) -> str | None:
    if not entry.path.is_file():
        return "file is missing"
    if force:
        return None
    stat = entry.path.stat()
    if stat.st_size != entry.size_after or stat.st_mtime_ns != entry.mtime_ns_after:
        return "file changed after SetTag wrote it"
    return None


def apply_undo(
    entries: Sequence[WriteRecord],
    *,
    on_progress: WriteProgressCallback | None = None,
) -> int:
    """Restore the tag values each write replaced, newest write first.

    Only the SetTag-owned bundle, an explicitly staged conventional genre edit,
    and explicitly cleaned hygiene fields are rewritten. This is not a
    byte-level restore: mutagen rewrites the tag block on save, so the file will
    not regain its pre-write SHA-256.
    """
    total = len(entries)
    completed = 0
    try:
        for entry in entries:
            standard = entry.standard_before if entry.standard_after is not None else None
            expected_standard_change = (
                plan_standard_genres(entry.path, standard) if standard is not None else None
            )
            hygiene = dict(entry.hygiene_before) if entry.hygiene_changes else None
            expected_hygiene_plan = (
                plan_hygiene_tags(entry.path, hygiene) if hygiene is not None else None
            )
            apply_metadata_tags(
                entry.path,
                dict(entry.owned_before),
                standard_genres=standard,
                expected_standard_change=expected_standard_change,
                hygiene_values=hygiene,
                expected_hygiene_plan=expected_hygiene_plan,
            )
            completed += 1
            if on_progress is not None:
                on_progress(completed, total, entry.path)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        raise PartialWriteError(completed, total, error) from error
    return completed


def save_plan(
    planned: Sequence[PlannedWrite],
    *,
    failures: Sequence[AnalysisFailure] = (),
    directory: Path | None = None,
) -> Path:
    stamp = utc_now().replace("-", "").replace(":", "").replace("T", "-").removesuffix("Z")
    parent = (directory or Path.cwd()).expanduser().resolve()
    candidate = parent / f"settag-plan-{stamp}.jsonl"
    suffix = 2
    while candidate.exists():
        candidate = parent / f"settag-plan-{stamp}-{suffix}.jsonl"
        suffix += 1

    records = [
        *(failure.to_record() for failure in failures),
        *(planned_write_record(item) for item in planned),
    ]
    with candidate.open("x", encoding="utf-8") as output:
        for record in records:
            print(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                file=output,
                flush=True,
            )
    return candidate
