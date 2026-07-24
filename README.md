# settag

SetTag is a terminal-first genre assistant for DJ music libraries. It analyzes
audio with Essentia's Discogs519 MAEST models, shows ranked genre evidence
beside the file's existing genre, and lets you stage and verify metadata
changes before writing.

The default experience is a Textual app. The same executable also provides a
plain, non-TUI CLI for scripts, redirected output, saved plans, and CI.

Supported files:

- MP3, AIFF, and WAV with ID3 metadata
- FLAC with Vorbis comments
- M4A, M4B, and MP4 with MP4 atoms

## Install

Python 3.10–3.14 and [uv](https://docs.astral.sh/uv/) are recommended:

```sh
uv sync
uv run settag models download
```

Models are downloaded once into `~/.cache/settag/models`. They are not bundled
with this repository or its Python distributions.

## Run the app

Give SetTag a track or a directory:

```sh
uv run settag "/path/to/track.mp3"
uv run settag "/path/to/music/library"
```

In an interactive terminal this opens the Textual app:

```text
 [x] Track                       File genre     Suggested          Score  Changes
 [x] Eli & Dani - What Do...     None           Progressive House  0.664        6

 Standard file genre
   None (unchanged)

 Ranked model evidence
    1. Electronic---Progressive House  0.664
    2. Electronic---Techno             0.269
```

The main keys are always visible in the footer:

| Key | Action |
|---|---|
| `↑` / `↓` | Move through tracks |
| `Space` | Include or exclude the current track |
| `A` / `N` | Include all changed tracks / include none |
| `G` | Stage the primary model suggestion as this track's standard genre |
| `E` or `Enter` | Edit this track's standard genre |
| `S` | Save the included tracks as a reusable JSONL plan |
| `W` | Preflight, confirm once, write, and verify |
| `Q` | Quit without writing |

Tracks with SetTag changes are initially included. Bulk selection controls
which tracks will be written; it never stages a standard genre edit. A
standard genre changes only after `G` or an explicit edit, and the
`before → after` value remains visible in the list and inspector.

`G` performs one transparent conversion only:

```text
Electronic---Progressive House → Progressive House
```

It removes the Discogs parent prefix. It does not silently map to Beatport,
SetPath, or another taxonomy. Use `E` to enter the exact value you want.

`W` runs a complete preflight, shows one batch confirmation, runs preflight
again, writes each file through its format-native adapter, and reopens it to
verify the result. Analysis errors disable writing for the batch.

Selection defaults can be adjusted when needed:

```sh
uv run settag "/path/to/music" --top 5 --threshold 0.10
```

## Plain CLI mode

When stdin or stdout is not a terminal, `settag PATH` automatically becomes a
plain dry run. It never prompts or writes. Force that mode in a terminal with:

```sh
uv run settag "/path/to/music" --no-tui
```

The named commands are also plain CLI commands:

```sh
# Human-readable dry-run logs
uv run settag analyze "/path/to/music"

# Complete machine-readable audit records
uv run settag analyze "/path/to/music" --output analysis.jsonl

# Read current tags without loading the model
uv run settag inspect "/path/to/music"

# Create, preview, and apply a durable plan
uv run settag analyze "/path/to/music" --plan settag-plan.jsonl
uv run settag preview settag-plan.jsonl
uv run settag apply settag-plan.jsonl
```

For deliberate automation, `apply --yes` suppresses only the confirmation.
It does not suppress source, metadata, or plan validation:

```sh
uv run settag apply settag-plan.jsonl --yes
```

An immediate evidence-only write is also available:

```sh
uv run settag analyze "/path/to/music" \
  --write \
  --output applied.jsonl
```

`analyze --write` updates SetTag-owned evidence only. Standard genre editing is
available through the app or a saved v2 plan because it must be staged
explicitly per track.

### Logging

Normal `INFO` output is compact:

```text
INFO [1/1] /path/to/track.mp3
INFO   file genre tag: House (unchanged)
INFO   SetTag genres: none -> Electronic---House score 0.765
INFO   dry run: 6 SetTag fields would change; nothing written
```

The complete 519-label record is written with `--output` or exposed through
debug logging:

```sh
LOG_LEVEL=DEBUG uv run settag analyze "/path/to/track.mp3"
```

`LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.
Essentia's known one-time Abseil/MLIR startup messages are filtered separately
because native TensorFlow code writes them before Python logging starts.

## Metadata safety

SetTag-owned model evidence and a conventional file genre are separate:

| Purpose | ID3 | FLAC | MP4 |
|---|---|---|---|
| Ranked SetTag evidence | `TXXX:SETTAG_*` | `SETTAG_*` | `----:com.lsdcapital.settag:*` |
| Standard file genre | `TCON` | `GENRE` | `©gen` |

SetTag owns these logical evidence fields:

- `SETTAG_GENRE`
- `SETTAG_GENRE_SCORES`
- `SETTAG_VERSION`
- `SETTAG_MODEL`
- `SETTAG_ANALYZED_AT`
- `SETTAG_CONFIG_SHA256`

`SETTAG_GENRE` is the selected ranked label list. `SETTAG_GENRE_SCORES` is one
compact JSON value containing the same labels, order, and scores, so metadata
consumers such as SetPath retain the complete evidence.

The standard genre is not part of the SetTag namespace. The app can edit it,
but only through a distinct per-track staged action. Title, artist, album,
comments, artwork, duplicate fields, and metadata owned by other software are
preserved.

Before any batch write, SetTag verifies:

1. source SHA-256, size, and observed metadata state;
2. the detected native adapter;
3. every reconstructed SetTag change;
4. any explicitly staged standard genre change.

After each write it reopens the file and verifies both the evidence and the
expected standard genre. A batch preflight is all-or-nothing, although native
file writes cannot form one transaction across multiple files.

## Saved plans

`settag.plan/v2` is the compact review and write contract. It records the
observed file genre separately from an optional staged target:

```json
{
  "schema": "settag.plan/v2",
  "path": "/path/to/music/track.mp3",
  "source": {
    "sha256": "b7b118125b3289157da76212b54c2e1f91b4db2c3c0ff1bca4094c4d0046ed23",
    "size": 12605465,
    "mtime_ns": 1633515033000000000
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
    "config_sha256": "3935b3e0c51c85b750ee8cffc471b7a8e0a5a4cec60b336dde42fd321d40b5e6"
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

Plans produced by `analyze --plan` set `target_file_genre` to `null`. Plans
saved from the app retain explicit edits. `preview` is the built-in
human-readable renderer; users do not need `jq`.

Every apply performs preflight both before and after confirmation. A source
change or an analysis-error record rejects the whole plan before writing.
Legacy `settag.plan/v1` evidence-only plans remain readable.

## Scores and model

The pinned model pair is:

- embedding: `discogs-maest-30s-pw-519l-2`
- classifier: `genre_discogs519-discogs-maest-30s-pw-519l-1`

Each score is the mean class-wise sigmoid activation across analyzed audio
patches. It is useful as model confidence for ranking and thresholding, but it
is not demonstrated to be a calibrated probability. Scores do not need to sum
to `1`.

Predictions below the threshold are not selected merely to force a non-empty
result:

```sh
uv run settag analyze "/path/to/music" --top 5 --threshold 0.10
```

## Licensing

SetTag source is licensed under AGPL-3.0-only. The default inference workflow
also uses separately licensed components and model files:

- `essentia-tensorflow` is AGPL-3.0-only; UPF also offers proprietary licensing
- TensorFlow runtime is Apache-2.0
- Essentia model weights and metadata are offered under CC BY-NC-ND 4.0 for
  non-commercial use, with proprietary licensing available

The source is open source, while the default model workflow is intended for
non-commercial use and is not a wholly OSI-open-source stack. Downloading the
models separately does not change their licence.

Exact attributions and dependency notices are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

See:

- <https://essentia.upf.edu/licensing_information.html>
- <https://essentia.upf.edu/models.html>

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv build
```

The tests use Textual's headless app runner and synthetic audio; they do not
run model inference. A real-audio smoke test requires downloaded models:

```sh
uv run settag models status
uv run settag "/path/to/track.flac"
```

Architecture and safety contracts are documented in [DESIGN.md](DESIGN.md).
