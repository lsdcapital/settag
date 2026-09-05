"""Beatport enrichment plans using the existing verified, journalled write path."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Protocol

from settag.beatport import (
    Candidate,
    LookupStopped,
    PublicPageProvider,
    TrackIdentity,
    identity_conflicts,
    normalized,
)
from settag.freshness import catalog_current, enrichment_record, record_values
from settag.hashing import sha256_file, sha256_json
from settag.plans import PlannedWrite, friendly_change, stage_default_file_genre
from settag.policy import select_predictions
from settag.records import ProvenanceStatus, read_task_provenance_status
from settag.tags import (
    OWNED_DESCRIPTIONS,
    owned_tag_store,
    read_task_provenance,
    task_evidence_from_owned,
    track_identity,
    track_identity_sha256,
)
from settag.tasks import AnalysisTask
from settag.workflow import (
    AnalysisBatch,
    AnalysisFailure,
    CancelCallback,
    ProgressCallback,
    inspect_track,
)


class EnrichmentCancelled(Exception):
    pass


class EnrichmentProvider(Protocol):
    def candidates(self, track: TrackIdentity) -> Sequence[Candidate]: ...

    def details(self, candidate: Candidate) -> Sequence[Candidate]: ...


def genre_evidence(
    track: TrackIdentity,
    provider: EnrichmentProvider,
    should_cancel: CancelCallback | None = None,
) -> dict[str, object] | None:
    """Retain source disagreement without promoting alternatives to accepted genres."""
    if not track.title or not any(track.artists):
        return None
    matches = {
        c.track_id: c for c in provider.candidates(track) if not identity_conflicts(track, c)
    }
    if not matches:
        return None
    verified: list[Candidate] = []
    for candidate in matches.values():
        if should_cancel is not None and should_cancel():
            raise EnrichmentCancelled
        details = [candidate] if candidate.detail_page else provider.details(candidate)
        exact = [
            c
            for c in details
            if c.track_id == candidate.track_id
            and c.detail_page
            and not identity_conflicts(track, c)
        ]
        if len(exact) != 1 or not exact[0].genres:
            # A missing/conflicting detail cannot silently turn a split result into consensus.
            raise LookupStopped("A matching release could not be verified on its detail page")
        verified.append(exact[0])
    verified.sort(key=lambda c: c.track_id)
    genre_sets = [{normalized(g) for g in c.genres} for c in verified]
    agreed = set.intersection(*genre_sets)
    labels = {normalized(g): g for c in verified for g in c.genres}
    consensus = all(g == genre_sets[0] for g in genre_sets)
    return {
        "schema": "settag.beatport/v1",
        "status": "consensus" if consensus else "conflicting_genres",
        "agreed_genres": [labels[g] for g in sorted(agreed)],
        "alternative_genres": [labels[g] for g in sorted(set(labels) - agreed)],
        "sources": [{**asdict(c), "url": c.url} for c in verified],
    }


def enrichment_plan(
    path: Path, provider: EnrichmentProvider, should_cancel: CancelCallback | None = None
) -> tuple[PlannedWrite | None, dict[str, object]]:
    path = path.expanduser().resolve()
    before = path.stat()
    digest = sha256_file(path)
    store = owned_tag_store(path)
    current = {key: store.read_value(key) for key in OWNED_DESCRIPTIONS}
    genres = store.genre_state().standard
    identity = track_identity(store)
    if isinstance(provider, PublicPageProvider):
        provider.oldest_observation = None
    evidence = genre_evidence(identity, provider, should_cancel)
    row: dict[str, object] = {
        "path": str(path),
        "existing_genres": list(genres),
        "identity": asdict(identity),
    }
    if evidence is None:
        if not identity.title or not any(identity.artists):
            return None, {
                **row,
                "status": "missing_identity",
                "reason": "Artist/title missing; catalog lookup could not run",
            }
        return None, {**row, "status": "unresolved", "reason": "No verified identity match"}
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or sha256_file(
        path
    ) != digest:
        raise ValueError("File changed during enrichment; run again")
    # Stable payload makes cached repeat runs no-ops. Observation times belong to page cache.
    desired = dict(current)
    desired["SETTAG_BEATPORT"] = [json.dumps(evidence, ensure_ascii=False, sort_keys=True)]
    changes = store.plan(desired)
    item = PlannedWrite(
        path=path,
        source_sha256=digest,
        source_size=before.st_size,
        source_mtime_ns=before.st_mtime_ns,
        file_genre=genres,
        evidence=task_evidence_from_owned(current).get("genre", ()),
        selected=(),
        desired=desired,
        metadata_format=changes.format,
        owned_changes=tuple(friendly_change(c) for c in changes.changes),
        source_owned_sha256=sha256_json(current),
    )
    return item, {**row, **evidence, "changes": list(item.readable_changes)}


class EnrichmentLoader:
    """One source-independent operation for the app and unattended runs.

    Reuse current audio evidence, merge fresh catalog evidence, and keep either
    source's useful result if the other is unavailable. No tag writes occur here.
    """

    def __init__(
        self,
        audio_loader: Callable[[Sequence[Path], ProgressCallback, CancelCallback], AnalysisBatch],
        *,
        provider: EnrichmentProvider | None = None,
        expected_model_ids: Mapping[AnalysisTask, str] | None = None,
        expected_config: Mapping[str, object] | None = None,
        top: int = 5,
        threshold: float = 0.1,
        cached_audio: Callable[[Path], PlannedWrite | None] | None = None,
    ) -> None:
        self.audio_loader = audio_loader
        self.provider = provider or PublicPageProvider(default_cache_dir(), max_requests=300)
        self.expected_model_ids = expected_model_ids
        self.expected_config = expected_config
        self.top = top
        self.threshold = threshold
        self.stopped_reason = ""
        self.cached_audio = cached_audio

    def begin_batch(self) -> None:
        self.stopped_reason = ""
        if isinstance(self.provider, PublicPageProvider):
            self.provider.requests = 0
            self.provider.cache_hits = 0

    def __call__(
        self, paths: Sequence[Path], on_progress: ProgressCallback, should_cancel: CancelCallback
    ) -> AnalysisBatch:
        planned: list[PlannedWrite] = []
        failures: list[AnalysisFailure] = []
        for index, path in enumerate(paths, 1):
            if should_cancel():
                break
            try:
                item = self._track(path, should_cancel)
                if item is not None:
                    planned.append(item)
            except Exception as error:
                failures.append(AnalysisFailure(path, type(error).__name__, str(error)))
            on_progress(index, len(paths), path)
        return AnalysisBatch(tuple(planned), tuple(failures), cancelled=should_cancel())

    def _track(self, path: Path, should_cancel: CancelCallback) -> PlannedWrite | None:
        notes: list[str] = []
        item = self._current_audio(path)
        if item is None:
            try:
                batch = self.audio_loader((path,), lambda *_args: None, should_cancel)
                item = next(iter(batch.planned), None)
                notes.extend(f"Audio analysis: {f.description}" for f in batch.failures)
                if batch.cancelled and item is None:
                    return None
            except Exception as error:
                notes.append(f"Audio analysis unavailable: {error}")
        audio_complete = item is not None
        if should_cancel():
            return self._finish(
                item, audio_complete, {"status": "unavailable", "reason": "Cancelled"}, notes
            )
        catalog: PlannedWrite | None = None
        catalog_info: dict[str, object]
        store = owned_tag_store(path) if item is None else None
        current = (
            item.desired
            if item is not None
            else {
                key: store.read_value(key) if store is not None else None
                for key in OWNED_DESCRIPTIONS
            }
        )
        identity_hash = track_identity_sha256(owned_tag_store(path))
        if catalog_current(current, identity_sha256=identity_hash):
            record = enrichment_record(current)
            assert record is not None
            catalog_info = record["catalog"]
            if item is None:
                assert store is not None
                stat = path.stat()
                item = PlannedWrite(
                    path=path.resolve(),
                    source_sha256=sha256_file(path),
                    source_size=stat.st_size,
                    source_mtime_ns=stat.st_mtime_ns,
                    file_genre=store.genre_state().standard,
                    evidence=task_evidence_from_owned(current).get("genre", ()),
                    selected=(),
                    desired=current,
                    metadata_format=store.format_name,
                    owned_changes=(),
                    source_owned_sha256=sha256_json(current),
                )
        elif self.stopped_reason:
            catalog_info = {"status": "unavailable", "reason": self.stopped_reason}
            notes.append(f"Beatport unavailable: {self.stopped_reason}; retry enrichment later.")
        else:
            try:
                catalog, outcome = enrichment_plan(path, self.provider, should_cancel)
                checked_at = getattr(self.provider, "oldest_observation", None)
                catalog_info = {
                    "status": "matched" if catalog else "no_match",
                    "checked_at": time.time() if checked_at is None else checked_at,
                    "identity_sha256": identity_hash,
                }
                if outcome["status"] == "missing_identity":
                    catalog_info = {"status": "unavailable", "reason": outcome["reason"]}
                    notes.append(str(outcome["reason"]))
                elif catalog is None:
                    notes.append("Beatport: no verified match found; existing evidence retained.")
            except EnrichmentCancelled:
                notes.append("Enrichment cancelled before catalog lookup completed.")
                return self._finish(
                    item, audio_complete, {"status": "unavailable", "reason": "Cancelled"}, notes
                )
            except LookupStopped as error:
                self.stopped_reason = str(error)
                catalog_info = {"status": "unavailable", "reason": str(error)}
                notes.append(f"Beatport unavailable: {error}; retry enrichment later.")
            except Exception as error:
                catalog_info = {"status": "unavailable", "reason": str(error)}
                notes.append(f"Beatport lookup failed: {error}; existing evidence retained.")
        if catalog is not None:
            if item is None:
                item = catalog
            else:
                if item.source_sha256 != catalog.source_sha256:
                    raise ValueError("File changed between enrichment sources; retry this track")
                item = replace(
                    item,
                    desired={**item.desired, "SETTAG_BEATPORT": catalog.desired["SETTAG_BEATPORT"]},
                )
        if item is None:
            raise ValueError("; ".join(notes) or "No enrichment source returned a result")
        return self._finish(item, audio_complete, catalog_info, notes)

    @staticmethod
    def _finish(
        item: PlannedWrite | None,
        audio_complete: bool,
        catalog_info: dict[str, object],
        notes: list[str],
    ) -> PlannedWrite | None:
        if item is None:
            return None
        desired = {
            **item.desired,
            "SETTAG_ENRICHMENT": record_values(
                audio_complete=audio_complete,
                catalog=catalog_info,
            ),
        }
        changes = owned_tag_store(item.path).plan(desired)
        return stage_default_file_genre(
            replace(
                item,
                desired=desired,
                notices=tuple(notes),
                owned_changes=tuple(friendly_change(c) for c in changes.changes),
            )
        )

    def _current_audio(self, path: Path) -> PlannedWrite | None:
        if self.expected_model_ids is None or self.expected_config is None:
            return None
        metadata = inspect_track(
            path,
            expected_model_ids=self.expected_model_ids,
            expected_config_sha256=str(self.expected_config["sha256"]),
            expected_config=self.expected_config,
        )
        cached = self.cached_audio(path) if self.cached_audio is not None else None
        if cached is not None and cached.source_sha256 == sha256_file(path):
            provenance = read_task_provenance(cached.desired)
            if all(
                read_task_provenance_status(
                    provenance.get(task),
                    task=task,
                    expected_model_id=model,
                    expected_config_sha256=str(self.expected_config["sha256"]),
                    expected_config=self.expected_config,
                ).status
                is ProvenanceStatus.CURRENT
                for task, model in self.expected_model_ids.items()
            ):
                return replace(cached, source_owned_sha256=sha256_json(metadata.owned))
        if metadata.status != "current":
            return None
        before = path.stat()
        evidence = task_evidence_from_owned(metadata.owned).get("genre", ())
        return PlannedWrite(
            path=path,
            source_sha256=sha256_file(path),
            source_size=before.st_size,
            source_mtime_ns=before.st_mtime_ns,
            file_genre=metadata.genre_state.standard,
            evidence=evidence,
            selected=tuple(select_predictions(evidence, top=self.top, threshold=self.threshold)),
            desired=metadata.owned,
            metadata_format=owned_tag_store(path).format_name,
            owned_changes=(),
            source_owned_sha256=sha256_json(metadata.owned),
        )


def default_cache_dir() -> Path:
    return Path(os.environ.get("SETTAG_BEATPORT_CACHE", "~/.cache/settag/beatport")).expanduser()
