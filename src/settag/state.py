from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from settag.hashing import sha256_audio, sha256_file
from settag.plans import (
    PlanError,
    PlannedWrite,
    planned_write_from_record,
    planned_write_record,
)
from settag.records import ProvenanceStatus, read_task_provenance_status
from settag.tags import read_task_provenance
from settag.tasks import AnalysisTask, checked_expected_models

STATE_SCHEMA_VERSION = 2
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
                INSERT INTO workbench_plans(
                    path, plan_json, updated_at, audio_sha256, source_size
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    plan_json = excluded.plan_json,
                    updated_at = excluded.updated_at,
                    audio_sha256 = excluded.audio_sha256,
                    source_size = excluded.source_size
                """,
                (
                    str(plan.path.expanduser().resolve()),
                    serialized,
                    _utc_now(),
                    plan.source_audio_sha256,
                    plan.source_size,
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

    def relocate(self, paths: Sequence[Path]) -> dict[Path, Path]:
        """Re-key plans whose file moved, matching on audio identity.

        A rename used to strand an analysis: the plan was keyed by path, the
        path no longer resolved, and the only report was "file is missing" for
        something sitting untouched one directory over. The audio digest
        identifies the track independently of where it lives, so the plan can
        follow it.

        Only a file that could plausibly be the missing one is hashed: same
        size, same directory, or same name. Size alone is not enough, because a
        rename often arrives together with a tag write that changes the file's
        length — that pairing is what a tag-based renamer does. Bounding the
        search by directory and name instead keeps the work near the hole the
        move left rather than scanning the library.

        A plan moves only when exactly one candidate matches it and it matches
        exactly one candidate. Anything ambiguous is left alone to be reported
        as before — a wrong relocation would attach an analysis to the wrong
        track, which is far worse than asking the user to rescan.

        Returns the ``{old: new}`` paths that moved, so callers can say so.
        """
        rows = self._identity_rows()
        orphans = [row for row in rows if not Path(row["path"]).is_file()]
        if not orphans:
            return {}

        claimed = {Path(row["path"]) for row in rows}
        candidates = [
            resolved
            for path in paths
            if (resolved := path.expanduser().resolve()) not in claimed and resolved.is_file()
        ]
        if not candidates:
            return {}

        by_size: dict[int, list[dict[str, Any]]] = {}
        by_parent: dict[Path, list[dict[str, Any]]] = {}
        by_name: dict[str, list[dict[str, Any]]] = {}
        for row in orphans:
            missing = Path(row["path"])
            by_size.setdefault(int(row["source_size"]), []).append(row)
            by_parent.setdefault(missing.parent, []).append(row)
            by_name.setdefault(missing.name, []).append(row)

        # Candidates per orphan and orphans per candidate, so a tie on either
        # side can be detected and skipped rather than resolved by luck.
        pairs: list[tuple[str, Path]] = []
        for candidate in candidates:
            possible = {
                row["path"]: row
                for row in (
                    *by_size.get(candidate.stat().st_size, ()),
                    *by_parent.get(candidate.parent, ()),
                    *by_name.get(candidate.name, ()),
                )
            }
            if not possible:
                continue
            digests: dict[str, str] = {}
            for row in possible.values():
                kind = "audio" if row["audio_sha256"] else "file"
                if kind not in digests:
                    digests[kind] = (
                        sha256_audio(candidate) if kind == "audio" else sha256_file(candidate)
                    )
                expected = row["audio_sha256"] or row["source_sha256"]
                if digests[kind] == expected:
                    pairs.append((str(row["path"]), candidate))

        orphan_counts = Counter(old for old, _ in pairs)
        candidate_counts = Counter(new for _, new in pairs)
        moved = {
            Path(old): new
            for old, new in pairs
            if orphan_counts[old] == 1 and candidate_counts[new] == 1
        }
        if moved:
            self._rekey(moved)
        return moved

    def _identity_rows(self) -> list[dict[str, Any]]:
        """Read each row's identity, backfilling from plan_json for schema 1 rows."""
        rows: list[dict[str, Any]] = []
        with closing(self._connect()) as connection:
            for row in connection.execute(
                "SELECT path, plan_json, audio_sha256, source_size FROM workbench_plans"
            ):
                audio = row["audio_sha256"]
                size = row["source_size"]
                source_sha256 = None
                if audio is None or size is None:
                    try:
                        source = json.loads(str(row["plan_json"])).get("source", {})
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if not isinstance(source, dict):
                        continue
                    audio = audio or source.get("audio_sha256")
                    size = size if size is not None else source.get("size")
                    source_sha256 = source.get("sha256")
                if not isinstance(size, int):
                    continue
                rows.append(
                    {
                        "path": str(row["path"]),
                        "audio_sha256": audio if isinstance(audio, str) else None,
                        "source_sha256": source_sha256 if isinstance(source_sha256, str) else None,
                        "source_size": size,
                    }
                )
        return rows

    def _rekey(self, moved: Mapping[Path, Path]) -> None:
        """Point rows at their new paths, keeping the embedded path in step.

        ``_decode`` rejects a row whose stored plan disagrees with its key, so
        both have to move together or the relocation destroys the row it just
        rescued.
        """
        with closing(self._connect()) as connection, connection:
            for old, new in moved.items():
                row = connection.execute(
                    "SELECT plan_json FROM workbench_plans WHERE path = ?", (str(old),)
                ).fetchone()
                if row is None:
                    continue
                try:
                    record = json.loads(str(row["plan_json"]))
                except json.JSONDecodeError:
                    continue
                record["path"] = str(new)
                connection.execute(
                    """
                    UPDATE workbench_plans
                    SET path = ?, plan_json = ?, updated_at = ?
                    WHERE path = ?
                    """,
                    (
                        str(new),
                        json.dumps(
                            record, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                        ),
                        _utc_now(),
                        str(old),
                    ),
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
        # Done here rather than at each call site because every caller wants it
        # and none of them knew to ask: the store is the only thing that can see
        # both the stranded rows and the paths that were just scanned.
        self.relocate(resolved_paths)

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
            # Schema 2 lifted the identity columns out of plan_json so a moved
            # file can be found without decoding every row. Added rather than
            # rebuilt: the existing rows are analyses the user paid for, and
            # they backfill themselves as each track is next saved.
            columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(workbench_plans)")
            }
            if "audio_sha256" not in columns:
                connection.execute("ALTER TABLE workbench_plans ADD COLUMN audio_sha256 TEXT")
            if "source_size" not in columns:
                connection.execute("ALTER TABLE workbench_plans ADD COLUMN source_size INTEGER")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS workbench_plans_audio ON workbench_plans(audio_sha256)"
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


def _audio_differs(plan: PlannedWrite) -> bool:
    """A v4 plan has no audio digest, so its whole-file one stands in."""
    if plan.source_audio_sha256 is None:
        return sha256_file(plan.path) != plan.source_sha256
    return sha256_audio(plan.path) != plan.source_audio_sha256


def _classify(
    plan: PlannedWrite,
    *,
    expected_model_ids: Mapping[AnalysisTask, str],
    expected_config_sha256: str,
    expected_config: Mapping[str, object] | None = None,
) -> WorkbenchEntry:
    if not plan.path.is_file():
        return WorkbenchEntry(plan, "stale", "source file is missing")

    # Size and mtime are the cheap prefilter, not the verdict. Any tag write moves
    # both without touching a sample, so confirm against the audio before
    # discarding an analysis that is still perfectly good.
    stat = plan.path.stat()
    if (
        stat.st_size != plan.source_size or stat.st_mtime_ns != plan.source_mtime_ns
    ) and _audio_differs(plan):
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
