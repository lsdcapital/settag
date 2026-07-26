from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from settag.plans import (
    PlanError,
    PlannedWrite,
    planned_write_from_record,
    planned_write_record,
)
from settag.records import ProvenanceStatus, read_task_provenance_status
from settag.tags import read_task_provenance
from settag.tasks import AnalysisTask, checked_expected_models

STATE_SCHEMA_VERSION = 1
CacheStatus = Literal["ready", "stale"]

# How a cached plan describes provenance that no longer matches this build. The comparison
# itself lives in `records`; only the wording is the workbench's own.
STALE_REASONS = {
    ProvenanceStatus.MISSING: "analysis tasks changed",
    ProvenanceStatus.UNREADABLE: "analysis provenance is unreadable",
    ProvenanceStatus.MODEL_CHANGED: "analysis model changed",
    ProvenanceStatus.CONFIG_CHANGED: "evidence settings changed",
}


class WorkbenchError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkbenchEntry:
    plan: PlannedWrite
    status: CacheStatus
    reason: str | None = None


def default_state_db() -> Path:
    override = os.environ.get("SETTAG_STATE_DB")
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        configured = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = Path(configured) if configured else Path.home() / "AppData" / "Local"
    else:
        configured = os.environ.get("XDG_DATA_HOME")
        base = Path(configured) if configured else Path.home() / ".local" / "share"
    return (base / "settag" / "state.sqlite3").resolve()


DEFAULT_STATE_DB = default_state_db()


class WorkbenchStore:
    def __init__(self, path: Path = DEFAULT_STATE_DB) -> None:
        self.path = path.expanduser().resolve()

    def save(self, plan: PlannedWrite) -> None:
        record = planned_write_record(plan)
        serialized = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO workbench_plans(path, plan_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    plan_json = excluded.plan_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(plan.path.expanduser().resolve()),
                    serialized,
                    _utc_now(),
                ),
            )

    def delete(self, paths: Sequence[Path]) -> None:
        if not paths:
            return
        values = [(str(path.expanduser().resolve()),) for path in paths]
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "DELETE FROM workbench_plans WHERE path = ?",
                values,
            )

    def load(
        self,
        paths: Sequence[Path],
        *,
        expected_config_sha256: str,
        expected_config: Mapping[str, object] | None = None,
        expected_model_ids: Mapping[AnalysisTask, str],
    ) -> dict[Path, WorkbenchEntry]:
        """Return the cached entry for every path the workbench can still read.

        A row this build cannot decode is dropped, not raised. The workbench is
        private, restartable working state whose only irreplaceable content is
        in the audio tags, so a superseded ``PLAN_SCHEMA`` or a corrupt row
        costs one reanalysis. Failing the load instead refused to start the app
        at all, and the only recovery was deleting the database by hand.
        """
        resolved_paths = tuple(path.expanduser().resolve() for path in paths)
        if not resolved_paths:
            return {}
        expected_models = checked_expected_models(expected_model_ids)

        entries: dict[Path, WorkbenchEntry] = {}
        unreadable: list[Path] = []
        with closing(self._connect()) as connection:
            for start in range(0, len(resolved_paths), 400):
                chunk = resolved_paths[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT path, plan_json
                    FROM workbench_plans
                    WHERE path IN ({placeholders})
                    """,
                    tuple(str(path) for path in chunk),
                )
                for row in rows:
                    path = Path(str(row["path"]))
                    try:
                        plan = self._decode(str(row["plan_json"]), path)
                    except WorkbenchError:
                        unreadable.append(path)
                        continue
                    entries[path] = _classify(
                        plan,
                        expected_model_ids=expected_models,
                        expected_config_sha256=expected_config_sha256,
                        expected_config=expected_config,
                    )
        self.delete(unreadable)
        return entries

    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > STATE_SCHEMA_VERSION:
                connection.close()
                raise WorkbenchError(
                    f"Workbench database schema {version} is newer than "
                    f"this SetTag supports ({STATE_SCHEMA_VERSION}): {self.path}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workbench_plans (
                    path TEXT PRIMARY KEY,
                    plan_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            if version < STATE_SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")
            connection.commit()
            return connection
        except (OSError, sqlite3.Error) as error:
            raise WorkbenchError(
                f"Could not open SetTag workbench database {self.path}: {error}"
            ) from error

    def _decode(self, serialized: str, path: Path) -> PlannedWrite:
        try:
            value = json.loads(serialized)
            plan = planned_write_from_record(
                value,
                location=f"{self.path}:{path}",
            )
        except (json.JSONDecodeError, PlanError) as error:
            raise WorkbenchError(f"Invalid cached analysis for {path}: {error}") from error
        if plan.path.expanduser().resolve() != path.expanduser().resolve():
            raise WorkbenchError(f"Cached analysis path does not match its database key: {path}")
        return plan


def _classify(
    plan: PlannedWrite,
    *,
    expected_model_ids: Mapping[AnalysisTask, str],
    expected_config_sha256: str,
    expected_config: Mapping[str, object] | None = None,
) -> WorkbenchEntry:
    if not plan.path.is_file():
        return WorkbenchEntry(plan, "stale", "source file is missing")

    stat = plan.path.stat()
    if stat.st_size != plan.source_size or stat.st_mtime_ns != plan.source_mtime_ns:
        return WorkbenchEntry(plan, "stale", "source file changed")

    provenance = read_task_provenance(plan.desired)
    for task, expected_model_id in expected_model_ids.items():
        reading = read_task_provenance_status(
            provenance.get(task),
            task=task,
            expected_model_id=expected_model_id,
            expected_config_sha256=expected_config_sha256,
            expected_config=expected_config,
        )
        if reading.status is not ProvenanceStatus.CURRENT:
            return WorkbenchEntry(plan, "stale", STALE_REASONS[reading.status])

    return WorkbenchEntry(plan, "ready")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
