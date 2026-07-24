# settag

`settag` is a small, analysis-first genre tagger for DJ music libraries. It
analyzes audio with Essentia's 2025 Discogs519 MAEST models, displays a compact
change-plan summary, and can save either a compact review plan or the complete
analysis as JSONL. It changes an audio file only through explicit writing,
interactive review, or confirmation of a verified batch plan.

The first release is intentionally narrow:

- MP3, FLAC, M4A/M4B/MP4, AIFF, and WAV files
- genre analysis only
- the 519-label Discogs taxonomy
- concise console output with optional complete JSONL records
- namespaced, format-native SetTag metadata only
- no watcher, daemon, web UI, or SetPath integration

It never writes or replaces the conventional file genre tag: ID3 `TCON`,
Vorbis `GENRE`, or MP4 `©gen`. These are standard metadata locations, not a
standardized genre vocabulary.

## Install

Python 3.10–3.14 and `uv` are recommended:

```sh
uv sync
uv run settag models download
```

The model command downloads weights and metadata directly from Essentia into
`~/.cache/settag/models`. Models are not included in this repository.

## Quick start

Run these commands from the repository root:

```sh
# Install dependencies
uv sync

# Download the Essentia models once
uv run settag models download
```

Then give SetTag a file or directory:

```sh
uv run settag "/path/to/track.flac"
uv run settag "/path/to/music/library"
```

The default command scans, analyzes, and presents a terminal summary without
writing anything. In an interactive terminal it offers one small menu:

```text
[v] view  [w] write  [s] save plan  [q] quit
Choice [q]:
```

Pressing Enter quits without writing. Choosing `w` runs a full preflight and
then asks for one explicit confirmation before any SetTag-owned metadata is
changed. Choosing `s` saves a timestamped reusable JSONL plan in the current
directory.

When output is redirected or stdin is not interactive, `settag PATH` becomes a
plain dry run: it prints the readable summary, never prompts, and never writes.
Rich terminal color and progress are automatically disabled when unsuitable;
`NO_COLOR` also disables color explicitly.

Selection defaults can still be adjusted when needed:

```sh
uv run settag "/path/to/music/library" \
  --top 5 \
  --threshold 0.10
```

Check whether the required models are installed:

```sh
uv run settag models status
```

### Supported formats

SetTag uses the parsed Mutagen container type to choose an approved writer:

| Files | Native SetTag metadata |
|---|---|
| MP3, AIFF, WAV | ID3v2 `TXXX:SETTAG_*` frames |
| FLAC | Vorbis comments named `SETTAG_*` |
| M4A, M4B, MP4 | MP4 freeform atoms under `com.lsdcapital.settag` |

The scanner recognizes `.mp3`, `.flac`, `.m4a`, `.m4b`, `.mp4`, `.aif`,
`.aiff`, `.wav`, and `.wave`. Raw AAC and other containers remain unsupported
for metadata writes even if Essentia could decode their audio.

## Analyze without changing files

The `analyze` subcommand is the advanced non-interactive interface for audit
files, scripts, and explicit plan paths:

```sh
uv run settag analyze /path/to/music --output analysis.jsonl
```

For a single track:

```sh
uv run settag analyze "/path/to/track.mp3"
```

The default `INFO` output shows the path, selected genres, scores, and number
of planned SetTag changes. It also makes the preservation boundary explicit:

```text
INFO   file genre tag: House (unchanged)
INFO   SetTag genres: none -> Electronic---Tropical House score 0.584, Electronic---House score 0.546
INFO   dry run: 6 SetTag fields would change; nothing written
```

It does not print all 519 model activations.

Each displayed score is the mean sigmoid activation across the analyzed audio
patches. Scores are useful for ranking and thresholding, but they are not
calibrated probabilities and do not need to sum to `1`.

Use `--output` for a complete, machine-readable JSONL audit record:

```sh
uv run settag analyze /path/to/music --output analysis.jsonl
```

Or enable debug logging to inspect the complete record in the console:

```sh
LOG_LEVEL=DEBUG uv run settag analyze "/path/to/track.mp3"
```

`LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`
(case-insensitive). Debug logs and normal progress go to stderr; a file passed
with `--output` contains JSONL only.

Essentia's TensorFlow backend may also print a one-time Abseil/MLIR startup
message directly from native code. That message is harmless and is not
controlled by Python's `LOG_LEVEL`.

Predictions must meet the threshold. `settag` does not force a top prediction:

```sh
uv run settag analyze /path/to/music --top 5 --threshold 0.10
```

## Batch plan and one-time approval

For automation, a named artifact, or review across separate sessions, split
expensive analysis from approval explicitly:

```sh
uv run settag analyze "/path/to/music/library" \
  --plan settag-plan.jsonl

uv run settag preview settag-plan.jsonl

uv run settag apply settag-plan.jsonl
```

JSONL keeps one track on each physical line as the durable machine-readable
artifact. `settag preview` renders it for people; no external JSON tool is
required. The preview shows each track's existing file genre tag, ranked
selected labels and scores, plain-English metadata changes, model, and native
metadata format. It ends with an aggregate summary and the exact apply command.

The underlying compact plan record contains the same review data plus safety
fingerprints:

```json
{
  "schema": "settag.plan/v1",
  "path": "/path/to/music/track.mp3",
  "source": {
    "sha256": "b7b118125b3289157da76212b54c2e1f91b4db2c3c0ff1bca4094c4d0046ed23",
    "size": 12605465,
    "mtime_ns": 1633515033000000000
  },
  "file_genre": [
    "House"
  ],
  "selected": [
    {
      "label": "Electronic---House",
      "score": 0.765
    },
    {
      "label": "Electronic---Deep House",
      "score": 0.564
    }
  ],
  "metadata_format": "id3",
  "provenance": {
    "settag_version": "0.1.0",
    "model": "essentia/genre-discogs519-maest/v1",
    "analyzed_at": "2026-07-23T16:22:53Z",
    "config_sha256": "3935b3e0c51c85b750ee8cffc471b7a8e0a5a4cec60b336dde42fd321d40b5e6"
  },
  "changes": [
    "Genre labels: 0 → 2",
    "Ranked score data: add",
    "SetTag version: not set → 0.1.0",
    "Analysis model: not set → essentia/genre-d…"
  ]
}
```

The plan deliberately omits all 519 raw model activations and the duplicated
native tag payload. Use `--output analysis.jsonl` when the complete audit
record is also required; `--plan` is the smaller review artifact and
`preview` is its built-in human renderer. The two analysis output options can
be used together as long as they name different files.

`apply` performs a full preflight before showing one confirmation for the
whole plan. It verifies every source SHA-256, existing file genre tag,
metadata format, and proposed SetTag change. It then performs the preflight
again after confirmation and writes the exact saved evidence without rerunning
Essentia:

```text
Batch write plan
/path/to/settag-plan.jsonl

  Tracks reviewed:        13
  Files to write:         13
  SetTag field changes:   78
  Selected label scores:  65
  Empty file genre tags:  4

Every source SHA-256 and metadata plan matches the reviewed file.
File genre tags and unrelated metadata will remain unchanged.

Apply this exact plan to 13 files? [y] yes  [n] no >
```

If any planned file changed after analysis, nothing is written during
preflight. A plan containing an analysis-error record is also rejected in
full. Once writing starts, cross-file writes cannot be transactional: an
unexpected change or write failure stops the run and reports how many earlier
files were already written and verified.

For deliberate non-interactive use, skip the single prompt with:

```sh
uv run settag apply settag-plan.jsonl --yes
```

## Review and confirm each write

Interactive review analyzes each track once, displays the proposed SetTag
evidence, and waits for a decision:

```sh
uv run settag analyze "/path/to/music" --review
```

Review mode uses a plain decision screen rather than log-formatted summaries:

```text
Review 1 of 1
track.mp3
/path/to/music

File genre tag
  House (will not be changed)

SetTag model evidence
   1. Electronic---Progressive House  score 0.664
   2. Electronic---Neo Trance         score 0.391

Metadata change (1)
  Analysis time: 2026-07-22T12:00:00Z → 2026-07-23T12:00:00Z

Write this SetTag metadata? [y] write  [n] skip  [q] quit >
```

If the file genre tag is empty, the screen identifies the highest-ranked
selected label as a suggested candidate. It remains display-only: SetTag does
not copy a Discogs519 prediction into the file genre tag. Canonical
taxonomy mapping and human acceptance belong downstream.

The prompt accepts:

- `y`: write and verify this track's SetTag-owned fields
- `n`: decline this track and continue
- `q`: leave this track unchanged and end the run
- `Ctrl-C`: interrupt the run cleanly with nothing written for the current track

`--review` requires an interactive terminal, so it cannot silently wait for
input in a pipeline. Tracks whose write is accepted are reopened immediately
to verify all SetTag-owned values and confirm that the file genre tag is
unchanged.

When `--output` is also supplied, each JSONL record reports `written`,
`declined`, `cancelled`, `interrupted`, or `unchanged` as appropriate.

## Write settag-owned fields

Use `--write` when immediate non-interactive analysis and writing is preferred
to saving and approving a reusable plan:

```sh
uv run settag analyze /path/to/music \
  --write \
  --output applied.jsonl
```

Only these logical fields are owned and updated by SetTag:

- `SETTAG_GENRE`
- `SETTAG_GENRE_SCORES`
- `SETTAG_VERSION`
- `SETTAG_MODEL`
- `SETTAG_ANALYZED_AT`
- `SETTAG_CONFIG_SHA256`

Each writer maps those fields to its format-native namespace. For example,
`SETTAG_GENRE` is `TXXX:SETTAG_GENRE` in ID3, `SETTAG_GENRE` in FLAC Vorbis
comments, and `----:com.lsdcapital.settag:GENRE` in MP4.
`SETTAG_GENRE` and the decoded `SETTAG_GENRE_SCORES` entries always contain
the same selected labels in the same order.

Existing title, artist, album, file genre tag, comments, artwork, and fields
owned by other tools are left untouched. Reanalysis may replace or remove
SetTag-owned fields so they continue to describe the current result.

`--write` and `--review` are mutually exclusive.

## Inspect existing tags

Inspect the file genre tag and SetTag-owned metadata without loading the model
or analyzing the audio:

```sh
uv run settag inspect "/path/to/track.mp3"
uv run settag inspect "/path/to/music"
```

This shows stored genre scores and the SetTag version, model, analysis time,
and configuration hash. It does not change files.

## Model

The initial model pair is:

- embedding: `discogs-maest-30s-pw-519l-2` (released 2025-01-22)
- classifier: `genre_discogs519-discogs-maest-30s-pw-519l-1`
  (released 2025-01-22)

The pairing and TensorFlow node names follow Essentia's official model
catalogue. Each JSONL record includes exact model filenames and local SHA-256
digests.

Mood is deliberately excluded. Essentia's currently documented mood/theme
head uses the older Discogs-EffNet embedding stack; including it would retain
the older model family and add a second inference pipeline.

## Licensing

The `settag` source code is licensed under AGPL-3.0-only and is intended to be
distributed as open-source software. The current pretrained-model workflow is
intended for non-commercial use because the model files have separate terms.

Essentia and its models have their own terms. Essentia's official licensing
page currently describes:

- Essentia under AGPLv3 for non-commercial applications, with a commercial
  licence available
- pretrained models under CC BY-NC-ND 4.0 for non-commercial use, with a
  proprietary licence available

Downloading models at setup time avoids redistributing the model files; it
does not change their licence or make commercial use permissible. Anyone
using or distributing this tool remains responsible for complying with
Essentia, model, and transitive dependency licences.

See:

- <https://essentia.upf.edu/licensing_information.html>
- <https://essentia.upf.edu/models.html>

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
```

The unit tests do not run model inference. A real-audio smoke test requires
downloaded models and a local supported audio file:

```sh
uv run settag models status
uv run settag analyze /path/to/track.flac
```

Design details and the JSONL/tag contracts are in [DESIGN.md](DESIGN.md).
