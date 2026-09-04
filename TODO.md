# TODO

Findings from the September 2026 review that are still open. Each item says
what is wrong, where, and why it matters, so it can be picked up cold.

Closed so far: journal errors swallowed on insert, abandoned `.settag-part`
temp files scanned as tracks, unguarded `mark_reverted` in `settag undo`, no
fsync before the write swap, no model download timeout, tests reading the
developer's real config, the stray root `pnpm-lock.yaml`, README and DESIGN
drift, third-party notices, SHA-pinned actions with Dependabot to keep them
current, the wheel build and `uv lock --check` in branch CI, sdist excludes,
`.gitignore`, the architecture test's self-check, partial undo marking a batch
reverted (CLI and app, both tested), the exact essentia pin on 3.14, the
hygiene rule flagging `word.to` and `word.me` as web addresses, and short
clips reaching Essentia through `settag analyze` with a native error message.

Decided and not changing: `os.fsync` without `F_FULLFSYNC` on macOS is enough
for a tag write; PRODUCT.md stays as the site tooling's brief.

## Structure

- **`tui/app.py`** is one 1700-line class with more than 80 methods covering
  phases, selection, the write flow, the undo flow, and worker plumbing.
  Extract the write and undo flows (each already has a preflight, confirm,
  apply, and complete step) before the next feature lands there. Plan first.
- **Two small main-thread I/O calls in the app.** `action_save` writes the
  plan file, and `_persist` upserts the workbench row after each analyzed
  track, both on the Textual event loop. Each is fast on a healthy disk, but a
  locked workbench database would hold the UI for the SQLite timeout (10 s).
  Move `_persist` into the analysis worker when the split above happens.

## Tests

- **Architecture rule scope.** The `sum()` guard now checks its own detection,
  but it still does not cover `len()` or `Counter`, which DESIGN.md
  acknowledges.
- **Real-audio smoke test is manual.** The analyzer's behaviour on real input
  (including the short-clip refusal, verified by hand against MAEST at 0.5 s,
  5 s, 20 s, and 35 s) is only exercised with downloaded models. A marked
  `@pytest.mark.models` test that skips when the cache is empty would let a
  developer with models run it in one command.
