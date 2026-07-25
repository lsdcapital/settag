# settag design

## Product boundary

```text
audio files → metadata scan → choose → analysis → staged plan → verified write
                         │          │       │     │
                         └──────── Textual UI ────┤
                                    │      ↕
                                    │ local SQLite workbench
                                    plain CLI / JSONL ────────┘
```

SetTag is independent from SetPath. SetPath may consume the resulting metadata
but should not install, invoke, bundle, or write through SetTag.

## One workflow, two presentations

`src/settag/workflow.py` owns the reusable application operations:

- inspect existing standard and SetTag metadata without loading the model
- classify metadata as never analyzed, up to date, needing reanalysis, or incomplete
- prepare a track and its evidence
- build a batch while isolating analysis errors
- preflight saved or in-memory plans
- apply and verify prepared writes, journaling each completed one
- preflight and apply an undo of a previous write
- persist compact plans

The Textual app and plain CLI are presentation and input adapters over those
operations. Metadata policy does not live in either UI.

Undo is deliberately built the same way: `preflight_undo` and `apply_undo`
mirror `preflight_plan` and `apply_prepared`, and both the `U` key in the app
and `settag undo` are adapters over them. Neither UI decides what an undo
restores, and the human-readable change lines come from one place
(`WriteRecord.readable_changes`, reusing `plans.friendly_change`) so the two
never drift.

### Where the presentations live

```text
settag/cli/     args      the accepted command grammar
                commands  dispatch and the work each command performs
                render    everything printed, prompted, or logged

settag/tui/     app       phases, selection, background work
                screens   modal dialogs
                table     column layout and row rendering
                entries   the per-track state a row displays
                style     the stylesheet
```

Both are packages named for the modules they replaced, so `settag.cli:main`
and existing imports resolve unchanged.

The rule that keeps the two honest: **no count or human-readable phrase
derived from `PlannedWrite`, `PreparedWrite`, `WriteRecord`, or
`MetadataTrack` belongs in `cli/` or `tui/`.** UI modules choose layout; the
domain layer decides what the numbers and words are. `WriteSummary` exists
because that rule was broken once — the CLI counted genre evidence while the
app counted every task, and one batch reported two different totals.

When the first argument is a file or directory, it is normalized to
`run PATH`.

- If stdin and stdout are TTYs, `run` opens `SetTagApp`.
- If either is not a TTY, `run` is a plain dry run.
- `run --no-tui` explicitly selects the plain dry run.
- Named `analyze`, `inspect`, `preview`, and `apply` commands are always plain.

There is no second interactive menu or per-track prompt workflow. Textual is
the single interactive product.

## Interactive state model

The app has two explicit phases:

1. **Choose:** read existing metadata, filter the library, and choose which
   tracks to analyze. No analyzer is constructed in this phase.
2. **Review:** inspect new ranked evidence, choose which staged changes to
   write, and optionally stage a conventional genre.

The choose phase classifies each track as `Never analyzed`, `Up to date`,
`Reanalyze (model/config changed)`, or `Incomplete metadata`. Tracks needing
analysis are preselected. Up-to-date tracks stay visible and unselected. The
library can be filtered to all tracks, tracks needing analysis, tracks missing
a conventional genre, or up-to-date tracks. Analysis consumes the intersection
of the current filtered view and its selected tracks; hidden selections are
never silently included.

Analysis is a serial background job layered over both phases. Its input batch
is frozen at start. Each completed track is staged, persisted to the workbench,
and exposed to review before the next track begins. The user can navigate the
library, inspect details, enter review with `V`, edit or save completed plans,
and write the completed snapshot while later tracks continue analyzing. An
in-flight track has no plan and cannot enter a write. Track-level concurrency
is deliberately avoided so background operation does not multiply model load.

The dense table combines analysis validity and date into one `Analysis`
column: `Never`, `Up to date · date`, `Reanalyze · date`,
`Incomplete · date`, or `New · date`. The details panel retains the full
status wording and timestamp.

The review phase has three distinct layers of state:

1. immutable ranked model evidence;
2. track inclusion in the pending write batch;
3. an optional staged standard-genre target for each track.

Changed tracks are checked for writing by default; `Space` checks or unchecks
the highlighted track. After a new analysis, an empty conventional genre
defaults to the conservative standard-genre suggestion; a non-empty genre is
never replaced automatically. No candidate above the review cutoff means no
default. `E` opens one genre screen where the user can enter or clear a value,
or explicitly use the model suggestion. House-family children use an explicit
allowlist to roll up to `House`; all other children retain their direct name.
The inspector shows any roll-up, and every edit is shown as `before → after`.

Completed interactive analysis is persisted before review in a local SQLite
workbench. On startup, the library remains the default view: a matching plan
skips inference and appears as `Ready · date`, with `V` available to open saved
results in review. A mismatched plan appears as `Reanalyze · date`. Returning
to the library does not preselect ready plans, though the user may select one
for deliberate reanalysis.

The app keeps the track table primary at full terminal width. The inspector is
secondary, hidden by default, and toggled with `I` without changing the cursor
or selection. It shows only candidates admitted by the current review policy,
followed by a count of additional ranked scores retained for importers. The
footer changes with the current phase:

```text
Choose: Space toggle · I details · A all/none · F filter · V review (when ready) · Enter/R analyze · Q quit
Analyzing in Library: I details · F filter · V review completed · Esc stop after current
Analyzing in Review: Space toggle · A all/none · I details · E genre · S save · Enter/W write completed · Esc stop after current
Review: Space toggle · A all/none · I details · E genre · S save · Enter/W write
```

SetTag follows SetPath's Booth Compass palette so both DJ tools read as one
family: green-tinted Booth Black and deck surfaces, cool equipment-like text,
and Ember Signal only for focus, action, and meaningful state. A selected
track is always marked with `✓`; color is supporting information, never the
only selection cue.

`W` performs preflight and opens one confirmation screen with `Write` focused
by default. `Enter` confirms and `Esc` returns to review. SetTag performs
preflight again, then writes and verifies. Analysis errors disable batch
writing. When analysis is still running, preflight snapshots only completed,
checked plans; later results cannot enter an already-confirmed write. A
successful write does not exit the app: written tracks become current library
entries, while any unwritten tracks remain in review.

## Local workbench

The workbench and embedded tags have different ownership:

- SQLite is SetTag's private, restartable working state.
- Audio tags are the portable published result consumed by other tools.
- JSONL plans are explicit export/apply artifacts.

The default SQLite path follows the platform application-data convention:

- macOS: `~/Library/Application Support/settag/state.sqlite3`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/settag/state.sqlite3`
- Windows: `%LOCALAPPDATA%\settag\state.sqlite3`

`SETTAG_STATE_DB` changes the default and `run --state-db PATH` overrides one
invocation. Plain CLI commands are stateless and never open the workbench.

Records reuse the validated `settag.plan/v4` representation. An upsert is
committed after every successful track analysis and after each staged standard
genre edit. A verified write deletes its corresponding entry. Persistence
failure leaves the in-memory review intact; cleanup failure after a verified
audio write is reported without reclassifying the audio write as failed.

On metadata load, cached plans are validated against source size and mtime,
model identifier, evidence-configuration hash, and the currently observed
standard genre. Review-only changes to the score cutoff or displayed-result
limit reuse the existing evidence. A true evidence mismatch retains the old
evidence for inspection but requires reanalysis. Current embedded SetTag
metadata is authoritative and causes an obsolete local entry to be removed.

## Write journal

The journal is durable history, not cache, so it is a separate database
(`journal.sqlite3`, `SETTAG_JOURNAL_DB`, `--journal-db PATH`) beside the
workbench. Clearing the workbench to recover from a problem must not destroy
the ability to undo a write.

One apply operation is one batch. Each entry stores the complete SetTag-owned
bundle and conventional genre exactly as they were before that file was
written, plus the size and mtime immediately after. The before-state is
captured during preflight and is trustworthy at write time because
`apply_prepared` rechecks the source SHA-256 and `apply_metadata_tags` rechecks
the plan before saving.

An entry is recorded only after a file is written and verified, so the journal
never claims a change that did not land. Recording is failure-absorbing: a
journal that cannot be written is surfaced as a warning beside an otherwise
successful write, never as a failed write.

Undo restores by rewriting the recorded before-state through the same verified
`apply_metadata_tags` path a normal write uses. Files whose size or mtime no
longer match what was recorded are skipped with a reason rather than restored,
using the same staleness signal the workbench cache uses; `--force` overrides.
This restores tag values, not bytes: mutagen rewrites the tag block on save, so
a reverted file does not regain its pre-write SHA-256.

## Safety invariants

1. Analysis and the default plain mode never write.
2. Opening the interactive app reads metadata only and does not load the model.
3. Only visible tracks explicitly selected in the current library filter are
   sent to the analyzer.
4. Interactive writes require `W` plus a batch confirmation.
5. Plain writes require a reviewed plan through confirmed `apply` or
   `apply --yes`; `analyze` never writes.
6. `apply --yes` bypasses only confirmation, never validation.
7. Only formats with an approved native metadata adapter are writable.
8. Ranked evidence is immutable in review and always written to SetTag-owned
   fields.
9. A conventional genre changes only through a separate staged target.
10. New TUI analysis may stage the conservative standard-genre suggestion only
    when the conventional genre is empty; the user can edit or clear it before
    writing.
11. Predictions below the review cutoff are never suggested merely to force a
    result, but may remain in the bounded evidence bundle for consumers.
12. Artwork, titles, artists, comments, and metadata owned by other tools are
    preserved.
13. Every completed write is reopened and verified against all SetTag values
    and the expected conventional genre.
14. Every completed interactive analysis is persisted for restart recovery.
15. Every input produces an analysis or error record when an output stream is
    requested.

## Pipeline

```text
scan → read tags → choose tracks → load model → infer → select → plan
                                                               ↓
                                    verify ← write ← confirm ← preflight
```

The Textual workflow starts one lightweight spawned analyzer process before
entering the terminal UI, then constructs the model there only on the first
analysis action and reuses it for later batches. Starting before Textual takes
over the terminal avoids platform-specific file-descriptor and thread hazards.
The Textual thread worker waits only on IPC so native inference cannot block
terminal rendering or input. The plain workflow constructs the model
immediately in its own process.
When MAEST and EffNet tasks are selected together, one 16 kHz decode feeds both
stacks; mood/theme and instrument share a single EffNet embedding pass. Every
required model artifact is verified against its pinned SHA-256 before the
analyzer is constructed.
Interactive cancellation is cooperative between tracks: the in-flight native
inference finishes, completed results remain reviewable, and unprocessed tracks
remain selected. Failures are isolated during analysis. A run returns non-zero
if any input fails.

Preflight verifies for every included track:

- the source exists and its SHA-256 matches
- the observed conventional genre still matches
- the parsed metadata adapter still matches
- reconstructed SetTag-owned changes match the plan
- any staged conventional genre change matches the plan

Preflight is all-or-nothing. Native files cannot form one transaction across a
directory, so a failure after writes begin stops immediately and reports the
number already completed.

## Metadata adapters

| Adapter | Files | SetTag evidence | Standard genre |
|---|---|---|---|
| `id3` | MP3, AIFF, WAV | `TXXX:SETTAG_*` | `TCON` |
| `vorbis-comments` | FLAC | `SETTAG_*` comments | `GENRE` |
| `mp4-freeform` | M4A, M4B, MP4 | `----:com.lsdcapital.settag:*` | `©gen` |

A combined write loads one native container, validates both planned layers,
updates SetTag fields plus the optional conventional genre, saves once, and
reopens the file for verification.

The scanner accepts `.mp3`, `.flac`, `.m4a`, `.m4b`, `.mp4`, `.aif`, `.aiff`,
`.wav`, and `.wave`. A recognized extension whose parsed metadata container is
unsupported remains unwritable.

## SetTag evidence contract

The owned logical fields are:

- `SETTAG_GENRE`: the bounded Discogs519 evidence labels in ranked order
- `SETTAG_GENRE_SCORES`: compact JSON with the same labels, order, and scores
- `SETTAG_MOOD_THEME`: bounded EffNet mood/theme labels in ranked order
- `SETTAG_MOOD_THEME_SCORES`: compact JSON with the same labels, order, and scores
- `SETTAG_INSTRUMENT`: bounded EffNet instrument labels in ranked order
- `SETTAG_INSTRUMENT_SCORES`: compact JSON with the same labels, order, and scores
- `SETTAG_VERSION`: SetTag version
- `SETTAG_MODEL`: model-pair identifier
- `SETTAG_ANALYZED_AT`: UTC analysis time
- `SETTAG_CONFIG_SHA256`: evidence-configuration fingerprint
- `SETTAG_PROVENANCE`: `settag.provenance/v2`, keyed by task with full model
  manifests, artifact digests, configuration, thresholds, and timestamps

`genre` is the default task and loads only MAEST. Explicit `mood-theme` and
`instrument` tasks share one Discogs-EffNet embedding pass and use their own
heads. Genre evidence is exclusively MAEST-derived. EffNet output cannot write
the SetTag or conventional genre fields.

Task updates are independent. A partial run replaces only the requested task
records and preserves valid evidence and provenance for other tasks. Audio
metadata stores the top 20 ranked results for each task without applying the
review cutoff.

Scores are mean sigmoid activations across audio patches. They are suitable
for ranking and applying a score cutoff but are not demonstrated calibrated
confidence or probabilities.

## Complete analysis record

`settag.analysis/v3` records the source fingerprint, analysis time, backend,
requested tasks, exact model files and SHA-256 values, evidence configuration,
review policy, task-keyed full predictions, bounded evidence, review-selected
subsets, and the native SetTag plan.

`analyze` never writes, so the record carries no write result. Every write goes
through a reviewed plan, `preflight_plan`, and `apply_prepared`.

Failed records use `settag.error/v1` and never claim analysis or writing
succeeded.

## Compact plan record

Current compact plans use `settag.plan/v4`:

```json
{
  "schema": "settag.plan/v4",
  "path": "/absolute/path/track.mp3",
  "source": {
    "sha256": "...",
    "size": 12345678,
    "mtime_ns": 1750000000000000000
  },
  "file_genre": [],
  "target_file_genre": ["House"],
  "evidence": [
    {
      "label": "Electronic---Progressive House",
      "score": 0.664
    }
  ],
  "selected": [
    {
      "label": "Electronic---Progressive House",
      "score": 0.664
    }
  ],
  "metadata_format": "id3",
  "provenance": {
    "settag_version": "0.1.0",
    "model": "essentia/genre-discogs519-maest/v1",
    "analyzed_at": "2026-07-24T12:34:56Z",
    "config_sha256": "..."
  },
  "changes": {
    "settag": [
      "Genre labels: 0 → 1",
      "Ranked score data: add"
    ],
    "file_genre": "File genre: None → House"
  }
}
```

`file_genre` is the observed safety precondition.
`target_file_genre` is either `null` (preserve it), an array of desired values,
or an empty array (explicitly clear it). SetTag and conventional changes are
serialized separately.

`analyze --plan` writes v4 with a null target. The Textual app may save an
explicit target. `settag.plan/v4` is the only accepted plan schema; earlier
drafts were never released and are rejected with an explicit error.

Failed tracks use `settag.plan-error/v1`. A file containing any error record
cannot be applied.

## Deferred work

- concurrent decoding with controlled model lifecycle
- Ogg Vorbis and Opus
- APEv2 formats such as WavPack, Monkey's Audio, and Musepack
- ASF/WMA
- analysis-only support for decodable but unwritable containers
- representative-library calibration of task-specific thresholds
- curated taxonomy search and aliases beyond direct user input
