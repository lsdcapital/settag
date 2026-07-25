from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from settag import __version__
from settag.hashing import sha256_file, sha256_json
from settag.policy import EVIDENCE_LIMIT
from settag.tags import TagPlan
from settag.tasks import AnalysisTask, ordered_tasks


class SourceRecord(TypedDict):
    path: str
    size: int
    mtime_ns: int
    sha256: str


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def config_record(
    *,
    top: int,
    threshold: float,
    tasks: tuple[AnalysisTask, ...] = ("genre",),
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema": "settag.evidence/v2",
        "limit": EVIDENCE_LIMIT,
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
    write_requested: bool,
    write_status: str,
    result_sha256: str | None,
) -> dict[str, Any]:
    write: dict[str, object] = {
        "requested": write_requested,
        "status": write_status,
    }
    if result_sha256 is not None:
        write["result_sha256"] = result_sha256

    return {
        "schema": "settag.analysis/v2",
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
        "write": write,
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
