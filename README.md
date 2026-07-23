# settag

`settag` is a small, analysis-first genre tagger for DJ music libraries. It
analyzes MP3 audio with Essentia's 2025 Discogs519 MAEST models and emits a
JSONL change plan. It changes an audio file only when `--write` is supplied.

The first release is intentionally narrow:

- MP3 files only
- genre analysis only
- the 519-label Discogs taxonomy
- JSONL output for every attempted file
- namespaced ID3 `TXXX` fields only
- no interactive mode, watcher, daemon, web UI, or SetPath integration

It never writes or replaces the standard `GENRE` (`TCON`) field.

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

Dry-run a single MP3:

```sh
uv run settag analyze "/path/to/track.mp3"
```

Or recursively analyze every MP3 in a directory and save the JSONL plan:

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

Only MP3 files are currently supported.

## Analyze without changing files

```sh
uv run settag analyze /path/to/music --output analysis.jsonl
```

For a single track:

```sh
uv run settag analyze "/path/to/track.mp3"
```

The default output is JSONL on stdout. Diagnostics go to stderr, so output can
also be redirected safely:

```sh
uv run settag analyze /path/to/music > analysis.jsonl
```

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

Only these ID3 `TXXX` descriptions are owned and updated by `settag`:

- `SETTAG_GENRE`
- `SETTAG_GENRE_SCORES`
- `SETTAG_MODEL`
- `SETTAG_ANALYZED_AT`
- `SETTAG_CONFIG_SHA256`

Existing title, artist, album, standard genre, comments, artwork, and fields
owned by other tools are left untouched. Reanalysis may replace or remove
`settag`-owned fields so they continue to describe the current result.

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

The `settag` source code is licensed under AGPL-3.0-only.

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
downloaded models and a local MP3:

```sh
uv run settag models status
uv run settag analyze /path/to/track.mp3
```

Design details and the JSONL/tag contracts are in [DESIGN.md](DESIGN.md).
