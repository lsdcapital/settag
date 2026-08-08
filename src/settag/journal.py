"""Durable record of every metadata write, so writes can be reverted.

SetTag changes files in a music library. The workbench database is a disposable
cache, so the journal deliberately lives in its own database: clearing the
workbench to recover from a problem must never destroy undo history.

A journal entry stores the complete SetTag-owned bundle, the conventional
genre tag, and any explicitly cleaned hygiene fields exactly as they were
immediately before the write, which is what ``settag undo`` restores.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from settag.plans import (
    friendly_change,
    friendly_hygiene_change,
    friendly_standard_genre_change,
    standard_genre_field,
)
from settag.records import utc_now
from settag.tags import OWNED_DESCRIPTIONS, OwnedValues, TagChange

JOURNAL_SCHEMA_VERSION = 1
WRITE_RECORD_SCHEMA = "settag.write/v2"
READABLE_WRITE_RECORD_SCHEMAS = frozenset({"settag.write/v1", WRITE_RECORD_SCHEMA})
DEFAULT_RETENTION_DAYS = 90


class JournalError(RuntimeError):
    pass


def default_journal_db() -> Path:
    override = os.environ.get("SETTAG_JOURNAL_DB")
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
    return (base / "settag" / "journal.sqlite3").resolve()


DEFAULT_JOURNAL_DB = default_journal_db()


def new_batch_id() -> str:
    stamp = utc_now().replace("-", "").replace(":", "").removesuffix("Z")
    return f"{stamp}-{uuid4().hex[:8]}"


@dataclass(frozen=True)
class WriteRecord:
    """One file's verified metadata write, with the state it replaced."""

    path: Path
    metadata_format: str
    owned_before: OwnedValues
    owned_after: OwnedValues
    standard_before: tuple[str, ...]
    standard_after: tuple[str, ...] | None
    #: Digest of the file as it was *before* the write. Provenance only — it identifies the
    #: content that was replaced, so a DJ restoring from their own backup can confirm they
    #: are looking at the same pre-write file. It deliberately does not gate undo: it cannot
    #: describe the current file, and re-hashing a whole library to check would cost minutes.
    sha256_before: str
    #: Size and mtime as SetTag left the file. These are what `undo` checks, so a file another
    #: tool has touched since is reported rather than silently overwritten. Cheaper than a
    #: digest and sufficient in practice; a rewrite preserving both exactly would slip past.
    size_after: int
    mtime_ns_after: int
    written_at: str
    hygiene_before: dict[str, list[str] | None] = field(default_factory=dict)
    hygiene_after: dict[str, list[str] | None] = field(default_factory=dict)

    @property
    def owned_changes(self) -> tuple[TagChange, ...]:
        return tuple(
            TagChange(
                field=description,
                before=self.owned_before.get(description),
                after=self.owned_after.get(description),
            )
            for description in OWNED_DESCRIPTIONS
            if self.owned_before.get(description) != self.owned_after.get(description)
        )

    @property
    def standard_genre_change(self) -> TagChange | None:
        if self.standard_after is None or self.standard_after == self.standard_before:
            return None
        return TagChange(
            field=standard_genre_field(self.metadata_format),
            before=list(self.standard_before) or None,
            after=list(self.standard_after) or None,
        )

    @property
    def hygiene_changes(self) -> tuple[TagChange, ...]:
        return tuple(
            TagChange(
                field=name,
                before=self.hygiene_before.get(name),
                after=self.hygiene_after.get(name),
            )
            for name in sorted(self.hygiene_before.keys() | self.hygiene_after.keys())
            if self.hygiene_before.get(name) != self.hygiene_after.get(name)
        )

    @property
    def readable_changes(self) -> tuple[str, ...]:
        """Describe what the write did, in the same words the review screen used."""
        lines = [friendly_change(change) for change in self.owned_changes]
        standard_change = self.standard_genre_change
        if standard_change is not None:
            lines.append(friendly_standard_genre_change(standard_change))
        lines.extend(friendly_hygiene_change(change) for change in self.hygiene_changes)
        return tuple(lines)

    def to_record(self) -> dict[str, object]:
        return {
            "schema": WRITE_RECORD_SCHEMA,
            "path": str(self.path),
            "metadata_format": self.metadata_format,
            "owned_before": self.owned_before,
            "owned_after": self.owned_after,
            "standard_before": list(self.standard_before),
            "standard_after": None if self.standard_after is None else list(self.standard_after),
            "hygiene_before": self.hygiene_before,
            "hygiene_after": self.hygiene_after,
            "sha256_before": self.sha256_before,
            "size_after": self.size_after,
            "mtime_ns_after": self.mtime_ns_after,
            "written_at": self.written_at,
        }


@dataclass(frozen=True)
class JournalBatch:
    """Every file written by one apply operation."""

    batch_id: str
    started_at: str
    entries: tuple[WriteRecord, ...]
    reverted_at: str | None = None

    @property
    def track_count(self) -> int:
        return len(self.entries)

    @property
    def standard_genre_count(self) -> int:
        return sum(entry.standard_genre_change is not None for entry in self.entries)

    @property
    def hygiene_count(self) -> int:
        return sum(len(entry.hygiene_changes) for entry in self.entries)

    @property
    def summary(self) -> str:
        tracks = f"{self.track_count} track{'s' if self.track_count != 1 else ''}"
        genres = self.standard_genre_count
        details: list[str] = []
        if genres:
            details.append(f"{genres} file genre edit{'s' if genres != 1 else ''}")
        if self.hygiene_count:
            details.append(
                f"{self.hygiene_count} tag cleanup{'s' if self.hygiene_count != 1 else ''}"
            )
        detail = f", {', '.join(details)}" if details else ""
        reverted = " (already reverted)" if self.reverted_at is not None else ""
        return f"{tracks}{detail}{reverted}"


class WriteJournal:
    def __init__(self, path: Path = DEFAULT_JOURNAL_DB) -> None:
        self.path = path.expanduser().resolve()

    def record(self, batch_id: str, entry: WriteRecord) -> None:
        """Append one completed write. Called only after the file is verified."""
        serialized = json.dumps(
            entry.to_record(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with closing(self._connect()) as connection, connection:
            # rowcount distinguishes the first entry of a new batch from later ones, which is
            # where retention is enforced: once per batch rather than once per file.
            started = (
                connection.execute(
                    """
                    INSERT INTO write_batches(batch_id, started_at)
                    VALUES (?, ?)
                    ON CONFLICT(batch_id) DO NOTHING
                    """,
                    (batch_id, entry.written_at),
                ).rowcount
                == 1
            )
            connection.execute(
                """
                INSERT INTO write_entries(batch_id, path, record_json, written_at)
                VALUES (?, ?, ?, ?)
                """,
                (batch_id, str(entry.path), serialized, entry.written_at),
            )
        if started:
            self._prune_quietly(keep=batch_id)

    def _prune_quietly(self, *, keep: str) -> None:
        """Enforce the retention window without ever letting housekeeping fail a write.

        Run outside the insert transaction and deliberately silent: a journal that cannot
        trim old batches is a disk-space problem, while a write that fails because trimming
        failed would be a data problem. The record has already been committed by this point.
        """
        with suppress(JournalError, sqlite3.Error, OSError):
            self.prune(keep=keep)

    def recent(self, limit: int = 20) -> tuple[JournalBatch, ...]:
        """Return the most recent batches, newest first."""
        if limit < 1:
            return ()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT batch_id, started_at, reverted_at
                FROM write_batches
                ORDER BY started_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return tuple(
                self._batch_from_row(row, self._entries(connection, str(row["batch_id"])))
                for row in rows
            )

    def batch(self, batch_id: str) -> JournalBatch | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT batch_id, started_at, reverted_at
                FROM write_batches
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            if row is None:
                return None
            return self._batch_from_row(row, self._entries(connection, batch_id))

    def latest(self) -> JournalBatch | None:
        batches = self.recent(limit=1)
        return batches[0] if batches else None

    def mark_reverted(self, batch_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE write_batches SET reverted_at = ? WHERE batch_id = ?",
                (utc_now(), batch_id),
            )

    def prune(self, days: int = DEFAULT_RETENTION_DAYS, *, keep: str | None = None) -> int:
        """Drop batches older than the retention window. Returns batches removed.

        ``keep`` protects one batch from removal regardless of its age. Retention runs when a
        batch starts, and a machine whose clock is behind the retention window would otherwise
        delete the very write it just recorded.
        """
        cutoff = _days_ago(days)
        with closing(self._connect()) as connection, connection:
            batch_ids = [
                str(row["batch_id"])
                for row in connection.execute(
                    "SELECT batch_id FROM write_batches WHERE started_at < ? AND batch_id IS NOT ?",
                    (cutoff, keep),
                )
            ]
            if not batch_ids:
                return 0
            placeholders = ",".join("?" for _ in batch_ids)
            connection.execute(
                f"DELETE FROM write_entries WHERE batch_id IN ({placeholders})",
                tuple(batch_ids),
            )
            connection.execute(
                f"DELETE FROM write_batches WHERE batch_id IN ({placeholders})",
                tuple(batch_ids),
            )
        return len(batch_ids)

    def _entries(self, connection: sqlite3.Connection, batch_id: str) -> tuple[WriteRecord, ...]:
        rows = connection.execute(
            """
            SELECT record_json
            FROM write_entries
            WHERE batch_id = ?
            ORDER BY rowid
            """,
            (batch_id,),
        )
        return tuple(self._decode(str(row["record_json"]), batch_id) for row in rows)

    def _batch_from_row(
        self,
        row: sqlite3.Row,
        entries: Sequence[WriteRecord],
    ) -> JournalBatch:
        reverted = row["reverted_at"]
        return JournalBatch(
            batch_id=str(row["batch_id"]),
            started_at=str(row["started_at"]),
            entries=tuple(entries),
            reverted_at=None if reverted is None else str(reverted),
        )

    def _decode(self, serialized: str, batch_id: str) -> WriteRecord:
        location = f"{self.path}:{batch_id}"
        try:
            value = json.loads(serialized)
        except json.JSONDecodeError as error:
            raise JournalError(f"Invalid journal entry in {location}: {error}") from error
        return write_record_from_record(value, location=location)

    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > JOURNAL_SCHEMA_VERSION:
                connection.close()
                raise JournalError(
                    f"Write journal schema {version} is newer than "
                    f"this SetTag supports ({JOURNAL_SCHEMA_VERSION}): {self.path}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS write_batches (
                    batch_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    reverted_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS write_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    written_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS write_entries_batch ON write_entries(batch_id)"
            )
            if version < JOURNAL_SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version = {JOURNAL_SCHEMA_VERSION}")
            connection.commit()
            return connection
        except (OSError, sqlite3.Error) as error:
            raise JournalError(
                f"Could not open SetTag write journal {self.path}: {error}"
            ) from error


class BatchRecorder:
    """Records one apply operation, absorbing storage failures.

    A write that reached the file successfully must never be reported as failed
    because the journal could not be updated, so this never raises. Callers read
    ``error`` afterwards and mention it alongside an otherwise successful write.
    """

    def __init__(self, journal: WriteJournal, batch_id: str | None = None) -> None:
        self.journal = journal
        self.batch_id = batch_id or new_batch_id()
        self.recorded = 0
        self.errors: list[str] = []

    def __call__(self, entry: WriteRecord) -> None:
        try:
            self.journal.record(self.batch_id, entry)
        except JournalError as error:
            self.errors.append(str(error))
            return
        self.recorded += 1

    @property
    def error(self) -> str | None:
        if not self.errors:
            return None
        failed = len(self.errors)
        return (
            f"{failed} write{'s' if failed != 1 else ''} could not be journaled "
            f"and cannot be undone: {self.errors[0]}"
        )


def write_record_from_record(value: object, *, location: str) -> WriteRecord:
    record = _mapping(value, location)
    schema = record.get("schema")
    if schema not in READABLE_WRITE_RECORD_SCHEMAS:
        raise JournalError(f"{location}: unsupported schema {schema!r}")
    standard_after = record.get("standard_after")
    return WriteRecord(
        path=Path(_string(record.get("path"), f"{location}.path")),
        metadata_format=_string(record.get("metadata_format"), f"{location}.metadata_format"),
        owned_before=_owned(record.get("owned_before"), f"{location}.owned_before"),
        owned_after=_owned(record.get("owned_after"), f"{location}.owned_after"),
        standard_before=_genres(record.get("standard_before"), f"{location}.standard_before"),
        standard_after=(
            None
            if standard_after is None
            else _genres(standard_after, f"{location}.standard_after")
        ),
        hygiene_before=_hygiene_values(
            record.get("hygiene_before"),
            f"{location}.hygiene_before",
        ),
        hygiene_after=_hygiene_values(
            record.get("hygiene_after"),
            f"{location}.hygiene_after",
        ),
        sha256_before=_string(record.get("sha256_before"), f"{location}.sha256_before"),
        size_after=_int(record.get("size_after"), f"{location}.size_after"),
        mtime_ns_after=_int(record.get("mtime_ns_after"), f"{location}.mtime_ns_after"),
        written_at=_string(record.get("written_at"), f"{location}.written_at"),
    )


def _mapping(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise JournalError(f"{location}: expected an object")
    return {str(key): item for key, item in value.items()}


def _string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise JournalError(f"{location}: expected a non-empty string")
    return value


def _int(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise JournalError(f"{location}: expected an integer")
    return value


def _genres(value: object, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise JournalError(f"{location}: expected an array of strings")
    return tuple(str(item) for item in value)


def _owned(value: object, location: str) -> OwnedValues:
    record = _mapping(value, location)
    owned: OwnedValues = {}
    for description in OWNED_DESCRIPTIONS:
        item = record.get(description)
        if item is None:
            owned[description] = None
            continue
        owned[description] = list(_genres(item, f"{location}.{description}"))
    return owned


def _hygiene_values(value: object, location: str) -> dict[str, list[str] | None]:
    if value is None:
        return {}
    record = _mapping(value, location)
    result: dict[str, list[str] | None] = {}
    for name, item in record.items():
        result[name] = None if item is None else list(_genres(item, f"{location}.{name}"))
    return result


def _days_ago(days: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
