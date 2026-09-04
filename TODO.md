# TODO

Findings from the September 2026 review that were not fixed in the same pass.
Each item says what is wrong, where, and why it matters, so it can be picked up
cold. Items already fixed: journal errors swallowed on insert, abandoned
`.settag-part` temp files scanned as tracks, unguarded `mark_reverted` in
`settag undo`, no fsync before the write swap, no model download timeout, tests
reading the developer's real config, and the stray root `pnpm-lock.yaml`.

## Documentation

- **Plan schema.** README and DESIGN.md both say `settag.plan/v4` is the only
  accepted schema and show v4 JSON. `src/settag/plans.py` emits v5 (adds
  `source.audio_sha256`) and reads v4 and v5. Update the prose and the sample,
  and drop the `"settag_version": "0.1.0"` in the sample or make it obviously
  illustrative.
- **Undocumented flags.** README omits `--model-dir` (run, analyze, models),
  `--journal-db` (run, hygiene, apply, undo), `undo --limit`, `undo --yes`,
  `models download --force`, and `--version`. The undo section mentions only
  `SETTAG_JOURNAL_DB` even though DESIGN.md names the flag.
- **THIRD_PARTY_NOTICES.md** omits `tomli` (MIT, Python < 3.11) and the
  transitive `platformdirs` and `pygments` that Textual pulls in.

## Packaging and CI

- **Pin third-party actions by SHA.** `actions/checkout@v7`,
  `astral-sh/setup-uv@v9.0.0`, `actions/upload-artifact@v7`,
  `actions/download-artifact@v8`, and especially
  `pypa/gh-action-pypi-publish@release/v1` (a moving branch) are all mutable
  references in a workflow that holds `id-token: write`.
- **Build the wheel in branch CI.** `uv build` and the clean-venv smoke test run
  only in `release.yml`, so a packaging break (missing subpackage, dead entry
  point) is first seen when it blocks a release. Add both to `ci.yml` on one
  Python version.
- **Lockfile drift.** Neither workflow runs `uv lock --check`, so a
  `pyproject.toml` change that leaves `uv.lock` stale passes CI.
- **sdist contents.** The sdist ships `Makefile`, `PRODUCT.md`, `uv.lock`, and
  `scripts/render_site_app_image.py`. Add `scripts/` and `PRODUCT.md` to the
  sdist exclude list in `pyproject.toml`.
- **`.gitignore`** lacks `dist/` and `.conductor/` at the root; `dist/`
  currently holds stale 0.1.0 artifacts locally.
- **`pnpm/setup@v2` in `site.yml`.** The canonical action is
  `pnpm/action-setup`. Confirm the one in use is intended.
- **essentia-tensorflow on 3.14** is `>=2.1b6.dev1438` with no upper bound,
  unlike the exact pin for older Pythons. Decide whether that is deliberate.

## Behaviour

- **Partial undo marks the whole batch reverted.** Both `settag undo`
  (`cli/commands.py`) and the app (`tui/app.py`) call `mark_reverted` after
  restoring the restorable entries even when some were blocked by the size or
  mtime check. The batch then shows as "Already reverted" though some files
  were never restored. It stays undoable with `--force`, so this is cosmetic,
  but a `reverted_count` or per-entry status would be more honest.
- **Durability on macOS.** `OwnedTagStore._commit` now calls `os.fsync` on the
  candidate before the rename. On APFS a full flush needs
  `fcntl(F_FULLFSYNC)`; decide whether the extra cost is worth it for a tag
  write, and whether the containing directory should be flushed too.
- **Hygiene web-address rule.** `_WEB_ADDRESS` in `hygiene.py` matches any
  `word.tld` token for a broad TLD list including `.to`, `.me`, `.co`, `.cc`.
  Findings are reviewed individually so nothing is written silently, but a
  comment such as "cue to.me" will be flagged. Consider requiring a scheme or
  `www.` for the short TLDs.

## Structure and tests

- **`tui/app.py`** is one 1729-line class with 84 methods covering phases,
  selection, the write flow, the undo flow, and worker plumbing. Extract the
  write and undo flows (each already has a preflight, confirm, apply, and
  complete step) before the next feature lands there.
- **Architecture test guards itself weakly.**
  `test_the_guard_would_catch_an_unmarked_aggregate` in
  `tests/test_architecture.py` only checks that `_sum_calls` finds a call; it
  never exercises the marker logic. The rule also covers `sum()` but not
  `len()` or `Counter`, which DESIGN.md acknowledges.
- **Review coverage.** `analyzer.py` and the TUI internals (`tui/app.py`,
  `tui/hygiene.py`, `tui/screens.py`) received a lighter review pass than the
  write path, journal, worker, model store, workbench, and CLI. A focused pass
  on blocking calls in Textual handlers and on analyzer error isolation is
  still worth doing.
