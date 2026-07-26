from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypedDict

from settag import __version__
from settag.hashing import sha256_file, sha256_json
from settag.policy import EVIDENCE_LIMIT, AudioSample
from settag.tags import TagPlan
from settag.tasks import AnalysisTask, ordered_tasks


class SourceRecord(TypedDict):
    path: str
    size: int
    mtime_ns: int
    sha256: str


class ProvenanceStatus(Enum):
    """How one task's recorded provenance compares to what this build would write."""

    CURRENT = "current"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    MODEL_CHANGED = "model_changed"
    CONFIG_CHANGED = "config_changed"


@dataclass(frozen=True)
class TaskProvenanceReading:
    status: ProvenanceStatus
    analyzed_at: str | None


def _recorded_string(record: object, key: str) -> str | None:
    if not isinstance(record, dict):
        return None
    value = record.get(key)
    return value if isinstance(value, str) and value else None


def read_task_provenance_status(
    task_provenance: object,
    *,
    task: AnalysisTask,
    expected_model_id: str,
    expected_config_sha256: str,
    expected_config: Mapping[str, object] | None = None,
) -> TaskProvenanceReading:
    """Compare one task's recorded provenance against what this build would write.

    The metadata scan and the workbench cache both need this decision and once made
    it separately, differing only in how they report the answer. The rule lives here
    so a change to what counts as stale reaches both, and each caller maps the status
    to its own presentation.
    """
    if task_provenance is None:
        return TaskProvenanceReading(ProvenanceStatus.MISSING, None)
    if not isinstance(task_provenance, dict):
        return TaskProvenanceReading(ProvenanceStatus.UNREADABLE, None)

    model_id = _recorded_string(task_provenance.get("model"), "id")
    config = task_provenance.get("config")
    config_sha256 = _recorded_string(config, "sha256")
    analyzed_at = _recorded_string(task_provenance, "analyzed_at")
    if model_id is None or config_sha256 is None or analyzed_at is None:
        return TaskProvenanceReading(ProvenanceStatus.UNREADABLE, None)

    if model_id != expected_model_id:
        return TaskProvenanceReading(ProvenanceStatus.MODEL_CHANGED, analyzed_at)
    if config_sha256 != expected_config_sha256 and not configs_match_for_task(
        config, expected_config, task
    ):
        return TaskProvenanceReading(ProvenanceStatus.CONFIG_CHANGED, analyzed_at)
    return TaskProvenanceReading(ProvenanceStatus.CURRENT, analyzed_at)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def config_record(
    *,
    top: int,
    threshold: float,
    tasks: tuple[AnalysisTask, ...] = ("genre",),
    sample: AudioSample = "full",
) -> dict[str, object]:
    """Describe the settings a stored analysis was produced under.

    ``sample`` belongs in ``evidence`` rather than ``selection`` because it changes
    which audio the model read, so a change to it must mark stored analyses stale.
    ``top`` and ``threshold`` only change what a reviewer is shown, which is why
    they sit outside the hashed part.
    """
    evidence: dict[str, object] = {
        "schema": "settag.evidence/v3",
        "limit": EVIDENCE_LIMIT,
        "sample": sample,
        "tasks": list(ordered_tasks(tasks)),
    }
    return {
        "evidence": evidence,
        "selection": {
            "top": top,
            "score_cutoff": threshold,
        },
        "sha256": sha256_json(evidence),
    }


def configs_match_for_task(
    recorded_config: object,
    expected_config: object,
    task: AnalysisTask,
) -> bool:
    """Compare evidence settings while allowing a task to move between task groups."""
    if not isinstance(recorded_config, dict) or not isinstance(expected_config, dict):
        return False
    recorded_evidence = recorded_config.get("evidence")
    expected_evidence = expected_config.get("evidence")
    if not isinstance(recorded_evidence, dict) or not isinstance(expected_evidence, dict):
        return False
    recorded_tasks = recorded_evidence.get("tasks")
    expected_tasks = expected_evidence.get("tasks")
    recorded_settings = {key: value for key, value in recorded_evidence.items() if key != "tasks"}
    expected_settings = {key: value for key, value in expected_evidence.items() if key != "tasks"}
    return (
        recorded_settings == expected_settings
        and isinstance(recorded_tasks, list)
        and isinstance(expected_tasks, list)
        and task in recorded_tasks
        and task in expected_tasks
    )


def source_record(path: Path) -> SourceRecord:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def analysis_record(
    *,
    source: Mapping[str, object],
    analyzed_at: str,
    backend_version: str,
    config: dict[str, object],
    tasks: dict[str, dict[str, object]],
    tag_plan: TagPlan,
) -> dict[str, Any]:
    """Build one audit record for an analysis run.

    ``analyze`` never writes; the only route to disk is a reviewed plan applied
    through ``preflight_plan`` and ``apply_prepared``.
    """
    return {
        "schema": "settag.analysis/v3",
        "source": source,
        "analyzed_at": analyzed_at,
        "analyzer": {
            "name": "settag",
            "version": __version__,
            "backend": "essentia-tensorflow",
            "backend_version": backend_version,
        },
        "config": config,
        "tasks": tasks,
        "tag_plan": tag_plan.to_dict(),
    }


def error_record(path: Path, error: BaseException) -> dict[str, object]:
    return {
        "schema": "settag.error/v1",
        "source": {"path": str(path.expanduser().resolve())},
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
