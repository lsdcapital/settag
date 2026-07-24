from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from settag import __version__
from settag.hashing import sha256_file
from settag.plans import (
    PLAN_ERROR_SCHEMA,
    PlannedWrite,
    friendly_change,
    plan_record,
    planned_write_record,
)
from settag.policy import Prediction, select_predictions
from settag.records import config_record, source_record, utc_now
from settag.tags import (
    GenreState,
    OwnedValues,
    TagChange,
    TagPlan,
    apply_metadata_tags,
    build_owned_values,
    plan_owned_tags,
    plan_standard_genres,
    read_genre_state,
)


class GenreAnalyzer(Protocol):
    spec: object
    model_manifest: dict[str, object]
    backend_version: str

    def analyze(self, path: Path) -> list[Prediction]: ...


ProgressCallback = Callable[[int, int, Path], None]
WriteProgressCallback = Callable[[int, int, Path], None]


@dataclass(frozen=True)
class PreparedTrack:
    source: dict[str, object]
    analyzed_at: str
    config: dict[str, object]
    predictions: list[Prediction]
    selected: list[Prediction]
    desired: OwnedValues
    genre_state: GenreState
    tag_plan: TagPlan


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

    @property
    def write_count(self) -> int:
        return sum(bool(item.readable_changes) for item in self.planned)


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
    config = config_record(top=top, threshold=threshold)
    predictions = analyzer.analyze(path)
    selected = select_predictions(predictions, threshold=threshold, top=top)
    spec = analyzer.spec
    model_id = getattr(spec, "id", None)
    if not isinstance(model_id, str):
        raise RuntimeError("Analyzer model specification has no string id")
    desired = build_owned_values(
        selected,
        model_id=model_id,
        analyzed_at=analyzed_at,
        config_sha256=str(config["sha256"]),
    )
    genre_state = read_genre_state(path)
    tag_plan = plan_owned_tags(path, desired)
    return PreparedTrack(
        source=source,
        analyzed_at=analyzed_at,
        config=config,
        predictions=predictions,
        selected=selected,
        desired=desired,
        genre_state=genre_state,
        tag_plan=tag_plan,
    )


def analyze_paths(
    paths: Sequence[Path],
    *,
    analyzer: GenreAnalyzer,
    top: int,
    threshold: float,
    on_progress: ProgressCallback | None = None,
) -> AnalysisBatch:
    planned: list[PlannedWrite] = []
    failures: list[AnalysisFailure] = []
    total = len(paths)
    for index, path in enumerate(paths, start=1):
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
    return AnalysisBatch(planned=tuple(planned), failures=tuple(failures))


def plan_record_for_track(track: PreparedTrack) -> dict[str, object]:
    model_id = track.desired["SETTAG_MODEL"]
    config_sha256 = track.desired["SETTAG_CONFIG_SHA256"]
    settag_version = track.desired["SETTAG_VERSION"]
    return plan_record(
        source=track.source,
        genre_state=track.genre_state,
        selected=track.selected,
        tag_plan=track.tag_plan,
        readable_changes=[friendly_change(change) for change in track.tag_plan.changes],
        model_id=model_id[0] if model_id else "unknown",
        analyzed_at=track.analyzed_at,
        settag_version=settag_version[0] if settag_version else __version__,
        config_sha256=config_sha256[0] if config_sha256 else "unknown",
    )


def planned_write_for_track(track: PreparedTrack) -> PlannedWrite:
    return PlannedWrite(
        path=Path(str(track.source["path"])),
        source_sha256=str(track.source["sha256"]),
        source_size=int(track.source["size"]),
        source_mtime_ns=int(track.source["mtime_ns"]),
        file_genre=track.genre_state.standard,
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
