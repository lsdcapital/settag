from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from settag.hashing import sha256_file
from settag.plans import (
    PLAN_ERROR_SCHEMA,
    PlannedWrite,
    friendly_change,
    planned_write_record,
)
from settag.policy import Prediction, collect_evidence, select_predictions
from settag.records import config_record, configs_match_for_task, source_record, utc_now
from settag.tags import (
    GenreState,
    OwnedValues,
    TagChange,
    TagPlan,
    apply_metadata_tags,
    build_task_owned_values,
    plan_owned_tags,
    plan_standard_genres,
    read_genre_state,
    read_owned_values,
    read_task_provenance,
    task_evidence_from_owned,
)
from settag.tasks import TASK_FIELDS, AnalysisTask, ordered_tasks


class GenreAnalyzer(Protocol):
    spec: object
    model_manifest: dict[str, object]
    backend_version: str

    def analyze(self, path: Path) -> list[Prediction]: ...


ProgressCallback = Callable[[int, int, Path], None]
WriteProgressCallback = Callable[[int, int, Path], None]
CancelCallback = Callable[[], bool]
MetadataStatus = Literal["not_analyzed", "current", "stale", "invalid"]
CacheStatus = Literal["ready", "stale"]


@dataclass(frozen=True)
class PreparedTrack:
    source: dict[str, object]
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

    @property
    def needs_analysis(self) -> bool:
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

    @property
    def has_changes(self) -> bool:
        return bool(self.owned_plan.changes or self.standard_genre_change)


class PartialWriteError(RuntimeError):
    def __init__(self, completed: int, total: int, cause: BaseException) -> None:
        self.completed = completed
        self.total = total
        self.cause = cause
        super().__init__(
            f"Stopped after {completed} of {total} planned writes: {cause}"
        )


def prepare_track(
    path: Path,
    *,
    analyzer: GenreAnalyzer,
    top: int,
    threshold: float,
) -> PreparedTrack:
    source = source_record(path)
    analyzed_at = utc_now()
    current_owned = read_owned_values(path)
    task_predictions, manifests = _analyze_tasks(analyzer, path)
    config = config_record(
        top=top,
        threshold=threshold,
        tasks=ordered_tasks(task_predictions),
    )
    task_evidence = {
        task: collect_evidence(predictions)
        for task, predictions in task_predictions.items()
    }
    task_selected = {
        task: select_predictions(evidence, threshold=threshold, top=top)
        for task, evidence in task_evidence.items()
    }
    task_provenance = {
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
    analyzer: GenreAnalyzer,
    path: Path,
) -> tuple[
    dict[AnalysisTask, list[Prediction]],
    dict[AnalysisTask, dict[str, object]],
]:
    analyze_tasks = getattr(analyzer, "analyze_tasks", None)
    manifests = getattr(analyzer, "model_manifests", None)
    if callable(analyze_tasks) and isinstance(manifests, dict):
        raw = analyze_tasks(path)
        if not isinstance(raw, dict):
            raise RuntimeError("Task analyzer returned an invalid result")
        results = {
            task: list(raw[task])
            for task in ordered_tasks(raw)
        }
        selected_manifests: dict[AnalysisTask, dict[str, object]] = {}
        for task in results:
            manifest = manifests.get(task)
            if not isinstance(manifest, dict):
                raise RuntimeError(f"Task analyzer has no model manifest for {task}")
            selected_manifests[task] = manifest
        return results, selected_manifests

    predictions = analyzer.analyze(path)
    spec = analyzer.spec
    model_id = getattr(spec, "id", None)
    if not isinstance(model_id, str):
        raise RuntimeError("Analyzer model specification has no string id")
    manifest = getattr(analyzer, "model_manifest", None)
    if not isinstance(manifest, dict):
        manifest = {"schema": "settag.models/v1", "id": model_id, "files": {}}
    elif manifest.get("id") != model_id:
        manifest = {**manifest, "id": model_id}
    return {"genre": predictions}, {"genre": manifest}


def analyze_paths(
    paths: Sequence[Path],
    *,
    analyzer: GenreAnalyzer,
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
    expected_model_id: str | None = None,
    expected_model_ids: Mapping[AnalysisTask, str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> MetadataBatch:
    expected_models = _normalize_expected_models(
        expected_model_id=expected_model_id,
        expected_model_ids=expected_model_ids,
    )
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
    expected_model_id: str | None = None,
    expected_model_ids: Mapping[AnalysisTask, str] | None = None,
) -> MetadataTrack:
    expected_models = _normalize_expected_models(
        expected_model_id=expected_model_id,
        expected_model_ids=expected_model_ids,
    )
    genre_state = read_genre_state(path)
    owned = read_owned_values(path)
    has_settag_metadata = any(values is not None for values in owned.values())
    if not has_settag_metadata:
        return MetadataTrack(
            path=path,
            genre_state=genre_state,
            owned=owned,
            stored_predictions=(),
            status="not_analyzed",
            analyzed_at=None,
        )

    stored_by_task = task_evidence_from_owned(owned)
    stored_predictions = stored_by_task.get("genre", ())
    evidence_valid = all(
        _task_evidence_valid(task, owned, stored_by_task)
        for task in expected_models
    )
    provenance = read_task_provenance(owned)
    version = _single_owned_value(owned, "SETTAG_VERSION")
    provenance_invalid = False
    provenance_missing = False
    provenance_stale = False
    analyzed_at_values: list[str] = []
    for task, expected_model_id in expected_models.items():
        task_provenance = provenance.get(task)
        if task_provenance is None:
            provenance_missing = True
            continue
        model = task_provenance.get("model")
        config = task_provenance.get("config")
        model_id = model.get("id") if isinstance(model, dict) else None
        config_sha256 = config.get("sha256") if isinstance(config, dict) else None
        analyzed_at = task_provenance.get("analyzed_at")
        if not all(
            isinstance(value, str) and value
            for value in (model_id, config_sha256, analyzed_at)
        ):
            provenance_invalid = True
            continue
        analyzed_at_values.append(analyzed_at)
        if (
            model_id != expected_model_id
            or (
                config_sha256 != expected_config_sha256
                and not configs_match_for_task(config, expected_config, task)
            )
        ):
            provenance_stale = True

    if not evidence_valid or provenance_invalid or version is None:
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
    return (
        predictions is not None
        and tuple(prediction.label for prediction in predictions) == tuple(labels)
    )


def _normalize_expected_models(
    *,
    expected_model_id: str | None,
    expected_model_ids: Mapping[AnalysisTask, str] | None,
) -> dict[AnalysisTask, str]:
    if expected_model_ids is not None:
        if expected_model_id is not None:
            raise ValueError("provide expected_model_id or expected_model_ids, not both")
        if not expected_model_ids:
            raise ValueError("at least one expected analysis model is required")
        return dict(expected_model_ids)
    if expected_model_id is None:
        raise ValueError("an expected analysis model is required")
    return {"genre": expected_model_id}


def _stored_predictions(
    genre_state: GenreState,
    owned: OwnedValues,
) -> tuple[tuple[Prediction, ...], bool]:
    serialized = owned["SETTAG_GENRE_SCORES"]
    if not genre_state.settag:
        return (), serialized is None
    if serialized is None or len(serialized) != 1:
        return (), False
    try:
        values = json.loads(serialized[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return (), False
    if not isinstance(values, list):
        return (), False

    predictions: list[Prediction] = []
    for value in values:
        if not isinstance(value, dict):
            return (), False
        label = value.get("label")
        score = value.get("score")
        if (
            not isinstance(label, str)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
        ):
            return (), False
        numeric_score = float(score)
        if not 0 <= numeric_score <= 1:
            return (), False
        predictions.append(Prediction(label=label, score=numeric_score))

    labels = tuple(prediction.label for prediction in predictions)
    if labels != genre_state.settag:
        return (), False
    return tuple(predictions), True


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
        source_size=int(track.source["size"]),
        source_mtime_ns=int(track.source["mtime_ns"]),
        file_genre=track.genre_state.standard,
        evidence=tuple(track.evidence),
        selected=tuple(track.selected),
        desired=track.desired,
        metadata_format=track.tag_plan.format,
        owned_changes=tuple(
            friendly_change(change) for change in track.tag_plan.changes
        ),
    )


def preflight_plan(planned: Sequence[PlannedWrite]) -> list[PreparedWrite]:
    prepared: list[PreparedWrite] = []
    errors: list[str] = []
    for item in planned:
        try:
            if not item.path.is_file():
                raise RuntimeError(f"file is missing: {item.path}")
            if sha256_file(item.path) != item.source_sha256:
                raise RuntimeError(f"source SHA-256 changed: {item.path}")
            genre_state = read_genre_state(item.path)
            if genre_state.standard != item.file_genre:
                raise RuntimeError(f"file genre tag changed: {item.path}")
            owned_plan = plan_owned_tags(item.path, item.desired)
            if owned_plan.format != item.metadata_format:
                raise RuntimeError(f"metadata format changed: {item.path}")
            owned_changes = tuple(
                friendly_change(change) for change in owned_plan.changes
            )
            if owned_changes != item.owned_changes:
                raise RuntimeError(
                    f"planned SetTag metadata changes do not match: {item.path}"
                )
            standard_change = (
                plan_standard_genres(item.path, item.target_file_genre)
                if item.target_file_genre is not None
                else None
            )
            if standard_change != item.standard_genre_change:
                raise RuntimeError(
                    f"planned file genre change does not match: {item.path}"
                )
            prepared.append(
                PreparedWrite(
                    item=item,
                    genre_state=genre_state,
                    owned_plan=owned_plan,
                    standard_genre_change=standard_change,
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
) -> int:
    changed = [item for item in prepared if item.has_changes]
    total = len(changed)
    completed = 0
    try:
        for prepared_item in changed:
            item = prepared_item.item
            if sha256_file(item.path) != item.source_sha256:
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
            if on_progress is not None:
                on_progress(completed, total, item.path)
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
    stamp = (
        utc_now()
        .replace("-", "")
        .replace(":", "")
        .replace("T", "-")
        .removesuffix("Z")
    )
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
