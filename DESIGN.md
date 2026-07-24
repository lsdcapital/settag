# settag design

## Product boundary

```text
audio files → analysis → staged plan → verified native metadata write
                   │            │
                   ├─ Textual UI┤
                   └─ plain CLI ┘
```

SetTag is independent from SetPath. SetPath may consume the resulting metadata
but should not install, invoke, bundle, or write through SetTag.

## One workflow, two presentations

`src/settag/workflow.py` owns the reusable application operations:

- prepare a track and its evidence
- build a batch while isolating analysis errors
- preflight saved or in-memory plans
- apply and verify prepared writes
- persist compact plans

The Textual app and plain CLI are presentation and input adapters over those
operations. Metadata policy does not live in either UI.

When the first argument is a file or directory, it is normalized to
`run PATH`.

- If stdin and stdout are TTYs, `run` opens `SetTagApp`.
- If either is not a TTY, `run` is a plain dry run.
- `run --no-tui` explicitly selects the plain dry run.
- Named `analyze`, `inspect`, `preview`, and `apply` commands are always plain.

There is no second interactive menu or per-track prompt workflow. Textual is
the single interactive product.

## Interactive state model

The app has three distinct layers of state:

1. immutable ranked model evidence;
2. track inclusion in the pending batch;
3. an optional explicit standard-genre target for each track.

Including a track allows its SetTag evidence changes to be written. Inclusion
alone never stages a standard genre edit. `G` stages the primary model child
label directly, and `E`/`Enter` stages exact user input. Both edits are shown
as `before → after`.

The app keeps the track table primary and a persistent inspector secondary.
All write-affecting shortcuts are visible in the footer:

```text
Space include · A all · N none · G suggestion · E edit · S save · W write · Q quit
```

`W` performs preflight, opens one confirmation screen, performs preflight
again, then writes and verifies. Analysis errors disable batch writing.

## Safety invariants

1. Analysis and the default plain mode never write.
2. Interactive writes require `W` plus a batch confirmation.
3. Plain writes require `analyze --write`, confirmed `apply`, or `apply --yes`.
4. `apply --yes` bypasses only confirmation, never validation.
5. Only formats with an approved native metadata adapter are writable.
6. Ranked evidence is immutable in review and always written to SetTag-owned
   fields.
7. A conventional genre changes only when a separate target is explicitly
   staged for that track.
8. Selecting all tracks never creates standard-genre targets.
9. Predictions below the threshold are never selected merely to force a
   result.
10. Artwork, titles, artists, comments, and metadata owned by other tools are
    preserved.
11. Every completed write is reopened and verified against all SetTag values
    and the expected conventional genre.
12. Every input produces an analysis or error record when an output stream is
    requested.

## Pipeline

```text
scan → fingerprint → infer → select → plan → stage → preflight → confirm
                                                       ↑           │
                                                       └───────────┘
                                                                   ↓
                                                            write → verify
```

The model is constructed once per invocation and reused across tracks.
Failures are isolated during analysis. A run returns non-zero if any input
fails.

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

- `SETTAG_GENRE`: selected Discogs519 labels in ranked order
- `SETTAG_GENRE_SCORES`: compact JSON with the same labels, order, and scores
- `SETTAG_VERSION`: SetTag version
- `SETTAG_MODEL`: model-pair identifier
- `SETTAG_ANALYZED_AT`: UTC analysis time
- `SETTAG_CONFIG_SHA256`: selection-configuration fingerprint

The full 519-label prediction vector is retained in `settag.analysis/v1`
output and debug logging. Only selected labels and scores are embedded in
audio metadata.

When reanalysis selects no label, stale `SETTAG_GENRE` and
`SETTAG_GENRE_SCORES` values are removed. Provenance fields are still updated.

Scores are mean sigmoid activations across audio patches. They are suitable
for ranking and thresholding but are not demonstrated calibrated
probabilities.

## Complete analysis record

`settag.analysis/v1` records the source fingerprint, analysis time, backend,
exact model files and SHA-256 values, selection configuration, full
predictions, selected evidence, native SetTag plan, and write result.

The immediate `analyze --write` path remains evidence-only. It does not stage
or write a conventional genre.

Failed records use `settag.error/v1` and never claim analysis or writing
succeeded.

## Compact plan record

Current compact plans use `settag.plan/v2`:

```json
{
  "schema": "settag.plan/v2",
  "path": "/absolute/path/track.mp3",
  "source": {
    "sha256": "...",
    "size": 12345678,
    "mtime_ns": 1750000000000000000
  },
  "file_genre": [],
  "target_file_genre": ["Progressive House"],
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
    "file_genre": "File genre: None → Progressive House"
  }
}
```

`file_genre` is the observed safety precondition.
`target_file_genre` is either `null` (preserve it), an array of desired values,
or an empty array (explicitly clear it). SetTag and conventional changes are
serialized separately.

`analyze --plan` writes v2 with a null target. The Textual app may save an
explicit target. `load_plan` remains compatible with evidence-only
`settag.plan/v1` files.

Failed tracks use `settag.plan-error/v1`. A file containing any error record
cannot be applied.

## Deferred work

- analysis cache and resume
- concurrent decoding with controlled model lifecycle
- Ogg Vorbis and Opus
- APEv2 formats such as WavPack, Monkey's Audio, and Musepack
- ASF/WMA
- analysis-only support for decodable but unwritable containers
- mood/theme models
- curated taxonomy search and aliases beyond direct user input
