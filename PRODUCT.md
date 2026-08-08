# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

SetTag is a personal power tool for DJs who manage a local music library and
want help reviewing and correcting genre metadata. Its primary user is
comfortable in a terminal, works through many tracks at once, and needs to
understand exactly what will change before committing a batch.

The marketing site serves DJs evaluating whether SetTag fits their library,
platform, workflow, and licensing context before they install it.

## Product Purpose

SetTag analyzes audio for ranked genre evidence, compares that evidence with
existing file metadata, and helps the user stage safe, explicit tag changes.
Its separate metadata-hygiene workflow reviews suspicious comments and
generated text debris without loading an analysis model.
Success means a DJ can scan a library, spot missing or implausible genres,
accept or edit individual suggestions, include or exclude tracks in bulk, and
write only the reviewed changes without losing unrelated metadata.

The site should make that workflow, its safety boundary, and its practical
installation constraints understandable before asking a visitor to install.

## Positioning

SetTag combines local, on-device music analysis with a reviewable staging
workflow: it proposes evidence-backed metadata changes, shows the user what
would change, and keeps writing behind a separate explicit approval. It
preserves unrelated metadata and does not send information about the library
off the machine.

## Operating Context

SetTag works directly with a DJ's local audio files. The core workflow is
scan, analyze, stage, review, then write on approval. The default experience is
a keyboard-first Textual app; a plain CLI supports scripts, redirected output,
saved plans, and automation.

Users install SetTag as a Python command-line tool, download model files
separately, and run it against a track or directory. Analysis is CPU-intensive
and operates on local files and cached local models. The marketing site is a
prerendered, static companion to the open-source repository and package.

## Capabilities and Constraints

- Genre analysis uses MAEST by default. Optional Discogs-EffNet heads add
  mood/theme and instrument evidence.
- Supported containers are MP3, AIFF, and WAV through ID3; FLAC through Vorbis
  comments; and M4A, M4B, and MP4 through MP4 atoms.
- SetTag preserves unrelated metadata, stages conventional genre changes
  separately from SetTag-owned evidence, and verifies completed writes.
- Metadata hygiene is a separate model-free review that flags web addresses in
  comment-like fields, duplicates, empty values, and encoder markers. Only
  individually checked suggestions are removed, verified, and journaled.
- SetTag requires Python 3.10–3.14 and currently supports recent macOS releases
  and Linux on x86_64. Windows and Linux on ARM are unsupported because
  `essentia-tensorflow` does not publish compatible wheels.
- SetTag is AGPL-3.0-only. Its downloaded model weights have separate
  non-commercial licensing constraints; professional or revenue-generating use
  is not presented as clearly permitted.
- Model scores are ranked evidence suitable for comparison and thresholds, not
  demonstrated calibrated confidence or probabilities.
- Standard genre changes are never applied opaquely or outside the staged,
  reviewable write flow.

## Brand Commitments

The product name is SetTag. Its voice is precise, restrained, and trustworthy.
Interaction density may take cues from lazygit—fast list navigation, visible
state, and keyboard efficiency—while remaining smaller, clearer, and easier to
learn.

SetTag must not present itself as a full terminal IDE, depend on
undiscoverable Vim-only commands, bury common actions in nested screens, or use
decorative terminal effects. It must not imply automatic taxonomy certainty,
hide the difference between model evidence and a conventional genre edit, or
rely on color alone to communicate selection, warnings, or staged writes.

## Evidence on Hand

- [README.md](README.md) documents the shipped workflows, supported formats,
  platform limits, model behavior, and installation path.
- [DESIGN.md](DESIGN.md) records the current application architecture, safety
  invariants, metadata contracts, and interaction model.
- [site/src/routes/index.tsx](site/src/routes/index.tsx) contains the current
  public product explanation and clearly labels its staged-change examples as
  illustrative, with invented track names.
- The repository's tests exercise analysis, planning, metadata adapters,
  preflight, verified writes, undo, persistence, CLI behavior, and the Textual
  interface.
- The README records a small 14-track comparison of genre sampling modes. It is
  useful implementation evidence, not broad independent product validation.
- There are no confirmed customer testimonials, case studies, press quotes, or
  independent benchmarks. Future work must not fabricate them.

## Product Principles

1. Make the batch understandable before making it writable.
2. Keep local analysis, staged review, and explicit approval as separate,
   legible parts of the workflow.
3. Preserve user control and unrelated metadata at every step.
4. Be exact about model evidence, platform support, and licensing limits.
5. Preserve expert speed without hiding essential actions or safety
   boundaries.

## Accessibility & Inclusion

The interface is keyboard-first with persistent, visible shortcuts and
complete focus states. Mouse interaction should work where the terminal
supports it. Selection, errors, and staged changes must remain understandable
without color. The interface should adapt to narrower terminals, honor
`NO_COLOR`, avoid unnecessary motion, and retain a plain non-interactive CLI
path for automation and terminals where a full-screen UI is unsuitable.

The marketing site should preserve semantic structure, visible keyboard focus,
adequate contrast, reduced-motion support, and usable narrow-screen layouts.
