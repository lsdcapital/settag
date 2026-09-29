# Review TODO

Findings from the September 2026 code review, in priority order.

## Data safety

- [x] 1. Clearing a FLAC genre never deletes it; undo of a genre added to a genreless FLAC fails verification (`tags.py` `VorbisOwnedTagStore.write_standard_genres`).
- [x] 2. ID3v2.3 files are saved as v2.4, dropping TSIZ/TRDA/RVAD/EQUA/unknown frames (`tags.py` `_commit`). Preserve the original version.
- [x] 3. External writes between the staleness check and `os.replace` are overwritten. Re-stat just before the replace.
- [x] 4. One malformed journal row makes `recent()`/`batch()` raise, locking out all undo history.
- [ ] 5. Journal entry is recorded after `os.replace`; a crash in between leaves an unrecorded write.
- [x] 6. Retrying after a partial undo flags already-restored files as changed; undo doesn't update the journal with restored stat. Undo order is oldest-first despite the docstring.
- [x] 7. Atomic replace drops macOS xattrs, breaks hard links; no directory fsync after rename.
  Xattrs/ACLs are now copied on macOS, with `F_FULLFSYNC` and a directory fsync. Hard links still break: inherent to write-then-rename.
- [ ] 8. Undo restores Vorbis hygiene keys lowercased and merged across case variants.
  Deferred: Vorbis field names are case-insensitive by spec, so readers see the same fields.
- [x] 9. MP4 freeform hygiene writes force UTF-8; `apply_hygiene` calls `on_write` without the `suppress` `apply_prepared` uses.
- [x] 10. Non-UTF-8 bytes in an owned MP4 atom raise a raw `UnicodeDecodeError`.

## TUI

- [ ] 11. An unhandled worker exception leaves `busy`/`analysis_running` set and the app can't be quit. Add `on_worker_state_changed` recovery and a force-quit.
- [ ] 12. Hung analysis can't be cancelled: the worker waits on the pipe with no timeout or cancel check.
- [ ] 13. A background analysis result overwrites a plan the user is reviewing/editing; a late write then deletes the fresh workbench row.
- [ ] 14. One persistently failing track blocks all writes; no way to dismiss it.
- [ ] 15. B (library) resets the analysis selection, contrary to DESIGN.md.
- [ ] 16. Hygiene reports "Nothing was changed" if the post-write rescan fails; partial-failure rescan is outside any `try`.
- [ ] 17. Genre edit persists synchronously on the event loop (can block up to the SQLite timeout).
- [ ] 18. `_accept_reverted` drops review plans silently and leaves stale workbench rows.

## Analysis, Beatport, CLI

- [ ] 19. A per-track `LookupStopped` disables Beatport for the rest of the batch. Split service-level from per-track errors.
- [ ] 20. Beatport scraping is always on; add an opt-out (`--offline` / config).
- [ ] 21. Read-phase network errors (`ConnectionResetError`, `IncompleteRead`, `RemoteDisconnected`) escape Beatport retry/stop handling.
- [ ] 22. `settag --no-tui ~/Music` (flag before path) fails with "command required".
- [ ] 23. Scanner picks up macOS `._*` AppleDouble files; unreadable directories are skipped silently.
- [ ] 24. `models download` / `models status` print raw tracebacks on network or checksum errors.
- [ ] 25. First-analysis stderr capture discards native crash output.
- [ ] 26. Worker response pickling failure loses the error message.
- [ ] 27. Model files are SHA-hashed ~3× per analyzer build and again on every retry.
- [ ] 28. `analyze --embeddings` leaves an empty file behind if opening `--output` fails; Beatport cache is never pruned.

## Architecture

- [ ] 29. Extract a pure `Session` model from `tui/core.py` (selection/review/write state transitions) and move inspector text out.
