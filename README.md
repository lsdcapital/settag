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
with this repository or its Python distributions. Review [Licensing](#licensing)
before using the default models in a professional, business, or
revenue-generating workflow.

## Run the app

Give SetTag a track or a directory:

```sh
uv run settag "/path/to/track.mp3"
uv run settag "/path/to/music/library"
```

In an interactive terminal this opens the Textual app as a full-width track
list. Press `I` whenever you want to toggle the details panel. With details
open, the library looks like this:

```text
  ✓  Track                       File genre  Analysis
  ✓  Eli & Dani - What Do...     None        Never
     Robin Schulz - Sugar.mp3    House       Up to date · 2026-07-23

 Current file metadata
   Standard genre: None
   SetTag status: Never analyzed
   Last analyzed: Never

 Selected for analysis.
 The audio model has not been loaded.
```

SetTag first scans existing metadata only. Tracks with no analysis, incomplete
metadata, or a changed model/config are selected by default. Up-to-date tracks
remain visible but unselected. Adjust the selection, then press `R` to load the
model and analyze only the selected tracks visible in the current filter.
Selections in other filtered views are not included.

The library keys are:

| Key | Action |
|---|---|
| `↑` / `↓` | Move through tracks |
| `Space` | Select or unselect the current track for analysis |
| `A` | Toggle all eligible tracks in the current view on or off |
| `I` | Show or hide details for the highlighted track |
| `F` | Cycle All, Needs analysis, Missing genre, and Up to date views |
| `Enter` / `R` | Analyze the selected tracks |
| `Esc` | Stop after the track currently being analyzed |
| `Q` | Quit |

Cancellation is cooperative because Essentia/TensorFlow inference cannot be
safely interrupted halfway through a track. Completed results remain available
for review and are saved in the local workbench; unprocessed tracks remain
selected for a later run. Cancellation never writes metadata.

After analysis, the app switches to review:

```text
  ✓  Track                       File genre    Analysis          Suggested          Score  Changes
  ✓  Eli & Dani - What Do...     None → House  New · 2026-07-23  Progressive House  0.664        7

 Standard file genre
   None → House (staged)
   Suggested roll-up: Progressive House → House

 Review candidates
   Score cutoff ≥ 0.10 · maximum 5
    1. Electronic---Progressive House  0.664
    2. Electronic---Techno             0.269
   18 additional ranked scores stored for importing apps.
```

The review keys are:

| Key | Action |
|---|---|
| `Space` | Include or exclude the current track from writing |
| `A` | Toggle all changed tracks on or off |
| `I` | Show or hide review candidates and staged changes |
| `E` | Set this track's standard genre, clear it, or use the suggestion |
| `S` | Save the included tracks as a reusable JSONL plan |
| `Enter` / `W` | Preflight, confirm once, write, and verify |
| `B` | Return to the metadata library to choose another analysis batch |
| `Q` | Quit |

Newly analyzed tracks with SetTag changes are checked for writing by default.
Press `Space` to check or uncheck the highlighted track.
When a newly analyzed track has no conventional genre, SetTag visibly stages
the conservative standard-genre suggestion there by default. It never replaces
a non-empty genre automatically. Use `E` to change the staged value or clear it
to preserve the empty genre; the `before → after` value remains visible in the
list and inspector. If no candidate clears the review cutoff, the genre remains
empty.

The automatic default and the editor's `Use suggestion` action remove the
Discogs parent prefix. For an explicit allowlist of House-family labels, they
also roll the detailed child label up to the stable conventional genre `House`:

```text
Electronic---Progressive House → House
Electronic---Tropical House    → House
```

The inspector shows this transformation. Other model children keep their
direct name; SetTag deliberately does not infer a family from a suffix alone
(`Witch House` remains `Witch House`). Detailed labels and scores are unchanged
in the SetTag evidence. Use `E` to open the genre screen, where you can restore
the suggestion after opting out or enter the exact value you want.

`W` runs a complete preflight and shows one batch confirmation. `Write` is the
default focused action, so `Enter` confirms it; `Esc` returns to review. SetTag
then runs preflight again, writes each file through its format-native adapter,
and reopens it to verify the result. Analysis errors disable writing for the
batch.

After a successful write, the app stays open. Written tracks leave the review
batch and appear as up to date in the library. Any skipped review tracks remain
available, so you can continue reviewing or return to the library for another
analysis batch before pressing `Q`.

### Local workbench and restart recovery

The app saves each completed analysis and staged genre edit to a small local
SQLite workbench. If you quit before writing, opening the same track or
directory resumes directly in review and shows the result as `Ready · date`.
The model is not rerun. Press `B` to return to the full metadata library or
reanalyze a track deliberately.

The workbench is private application state, not portable music metadata:

- macOS: `~/Library/Application Support/settag/state.sqlite3`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/settag/state.sqlite3`
- Windows: `%LOCALAPPDATA%\settag\state.sqlite3`

Override it for a run with `--state-db PATH`, or globally with
`SETTAG_STATE_DB`. For example:

```sh
uv run settag "/path/to/music" --state-db "/path/to/settag-state.sqlite3"
```

A cached result is ready only while the source size and modification time,
analysis model, evidence format, and observed standard genre still match.
Changing `--top` or `--score-cutoff` does not require reanalysis; SetTag applies
the new review policy to the stored evidence. Metadata already embedded in the
audio file is authoritative and supersedes an obsolete workbench entry.
Successfully written and verified entries are removed from the workbench.
Metadata created by the older cutoff-filtered contract needs one reanalysis to
populate the new bounded evidence bundle.

The database is deliberately not an export format. Use `S` in review or
`analyze --plan` when you want an explicit, portable JSONL plan that another
command or machine can preview and apply.

Review defaults can be adjusted without changing the evidence written to files:

```sh
uv run settag "/path/to/music" --top 5 --score-cutoff 0.10
```

`--threshold` remains an alias for `--score-cutoff`.

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
available through the app or a saved v3 plan because it must be staged
explicitly per track.

### Logging

Normal `INFO` output is compact:

```text
INFO [1/1] /path/to/track.mp3
INFO   file genre tag: House (unchanged)
INFO   SetTag genres: none -> Electronic---House score 0.765
INFO   dry run: SetTag analysis bundle would change (6 internal fields); nothing written
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

`SETTAG_GENRE` contains the bounded ranked evidence labels.
`SETTAG_GENRE_SCORES` is one compact JSON value containing the same labels,
order, and raw model scores. SetTag currently stores the top 20 results without
a score cutoff. Metadata consumers such as SetPath can therefore choose their
own cutoff instead of inheriting SetTag's review preference.

The six SetTag-owned fields form one analysis bundle and are written together.
They are not six independently selectable tags. The conventional genre remains
a separate staged layer.

The standard genre is not part of the SetTag namespace. For newly analyzed
tracks where it is empty, the app stages the conservative standard-genre
suggestion by default; the user can edit or opt out before confirming the
write. Title, artist, album, comments, artwork, duplicate fields, and metadata
owned by other software are preserved.

Before any batch write, SetTag verifies:

1. source SHA-256, size, and observed metadata state;
2. the detected native adapter;
3. every reconstructed SetTag change;
4. any explicitly staged standard genre change.

After each write it reopens the file and verifies both the evidence and the
expected standard genre. A batch preflight is all-or-nothing, although native
file writes cannot form one transaction across multiple files.

## Saved plans

`settag.plan/v3` is the compact review and write contract. It records bounded
evidence separately from SetTag's current review selection and records the
observed file genre separately from an optional staged target:

```json
{
  "schema": "settag.plan/v3",
  "path": "/path/to/music/track.mp3",
  "source": {
    "sha256": "b7b118125b3289157da76212b54c2e1f91b4db2c3c0ff1bca4094c4d0046ed23",
    "size": 12605465,
    "mtime_ns": 1633515033000000000
  },
  "file_genre": [],
  "target_file_genre": ["House"],
  "evidence": [
    {
      "label": "Electronic---Progressive House",
      "score": 0.664
    },
    {
      "label": "Electronic---Techno",
      "score": 0.069
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
    "config_sha256": "3935b3e0c51c85b750ee8cffc471b7a8e0a5a4cec60b336dde42fd321d40b5e6"
  },
  "changes": {
    "settag": [
      "Genre labels: 0 → 2",
      "Ranked score data: add"
    ],
    "file_genre": "File genre: None → House"
  }
}
```

Plans produced by `analyze --plan` set `target_file_genre` to `null`. Plans
saved from the app retain explicit edits. `preview` is the built-in
human-readable renderer; users do not need `jq`.

Every apply performs preflight both before and after confirmation. A source
change or an analysis-error record rejects the whole plan before writing.
Legacy `settag.plan/v1` and `settag.plan/v2` plans remain readable; their
previously selected labels are treated as the complete available evidence.

## Scores and model

The pinned model pair is:

- embedding: `discogs-maest-30s-pw-519l-2`
- classifier: `genre_discogs519-discogs-maest-30s-pw-519l-1`

Each score is the mean class-wise sigmoid activation across analyzed audio
patches. It is useful for ranking and applying a score cutoff, but it is not
demonstrated to be calibrated confidence or probability. Scores do not need to
sum to `1`.

The review cutoff controls review markings and suggestions only. It does not
remove scores from the bounded evidence bundle:

```sh
uv run settag analyze "/path/to/music" --top 5 --score-cutoff 0.10
```

## Licensing

SetTag source is licensed under AGPL-3.0-only. The default inference workflow
also uses separately licensed components and model files:

- `essentia-tensorflow` is AGPL-3.0-only; UPF also offers proprietary licensing
- TensorFlow runtime is Apache-2.0
- UPF publicly offers the Essentia model weights and metadata for
  non-commercial use, with proprietary licensing available

UPF's public documentation currently identifies the exact Creative Commons
variant inconsistently as CC BY-NC-ND 4.0 and CC BY-NC-SA 4.0. The model
repository's licence file is also internally inconsistent, and the pinned
model metadata does not specify a licence. Both stated variants restrict the
public grant to non-commercial use. Until UPF provides model-specific
clarification, SetTag does not assert permission to redistribute or publish
adapted model files.

Personal, educational, or research use may fall within the public model terms.
Professional, business, or other revenue-generating use is not clearly
permitted and may require separate permission from UPF or a different analysis
backend. Downloading the models separately does not change their terms.

Metadata produced by SetTag can be exported to and imported by compatible
music-library applications. Users remain responsible for ensuring that their
selected analysis backend permits their intended use.

Exact attributions and dependency notices are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

See:

- <https://essentia.upf.edu/licensing_information.html>
- <https://essentia.upf.edu/models.html>
- <https://essentia.upf.edu/models/LICENSE>

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
