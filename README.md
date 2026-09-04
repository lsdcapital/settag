# settag

SetTag is a terminal-first analysis assistant for DJ music libraries. It uses
MAEST for genre and optional Discogs-EffNet heads for mood/theme and instrument
evidence, then lets you stage and verify metadata changes before writing.

The default experience is a Textual app. The same executable also provides a
plain, non-TUI CLI for scripts, redirected output, saved plans, and CI.

Metadata hygiene is a separate, model-free workflow. It finds suspicious
comments, web addresses, duplicate or empty text values, and generated encoder
markers, then lets you review individual field-level removals before writing.

Supported files:

- MP3, AIFF, and WAV with ID3 metadata
- FLAC with Vorbis comments
- M4A, M4B, and MP4 with MP4 atoms

## Install

```sh
uv tool install settag
settag models download
settag "/path/to/music"
```

Or with pipx: `pipx install settag`. SetTag needs Python 3.10–3.14.

Platform support is inherited from `essentia-tensorflow`, which ships only
prebuilt binary wheels. There is no pure-Python fallback and no source build
worth attempting, so a platform without a wheel cannot install SetTag at all:

| Platform | Requirement |
| --- | --- |
| macOS, Apple Silicon | macOS 15 (Sequoia) or newer |
| macOS, Intel | macOS 14 (Sonoma) or newer; macOS 15 on Python 3.14 |
| Linux, x86_64 | glibc 2.17 or newer (manylinux2014, so any current distro) |

**Windows and Linux on ARM are not supported.** Upstream has never published
wheels for either, on any release.

The analysis backend is a large download — roughly 100 MB on macOS and 290 MB
on Linux — and the models are fetched separately on top of that.

Genre is the default and the only model loaded unless tasks are requested
explicitly:

```sh
settag models download --tasks genre,mood-theme,instrument
settag analyze "/path/to/music" --tasks genre,mood-theme,instrument
```

The interactive app reads its default tasks from
`~/.config/settag/config.toml`:

```toml
[analysis]
tasks = ["genre", "mood-theme", "instrument"]
```

The config file is optional. `--tasks` overrides it for one TUI run:

```sh
settag "/path/to/music" --tasks instrument
settag "/path/to/music" --tasks mood-theme,instrument
settag "/path/to/music" --tasks genre,mood-theme,instrument
```

Set `SETTAG_CONFIG` or pass `--config /path/to/config.toml` to use another
config file. Task precedence is `--tasks`, then the config file, then the
`genre` default. The config file is read only for options the flags left
unset.

### How much audio the genre model reads

The genre model, MAEST, embeds one 30-second patch at a time and dominates the
run: 15.5s against EffNet's 1.2s on a 482-second track. `--genre-sample`
chooses how many of those patches it reads. Mood/theme and instrument always read the whole
track, because they are cheap and their taxonomies want whole-track averaging.

| `--genre-sample` | reads | relative speed |
| --- | --- | --- |
| `full` | every 30s patch | 1.0x |
| `middle` (default) | 4 patches from the centre | 2.2x |
| `spaced` | 6 patches spread across the track | 1.6x |

Measured against the full-track answer over 14 tracks, `middle` preserved the
rolled-up conventional genre on 14/14 and `spaced` on 13/14, both with a rank
correlation above 0.98 across all 519 labels. What moves is the crowded
0.1-0.25 tail, where the model is not confident anyway. Fewer patches also
means less averaging, so scores come out more peaked.

```toml
[analysis]
genre_sample = "full"
```

Changing this changes the evidence configuration digest, so tracks analyzed
under a different setting are correctly reported as stale and reanalyzed.

Models are downloaded once into `~/.cache/settag/models`; `--model-dir` on
`models`, `analyze`, and the app points at another directory, and
`models download --force` replaces files that are already present. They are not bundled
with this repository or its Python distributions. Downloads and installed
files must match the SHA-256 digests pinned in SetTag's model catalogue before
inference can start. Review [Licensing](#licensing) before using the default
models in a professional, business, or revenue-generating workflow.

## Run the app

Give SetTag a track or a directory:

```sh
settag "/path/to/track.mp3"
settag "/path/to/music/library"
```

For metadata cleanup without analysis, open the independent hygiene review:

```sh
settag hygiene "/path/to/music/library"
```

No model files are loaded. In the main app, `H` switches to the same hygiene
step after any running analysis has stopped. Use `--no-tui` to print findings
without changing files.

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
metadata for any configured task, or a changed task model/config are selected
by default. Up-to-date tracks remain visible but unselected. The active task
families are shown in the library context line. Adjust the selection, then
press `R` to load MAEST, EffNet, or both and analyze only the selected tracks
visible in the current filter. Selections in other filtered views are not
included.

Analysis runs serially in a background worker. The selected batch is fixed when
the job starts, but the interface remains available for navigation, filtering,
and inspection. Each completed track is persisted immediately and becomes
available under `V` while the next track is still running.

The library keys are:

| Key | Action |
|---|---|
| `↑` / `↓` | Move through tracks |
| `Space` | Select or unselect the current track for analysis |
| `A` | Toggle all eligible tracks in the current view on or off |
| `I` | Show or hide details for the highlighted track |
| `F` | Cycle All, Needs analysis, Missing genre, and Up to date views |
| `V` | Open saved results that are ready to review, when available |
| `Enter` / `R` | Analyze the selected tracks |
| `Esc` | Stop after the track currently being analyzed |
| `U` | Undo a previous write |
| `H` | Switch to the separate metadata-hygiene review |
| `Q` | Quit |

Cancellation is cooperative because Essentia/TensorFlow inference cannot be
safely interrupted halfway through a track. Completed results remain available
for review and are saved in the local workbench; unprocessed tracks remain
selected for a later run. Cancellation never writes metadata.

Background execution keeps the interface interactive, but inference remains
CPU-intensive; a warm laptop and active fans are expected during a large batch.
SetTag deliberately analyzes one track at a time rather than multiplying that
load with parallel track workers.

Press `V` as soon as the first track completes to open review. If you stay in
the library, the app switches to review when the full batch finishes:

```text
  ✓  Track                       File genre    Analysis          Suggested          Write plan
  ✓  Eli & Dani - What Do...     None → House  New · 2026-07-23  Progressive House  Evidence + Genre

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
| `Enter` / `W` | Preflight, confirm once, write, and verify completed tracks |
| `B` | Return to the metadata library to choose another analysis batch |
| `U` | Undo a previous write |
| `H` | Switch to the separate metadata-hygiene review |
| `Q` | Quit |

Newly analyzed tracks with SetTag changes are checked for writing by default.
Press `Space` to check or uncheck the highlighted track.
While background analysis continues, review, genre editing, plan saving, and
writing operate on the completed snapshot only. The in-flight track cannot
enter a write until its analysis plan is complete.
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
directory opens in the library and shows the saved result as `Ready · date`.
The model is not rerun. Press `V` to review saved results, or select a ready
track in the library to reanalyze it deliberately.

The workbench is private application state, not portable music metadata:

- macOS: `~/Library/Application Support/settag/state.sqlite3`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/settag/state.sqlite3`
- Windows: `%LOCALAPPDATA%\settag\state.sqlite3` — the path SetTag would use,
  though Windows cannot install the analysis backend today (see [Install](#install))

Override it for a run with `--state-db PATH`, or globally with
`SETTAG_STATE_DB`. For example:

```sh
settag "/path/to/music" --state-db "/path/to/settag-state.sqlite3"
```

A cached result is ready only while the source size and modification time,
analysis model, evidence format, and observed standard genre still match.
Changing `--top` or `--score-cutoff` does not require reanalysis; SetTag applies
the new review policy to the stored evidence. Metadata already embedded in the
audio file is authoritative and supersedes an obsolete workbench entry.
Successfully written and verified entries are removed from the workbench.
A track renamed or moved between runs is found again by its audio digest
when exactly one scanned file matches, so its saved result follows it;
an ambiguous match is reported as missing rather than guessed.
Metadata created by the older cutoff-filtered contract needs one reanalysis to
populate the new bounded evidence bundle.

The database is deliberately not an export format. Use `S` in review or
`analyze --plan` when you want an explicit, portable JSONL plan that another
command or machine can preview and apply.

Review defaults can be adjusted without changing the evidence written to files:

```sh
settag "/path/to/music" --top 5 --score-cutoff 0.10
```

`--threshold` remains an alias for `--score-cutoff`.

## Metadata hygiene

Hygiene is intentionally separate from genre analysis:

```text
scan tags → flag suspicious values → choose fields → confirm → clean and verify
```

The first release recognizes comment-like and generated metadata in each
supported container:

| Container | Reviewed fields |
|---|---|
| ID3 | `COMM`, user URL frames, comment/source/url `TXXX` fields, `TSSE` |
| FLAC | comment, description, source, URL, download, and encoder comments |
| M4A/MP4 | `©cmt`, `©too`, and matching text freeform atoms |

A comment such as `electronicfresh.com` is suggested for removal because it
contains a web address. An ordinary DJ note remains untouched. Encoder markers
such as `Lavf62.12.102`, empty values, and exact duplicate values are also
suggested. These rules create review suggestions, never automatic writes.

Every finding is checked independently. `Space` includes or excludes one
field-level suggestion, `A` toggles all findings, `I` shows the exact before and
after values, and `W` runs preflight and opens one confirmation. Only checked
suggestions are written. Titles, artists, albums, artwork, genres, SetTag
evidence, and unselected comments remain unchanged.

Hygiene writes use the same temporary-copy, reopen-and-verify, and journal path
as analysis writes. `settag undo` therefore restores cleaned values too.

## Plain CLI mode

When stdin or stdout is not a terminal, `settag PATH` automatically becomes a
plain dry run. It never prompts or writes. Force that mode in a terminal with:

```sh
settag "/path/to/music" --no-tui
```

The named commands are also plain CLI commands:

```sh
# Which build stamps SETTAG_VERSION
settag --version

# Human-readable dry-run logs
settag analyze "/path/to/music"

# Complete machine-readable audit records
settag analyze "/path/to/music" --output analysis.jsonl

# Read current tags without loading the model
settag inspect "/path/to/track.mp3"

# Every field, but no ranked score lines: readable across a directory
settag inspect "/path/to/music" --no-scores

# Create, preview, and apply a durable plan
settag analyze "/path/to/music" --plan settag-plan.jsonl
settag preview settag-plan.jsonl
settag apply settag-plan.jsonl
```

For deliberate automation, `apply --yes` suppresses only the confirmation.
It does not suppress source, metadata, or plan validation:

```sh
settag apply settag-plan.jsonl --yes
```

`analyze` never writes. A reviewed plan is the only route to disk, in the app
or through `apply`, so every write in SetTag passes the same preflight and
verification. Standard genre editing is available in the app or in a saved
plan because it must be staged explicitly per track.

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
LOG_LEVEL=DEBUG settag analyze "/path/to/track.mp3"
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

Each task's label field holds bounded ranked evidence, and its `_SCORES` twin
holds the same labels and order with the raw model scores as one compact JSON
value. SetTag stores the top 60 results without a score cutoff so consumers such
as SetPath can apply their own policy. Genre evidence is exclusively
MAEST-derived; EffNet cannot populate `SETTAG_GENRE` or a conventional genre
field. Task updates are independent, so an instrument-only run replaces
instrument evidence while preserving valid genre and mood/theme evidence.

The full field list, the per-task `vocabulary` declaration consumers should
key on, and the `SETTAG_PROVENANCE` record are specified once, in
[DESIGN.md](DESIGN.md#settag-evidence-contract).

The standard genre is not part of the SetTag namespace. For newly analyzed
tracks where it is empty, the app stages the conservative standard-genre
suggestion by default; the user can edit or opt out before confirming the
write. Title, artist, album, artwork, duplicate fields, and metadata owned by
other software are preserved. Comments and other hygiene fields are preserved
unless the user explicitly checks their removal in the separate hygiene review.

Before any batch write, SetTag verifies for every included track:

1. the audio has not changed since analysis. The check uses a digest of the
   audio samples only, so another tool retagging the file in between does not
   block the write, while a re-encode, edit, or truncation does;
2. the observed conventional genre;
3. the detected native adapter;
4. every reconstructed SetTag change;
5. any explicitly staged standard genre change.

The audio digest is checked once more immediately before each file is written.
After each write SetTag reopens the file and verifies both the evidence and the
expected standard genre. A batch preflight is all-or-nothing, although native
file writes cannot form one transaction across multiple files.

## Undoing a write

Every verified write is journaled with the tag values it replaced, so a write
can be reverted. Press `U` in the app, or use the CLI:

```bash
# What has SetTag written?
settag undo --list

# Preview reverting the most recent write
settag undo --dry-run

# Revert the most recent write, or a named one
settag undo
settag undo 20260725T110349-8993c143

# Show more history, or revert without the confirmation prompt
settag undo --list --limit 25
settag undo --yes
```

The journal lives in its own database, separate from the workbench cache, so
clearing the cache never destroys undo history:

| Purpose | Location |
|---|---|
| Write journal (durable) | `journal.sqlite3`, overridable with `SETTAG_JOURNAL_DB` or `--journal-db` |
| Workbench cache (disposable) | `state.sqlite3`, overridable with `SETTAG_STATE_DB` or `--state-db` |

Undo restores exactly what a write changed: the SetTag-owned fields listed
above, the conventional genre tag when that write explicitly staged an edit,
and any fields explicitly removed by a hygiene write.
A track that had no SetTag metadata beforehand is returned to having none rather
than being left with debris.

Some limits worth knowing:

- **It restores tag values, not bytes.** mutagen rewrites the tag block on save,
  so the file will not regain its pre-write SHA-256 even after a perfect undo.
- **It is per write, not "restore original".** Two writes to one file produce two
  journal entries; undoing the newest returns the state after the first write.
- **It leaves other software alone.** Edits made elsewhere between the write and
  the undo are untouched.
- **It refuses changed files.** If a file was modified after SetTag wrote it,
  that file is skipped and named; `--force` overrides.

Entries older than 90 days are pruned.

## Saved plans

A plan is one JSONL record per track on the `settag.plan/v5` schema. It carries
the bounded evidence separately from SetTag's current review selection, the
observed file genre separately from an optional staged target, the audio
digest preflight checks against, and the human-readable change lines `preview`
prints. The record layout is specified once, in
[DESIGN.md](DESIGN.md#compact-plan-record).

Plans produced by `analyze --plan` set `target_file_genre` to `null`. Plans
saved from the app retain explicit edits. `preview` is the built-in
human-readable renderer; users do not need `jq`.

Every apply performs preflight both before and after confirmation. A source
change or an analysis-error record rejects the whole plan before writing.

Plans on the pre-release `settag.plan/v4` schema still apply. They predate the
audio digest, so preflight falls back to comparing the whole file for them: a
tag write by another tool blocks a v4 plan where a v5 plan would proceed. Any
other schema is rejected with an explicit error.

## Scores and models

The pinned production models are:

- MAEST genre:
  - `discogs-maest-30s-pw-519l-2`
  - `genre_discogs519-discogs-maest-30s-pw-519l-1`
- optional Discogs-EffNet metadata:
  - `discogs-effnet-bs64-1`
  - `mtg_jamendo_moodtheme-discogs-effnet-1`
  - `mtg_jamendo_instrument-discogs-effnet-1`

When genre and EffNet tasks are requested together, one 16 kHz audio decode
feeds both model stacks. Mood/theme and instrument always reuse one EffNet
embedding pass.

Each score is the mean class-wise sigmoid activation across analyzed audio
patches. It is useful for ranking and applying a score cutoff, but it is not
demonstrated to be calibrated confidence or probability. Scores do not need to
sum to `1`.

The review cutoff controls review markings and suggestions only. It does not
remove scores from the bounded evidence bundle:

```sh
settag analyze "/path/to/music" --top 5 --score-cutoff 0.10
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
uv run ruff format --check .
uv run ty check
uv build
```

GitHub Actions runs the same lint, format, type, and test checks on every
supported Python version (3.10 through 3.14) for each push to `main`, pull
request, and release tag.

The default test run uses Textual's headless app runner and synthetic audio;
it does not run model inference. A small set of smoke tests against the real
genre model is opted into separately and skips itself when the models are not
downloaded:

```sh
uv run pytest -m models
uv run settag models status --tasks genre,mood-theme,instrument
uv run settag analyze "/path/to/track.flac" --tasks genre,mood-theme,instrument
```

Architecture and safety contracts are documented in [DESIGN.md](DESIGN.md).
