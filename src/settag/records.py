from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from settag import __version__
from settag.hashing import sha256_file, sha256_json
from settag.policy import Prediction
from settag.tags import TagPlan


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def config_record(*, top: int, threshold: float) -> dict[str, object]:
    values: dict[str, object] = {
        "top": top,
        "threshold": threshold,
    }
    return {**values, "sha256": sha256_json(values)}


def source_record(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def analysis_record(
    *,
    source: dict[str, object],
    analyzed_at: str,
    backend_version: str,
    model: dict[str, object],
    config: dict[str, object],
    predictions: list[Prediction],
    selected: list[Prediction],
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
        "schema": "settag.analysis/v1",
        "source": source,
        "analyzed_at": analyzed_at,
        "analyzer": {
            "name": "settag",
            "version": __version__,
            "backend": "essentia-tensorflow",
            "backend_version": backend_version,
        },
        "model": model,
        "config": config,
        "predictions": [item.to_dict() for item in predictions],
        "selected": [item.to_dict() for item in selected],
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
