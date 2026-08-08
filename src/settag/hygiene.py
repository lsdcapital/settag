"""Model-free inspection and review plans for suspicious text metadata."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from settag.hashing import sha256_file
from settag.journal import WriteRecord
from settag.records import utc_now
from settag.tags import (
    HygieneTag,
    HygieneValues,
    OwnedValues,
    TagChange,
    TagPlan,
    apply_metadata_tags,
    owned_tag_store,
    plan_hygiene_tags,
    read_genre_state,
    read_owned_values,
)

_WEB_ADDRESS = re.compile(
    r"(?ix)(?:\bhttps?://|\bwww\.)\S+|"
    r"(?<!@)\b(?:[a-z0-9](?:[a-z0-9-]{0,62})\.)+"
    r"(?:com|net|org|io|co|ru|to|cc|me|info|biz|xyz|site|club|music|download)\b"
)
_PROMOTIONAL_COMMENT = re.compile(
    r"(?ix)\b(?:downloaded\s+from|visit\s+(?:our\s+)?website|free\s+mp3|"
    r"music\s+download|provided\s+by|ripped\s+by)\b"
)

WriteCallback = Callable[[WriteRecord], None]
WriteProgressCallback = Callable[[int, int, Path], None]
HygieneProgressCallback = Callable[[int, int, Path], None]


@dataclass(frozen=True)
class HygieneFinding:
    """One deterministic suggestion, always reviewed before it is written."""

    field: str
    label: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def desired(self) -> list[str] | None:
        return list(self.after) or None

    @property
    def current_text(self) -> str:
        return " · ".join(value or "(empty)" for value in self.before) or "(empty tag)"

    @property
    def result_text(self) -> str:
        return " · ".join(self.after) if self.after else "Remove tag"

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)

    @property
    def change(self) -> TagChange:
        return TagChange(
            field=self.field,
            before=list(self.before),
            after=list(self.after) or None,
        )


@dataclass(frozen=True)
class HygieneTrack:
    path: Path
    metadata_format: str
    source_size: int
    source_mtime_ns: int
    findings: tuple[HygieneFinding, ...]


@dataclass(frozen=True)
class HygieneFailure:
    path: Path
    error_type: str
    message: str

    @property
    def description(self) -> str:
        return f"{self.error_type}: {self.message}"


@dataclass(frozen=True)
class HygieneBatch:
    tracks: tuple[HygieneTrack, ...]
    failures: tuple[HygieneFailure, ...]

    @property
    def finding_count(self) -> int:
        return sum(len(track.findings) for track in self.tracks)

    @property
    def affected_track_count(self) -> int:
        return sum(bool(track.findings) for track in self.tracks)

    @property
    def clean_track_count(self) -> int:
        return len(self.tracks) - self.affected_track_count


@dataclass(frozen=True)
class HygienePlan:
    path: Path
    metadata_format: str
    source_size: int
    source_mtime_ns: int
    findings: tuple[HygieneFinding, ...]

    @property
    def desired(self) -> HygieneValues:
        return {finding.field: finding.desired for finding in self.findings}

    @property
    def changes(self) -> tuple[TagChange, ...]:
        return tuple(finding.change for finding in self.findings)


@dataclass(frozen=True)
class PreparedHygiene:
    plan: HygienePlan
    tag_plan: TagPlan
    owned_before: OwnedValues
    standard_before: tuple[str, ...]
    source_sha256: str


@dataclass(frozen=True)
class HygieneTrackSummary:
    filename: str
    changes: tuple[str, ...]

    def render(self) -> str:
        return "\n".join((self.filename, *(f"  {change}" for change in self.changes)))


@dataclass(frozen=True)
class HygieneSummary:
    track_count: int
    finding_count: int
    tracks: tuple[HygieneTrackSummary, ...]

    @property
    def confirmation_title(self) -> str:
        return (
            f"Clean {self.finding_count} {_plural(self.finding_count, 'tag')} "
            f"in {self.track_count} {_plural(self.track_count, 'track')}?"
        )

    @property
    def confirmation_action(self) -> str:
        return f"Clean {self.track_count} {_plural(self.track_count, 'track')}"

    @property
    def confirmation_help(self) -> str:
        return (
            "Only the checked hygiene suggestions will change. Titles, artists, albums, "
            "artwork, genres, and SetTag evidence stay untouched. Every file is reopened "
            "and verified."
        )

    def confirmation_preview(self, *, limit: int = 3) -> str:
        visible = self.tracks[:limit]
        sections = [track.render() for track in visible]
        hidden = len(self.tracks) - len(visible)
        if hidden:
            sections.append(f"+ {hidden} more {_plural(hidden, 'track')}")
        sections.append(
            f"Batch total: {self.finding_count} {_plural(self.finding_count, 'tag cleanup')}"
        )
        return "\n\n".join(sections)


class PartialHygieneWriteError(RuntimeError):
    def __init__(self, completed: int, total: int, cause: BaseException) -> None:
        super().__init__(
            f"Hygiene write stopped after {completed} of {total} {_plural(total, 'file')}: {cause}"
        )
        self.completed = completed
        self.total = total
        self.cause = cause


def inspect_hygiene_path(path: Path) -> HygieneTrack:
    resolved = path.expanduser().resolve()
    stat = resolved.stat()
    store = owned_tag_store(resolved)
    findings = tuple(
        finding
        for tag in store.read_hygiene_tags()
        if (finding := hygiene_finding(tag)) is not None
    )
    return HygieneTrack(
        path=resolved,
        metadata_format=store.format_name,
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
        findings=findings,
    )


def inspect_hygiene_paths(
    paths: Sequence[Path],
    *,
    on_progress: HygieneProgressCallback | None = None,
) -> HygieneBatch:
    tracks: list[HygieneTrack] = []
    failures: list[HygieneFailure] = []
    total = len(paths)
    for index, path in enumerate(paths, start=1):
        try:
            tracks.append(inspect_hygiene_path(path))
        except Exception as error:
            failures.append(
                HygieneFailure(
                    path=path,
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )
        finally:
            if on_progress is not None:
                on_progress(index, total, path)
    return HygieneBatch(tracks=tuple(tracks), failures=tuple(failures))


def hygiene_finding(tag: HygieneTag) -> HygieneFinding | None:
    reasons: list[str] = []
    kept: list[str] = []
    seen: set[str] = set()

    if not tag.values:
        return HygieneFinding(
            field=tag.field,
            label=tag.label,
            before=(),
            after=(),
            reasons=("empty tag",),
        )

    if tag.category == "encoder":
        return HygieneFinding(
            field=tag.field,
            label=tag.label,
            before=tag.values,
            after=(),
            reasons=("generated encoder marker",),
        )

    for value in tag.values:
        normalized = value.strip().casefold()
        if not normalized:
            _add_reason(reasons, "empty value")
            continue
        if normalized in seen:
            _add_reason(reasons, "duplicate value")
            continue
        seen.add(normalized)
        if _WEB_ADDRESS.search(value):
            _add_reason(reasons, "contains a web address")
            continue
        if tag.category in {"comment", "text"} and _PROMOTIONAL_COMMENT.search(value):
            _add_reason(reasons, "promotional comment")
            continue
        kept.append(value)

    after = tuple(kept)
    if after == tag.values:
        return None
    return HygieneFinding(
        field=tag.field,
        label=tag.label,
        before=tag.values,
        after=after,
        reasons=tuple(reasons),
    )


def plan_hygiene_track(
    track: HygieneTrack,
    findings: Sequence[HygieneFinding] | None = None,
) -> HygienePlan:
    selected = tuple(track.findings if findings is None else findings)
    known = {finding.field: finding for finding in track.findings}
    if not selected:
        raise ValueError(f"No hygiene changes selected for {track.path}")
    for finding in selected:
        if known.get(finding.field) != finding:
            raise ValueError(f"Hygiene finding does not belong to {track.path}: {finding.label}")
    return HygienePlan(
        path=track.path,
        metadata_format=track.metadata_format,
        source_size=track.source_size,
        source_mtime_ns=track.source_mtime_ns,
        findings=selected,
    )


def summarize_hygiene(plans: Sequence[HygienePlan]) -> HygieneSummary:
    return HygieneSummary(
        track_count=len(plans),
        finding_count=sum(len(plan.findings) for plan in plans),
        tracks=tuple(
            HygieneTrackSummary(
                filename=plan.path.name,
                changes=tuple(
                    f"{finding.label}: {finding.result_text} — {finding.reason_text}"
                    for finding in plan.findings
                ),
            )
            for plan in plans
        ),
    )


def preflight_hygiene(plans: Sequence[HygienePlan]) -> tuple[PreparedHygiene, ...]:
    prepared: list[PreparedHygiene] = []
    errors: list[str] = []
    for plan in plans:
        try:
            if not plan.path.is_file():
                raise RuntimeError(f"file is missing: {plan.path}")
            stat = plan.path.stat()
            if stat.st_size != plan.source_size or stat.st_mtime_ns != plan.source_mtime_ns:
                raise RuntimeError(f"file changed since hygiene review: {plan.path}")
            current = plan_hygiene_tags(plan.path, plan.desired)
            expected = TagPlan(format=plan.metadata_format, changes=plan.changes)
            if current != expected:
                raise RuntimeError(f"planned hygiene changes do not match: {plan.path}")
            prepared.append(
                PreparedHygiene(
                    plan=plan,
                    tag_plan=current,
                    owned_before=read_owned_values(plan.path),
                    standard_before=read_genre_state(plan.path).standard,
                    source_sha256=sha256_file(plan.path),
                )
            )
        except Exception as error:
            errors.append(str(error))
    if errors:
        details = "\n  ".join(errors)
        raise RuntimeError(f"{len(errors)} stale or invalid track(s):\n  {details}")
    return tuple(prepared)


def apply_hygiene(
    prepared: Sequence[PreparedHygiene],
    *,
    on_progress: WriteProgressCallback | None = None,
    on_write: WriteCallback | None = None,
) -> int:
    total = len(prepared)
    completed = 0
    try:
        for item in prepared:
            plan = item.plan
            stat = plan.path.stat()
            if stat.st_size != plan.source_size or stat.st_mtime_ns != plan.source_mtime_ns:
                raise RuntimeError(f"File changed before its hygiene write: {plan.path}")
            apply_metadata_tags(
                plan.path,
                {},
                hygiene_values=plan.desired,
                expected_hygiene_plan=item.tag_plan,
            )
            completed += 1
            if on_write is not None:
                on_write(_write_record(item))
            if on_progress is not None:
                on_progress(completed, total, plan.path)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        raise PartialHygieneWriteError(completed, total, error) from error
    return completed


def _write_record(prepared: PreparedHygiene) -> WriteRecord:
    plan = prepared.plan
    stat = plan.path.stat()
    before: HygieneValues = {
        finding.field: list(finding.before) for finding in prepared.plan.findings
    }
    return WriteRecord(
        path=plan.path,
        metadata_format=plan.metadata_format,
        owned_before=dict(prepared.owned_before),
        owned_after=dict(prepared.owned_before),
        standard_before=prepared.standard_before,
        standard_after=None,
        sha256_before=prepared.source_sha256,
        size_after=stat.st_size,
        mtime_ns_after=stat.st_mtime_ns,
        written_at=utc_now(),
        hygiene_before=before,
        hygiene_after=dict(plan.desired),
    )


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _plural(count: int, singular: str) -> str:
    return singular if count == 1 else f"{singular}s"
