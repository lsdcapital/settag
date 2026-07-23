# settag design

## Product boundary

```text
audio files → settag analysis/change plan → optional settag-owned ID3 tags
                                                    ↓
                                          any metadata consumer
```

`settag` is independent from SetPath. SetPath should remain a read-only
metadata consumer and should not install, invoke, bundle, or depend on this
tool.

## Safety invariants

1. Analysis is the default; writing requires `--write`.
2. Only MP3 files are accepted.
3. Only `TXXX` frames with a `SETTAG_` description are changed.
4. Standard `TCON`/`GENRE` is never changed.
5. Predictions below the configured threshold are never selected merely to
   ensure a non-empty result.
6. Existing multi-value fields and all metadata owned by other software are
   preserved.
7. Every result states the model files, their SHA-256 digests, the analysis
   time, and a hash of the selection configuration.
8. Every attempted input emits either an analysis record or an error record.

## Pipeline

```text
scan → fingerprint → infer → select → plan → optionally apply → emit JSONL
```

The model is loaded once per CLI invocation and reused for all tracks.
Inference failures are isolated to the affected record so a directory scan can
continue. A run exits non-zero when any track fails.

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

Paths are absolute because records may be imported from a different working
directory. Model activations are recorded as model outputs, not as calibrated
probabilities.

`source.sha256` fingerprints the file before any requested write. Written
records also contain `write.result_sha256`, which fingerprints the resulting
file after the ID3 update.

## Tag contract

`SETTAG_GENRE` is a multi-value list containing only selected Discogs519
labels. `SETTAG_GENRE_SCORES` is compact JSON containing the selected label
and activation pairs. `SETTAG_MODEL` identifies the model pair.
`SETTAG_ANALYZED_AT` is UTC. `SETTAG_CONFIG_SHA256` identifies the selection
configuration.

The full prediction ranking remains in JSONL rather than being embedded in the
audio file.

If reanalysis selects no genres, an existing `SETTAG_GENRE` and
`SETTAG_GENRE_SCORES` are removed. Provenance fields are still updated. This
prevents stale settag output from masquerading as a current result while
leaving all non-settag metadata alone.

## Deferred work

- analysis cache and resume support
- FLAC/Vorbis comments
- M4A/MP4 freeform atoms
- AIFF and WAV ID3 adapters
- mood/theme models
- explicit opt-in support for filling empty standard fields
- concurrency and worker model lifecycle
- a generic SetPath file-tag importer

These should be added only after the MP3/genre contract is exercised on a real
DJ library.
