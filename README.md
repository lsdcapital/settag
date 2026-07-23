# settag

`settag` is a small, analysis-first genre tagger for DJ music libraries. It
analyzes audio with Essentia's 2025 Discogs519 MAEST models, displays a compact
change-plan summary, and can save the complete result as JSONL. It changes an
audio file only when `--write` is supplied.

The first release is intentionally narrow:

- MP3, FLAC, M4A/M4B/MP4, AIFF, and WAV files
- genre analysis only
- the 519-label Discogs taxonomy
- concise console output with optional complete JSONL records
- namespaced, format-native SetTag metadata only
- no interactive mode, watcher, daemon, web UI, or SetPath integration

It never writes or replaces standard genre fields such as ID3 `TCON`, Vorbis
`GENRE`, or MP4 `©gen`.

## Install

Python 3.10–3.14 and `uv` are recommended:

```sh
uv sync
uv run settag models download
```

The model command downloads weights and metadata directly from Essentia into
`~/.cache/settag/models`. Models are not included in this repository.

## Quick start: dry run

Run these commands from the repository root:

```sh
# Install dependencies
uv sync

# Download the Essentia models once
uv run settag models download
```

Dry-run a single audio file:

```sh
uv run settag analyze "/path/to/track.flac"
```

Or recursively analyze every supported audio file in a directory and save the
JSONL plan:

```sh
uv run settag analyze "/path/to/music/library" \
  --output analysis.jsonl
```

Analysis is a dry run by default. There is no `--dry-run` flag: audio files
are changed only when `--write` is explicitly supplied.

Review a saved plan with:

```sh
jq . analysis.jsonl
```

Useful selection options:

```sh
uv run settag analyze "/path/to/music/library" \
  --top 5 \
  --threshold 0.10 \
  --output analysis.jsonl
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
INFO   standard genre: House (unchanged)
INFO   SetTag genres: none -> Electronic---Tropical House 58.4%, Electronic---House 54.6%
INFO   dry run: 6 SetTag fields would change; nothing written
```

It does not print all 519 model activations.

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

## Write settag-owned fields

Review the JSONL plan first, then repeat with the explicit write flag:

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

Existing title, artist, album, standard genre, comments, artwork, and fields
owned by other tools are left untouched. Reanalysis may replace or remove
SetTag-owned fields so they continue to describe the current result.

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
