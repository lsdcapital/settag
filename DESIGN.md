# settag design

## Product boundary

```text
audio files → settag analysis/change plan → optional format-native SetTag metadata
                                                    ↓
                                           any metadata consumer
```

`settag` is independent from SetPath. SetPath should remain a read-only
metadata consumer and should not install, invoke, bundle, or depend on this
tool.

## Safety invariants

1. Analysis is the default; writing requires `--write`, an explicit per-track
   confirmation in `--review` mode, or confirmation of a fully verified
   `settag apply` plan.
2. Only formats with an approved SetTag metadata adapter are accepted.
3. Only fields in SetTag's native namespace are changed.
4. The conventional file genre tag—ID3 `TCON`, Vorbis `GENRE`, or MP4
   `©gen`—is never changed.
5. Predictions below the configured threshold are never selected merely to
   ensure a non-empty result.
6. Existing multi-value fields, artwork, and logical metadata owned by other
   software are preserved.
7. Every result states the model files, their SHA-256 digests, the analysis
   time, and a hash of the selection configuration.
8. Every attempted input produces either an analysis record or an error
   record; complete records are persisted with `--output` or exposed through
   debug logging.
9. A completed metadata write is reopened and verified against every
   SetTag-owned value; the file genre tag is also confirmed unchanged.

## Pipeline

```text
scan → fingerprint → infer → select → plan → optionally review/apply → verify
                                                                         ↓
                                                               summarize/JSONL
```

The model is loaded once per CLI invocation and reused for all tracks.
Inference failures are isolated to the affected record so a directory scan can
continue. A run exits non-zero when any track fails.

`--review` and `--write` are mutually exclusive. Review mode prompts only when
the plan contains changes: `y` applies the displayed plan, `n` records a
decline and continues, and `q` records a cancellation and ends the run. It
requires a TTY. `Ctrl-C` at the prompt records an interruption, writes nothing
for the current track, and exits with status 130. Human editing of predictions
is deliberately excluded because SetTag fields represent model evidence rather
than curated genre metadata.

When a track has no file genre tag, review mode may identify the highest-ranked
selected Discogs519 label as a display-only candidate. The write boundary does
not change: SetTag never copies that candidate into the file genre tag.
Canonical taxonomy mapping, authority decisions, and human acceptance belong
to a downstream consumer such as SetPath.

### Batch plan and apply

`analyze --plan PATH` saves a compact, review-oriented JSONL artifact without
writing metadata. A successful `settag.plan/v1` record contains:

- the absolute audio path and pre-analysis SHA-256, size, and modification time
- the existing file genre tag
- only the ranked labels and scores selected by the configured threshold
- the metadata adapter and analysis provenance
- concise human-readable descriptions of the proposed SetTag changes

The compact plan omits the other model activations and native tag payload.
`--output` remains the complete `settag.analysis/v1` audit stream and may be
used alongside `--plan` when the paths differ.

Every failed track produces a `settag.plan-error/v1` record. `apply` rejects
the entire file if any such record is present; a partial analysis is not an
applicable plan.

`apply` validates the complete plan before asking for one batch confirmation.
For every track it verifies the source SHA-256, file genre tag, detected
metadata format, and reconstructed change descriptions. Desired native values
are deterministically reconstructed from the reviewed selected evidence and
provenance; Essentia is not loaded or rerun.

After confirmation, the complete preflight runs again. The source hash is also
checked immediately before each individual write, and every completed write is
reopened and verified. `--yes` suppresses only the prompt, not validation.

Preflight is all-or-nothing, but the format writers cannot provide a
transaction across multiple files. If a source changes or a write fails after
the loop begins, processing stops immediately and reports the number of
earlier files already written.

Normal `INFO` logging contains a compact per-track summary. The complete record
is written as JSONL when `--output` is supplied and is also logged when
`LOG_LEVEL=DEBUG`. JSONL is an audit/data format rather than routine console
noise.

Essentia decoding and metadata writing are separate capabilities. The scanner
currently admits MP3, FLAC, M4A/M4B/MP4, AIFF, and WAV. After analysis, Mutagen
detects the actual metadata container and selects an approved adapter. A file
whose extension is recognized but whose metadata container is not cannot be
written.

## JSONL record

Successful records use `schema: "settag.analysis/v1"`:

```json
{
  "schema": "settag.analysis/v1",
  "source": {
    "path": "/absolute/path/track.mp3",
    "size": 12345678,
    "mtime_ns": 1750000000000000000,
    "sha256": "..."
  },
  "analyzed_at": "2026-07-23T12:34:56Z",
  "analyzer": {
    "name": "settag",
    "version": "0.1.0",
    "backend": "essentia-tensorflow",
    "backend_version": "..."
  },
  "model": {
    "id": "essentia/genre-discogs519-maest/v1",
    "files": {
      "embedding": {"name": "...pb", "sha256": "..."},
      "classifier": {"name": "...pb", "sha256": "..."},
      "classifier_metadata": {"name": "...json", "sha256": "..."}
    }
  },
  "config": {
    "top": 5,
    "threshold": 0.1,
    "sha256": "..."
  },
  "predictions": [
    {"label": "Electronic---Deep House", "score": 0.72}
  ],
  "selected": [
    {"label": "Electronic---Deep House", "score": 0.72}
  ],
  "tag_plan": {
    "format": "id3",
    "changes": [
      {
        "field": "TXXX:SETTAG_GENRE",
        "before": null,
        "after": ["Electronic---Deep House"]
      }
    ]
  },
  "write": {
    "requested": false,
    "status": "not_requested"
  }
}
```

Failed records use `schema: "settag.error/v1"` and contain the source path,
error type, and message. They never claim that analysis or writing succeeded.
Interactive review additionally uses `write.status` values `declined` and
`cancelled` when no write was approved, or `interrupted` when the prompt
received `Ctrl-C`.

Paths are absolute because records may be imported from a different working
directory. Each recorded score is the mean class-wise sigmoid activation
across the analyzed audio patches. It is a model confidence score suitable for
ranking and thresholding, not a demonstrated calibrated probability.

`source.sha256` fingerprints the file before any requested write. Written
records also contain `write.result_sha256`, which fingerprints the resulting
file after the native metadata update and verification.

## Batch plan JSONL

Successful review-plan records use `schema: "settag.plan/v1"`:

```json
{
  "schema": "settag.plan/v1",
  "path": "/absolute/path/track.mp3",
  "source": {
    "sha256": "...",
    "size": 12345678,
    "mtime_ns": 1750000000000000000
  },
  "file_genre": ["House"],
  "selected": [
    {"label": "Electronic---House", "score": 0.72}
  ],
  "metadata_format": "id3",
  "provenance": {
    "settag_version": "0.1.0",
    "model": "essentia/genre-discogs519-maest/v1",
    "analyzed_at": "2026-07-23T12:34:56Z",
    "config_sha256": "..."
  },
  "changes": [
    "Genre labels: 0 → 1",
    "Ranked score data: add"
  ]
}
```

Failed tracks use `schema: "settag.plan-error/v1"` with the absolute path and
error message. Plan records are one physical line each; pretty-printing is a
presentation concern and does not change the JSONL contract.

## Tag contract

`SETTAG_GENRE` is a multi-value list containing only selected Discogs519
labels. `SETTAG_GENRE_SCORES` is compact JSON containing the selected label
and activation pairs in exactly the same membership and order.
`SETTAG_VERSION` identifies the SetTag software version.
`SETTAG_MODEL` identifies the analysis model pair.
`SETTAG_ANALYZED_AT` is UTC. `SETTAG_CONFIG_SHA256` identifies the selection
configuration.

The native representations are:

| Adapter | Files | Representation |
|---|---|---|
| `id3` | MP3, AIFF, WAV | `TXXX:SETTAG_*` |
| `vorbis-comments` | FLAC | `SETTAG_*` comments |
| `mp4-freeform` | M4A, M4B, MP4 | `----:com.lsdcapital.settag:*` |

For MP4, the logical `SETTAG_` prefix is represented by the
`com.lsdcapital.settag` freeform namespace, so `SETTAG_MODEL` becomes
`----:com.lsdcapital.settag:MODEL`.

The full prediction ranking remains in JSONL and debug logging rather than
being embedded in the audio file or printed during a normal run.

If reanalysis selects no genres, an existing `SETTAG_GENRE` and
`SETTAG_GENRE_SCORES` are removed. Provenance fields are still updated. This
prevents stale settag output from masquerading as a current result while
leaving all non-settag metadata alone.

## Deferred work

- analysis cache and resume support
- Ogg Vorbis and Opus comments
- APEv2 formats such as WavPack, Monkey's Audio, and Musepack
- ASF/WMA attributes
- analysis-only support for decodable but non-writable containers
- mood/theme models
- explicit opt-in support for filling an empty file genre tag
- concurrency and worker model lifecycle

These should be added only after the format-native genre contract is exercised
on a real DJ library.
