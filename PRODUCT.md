# Product

## Register

product

## Users

SetTag is a personal power tool for DJs who manage a local music library and
want help reviewing and correcting genre metadata. Its primary user is
comfortable in a terminal, works through many tracks at once, and needs to
understand exactly what will change before committing a batch.

## Product Purpose

SetTag analyzes audio for ranked genre evidence, compares that evidence with
existing file metadata, and helps the user stage safe, explicit tag changes.
Success means a DJ can scan a library, spot missing or implausible genres,
accept or edit individual suggestions, include or exclude tracks in bulk, and
write only the reviewed changes without losing unrelated metadata.

## Brand Personality

Precise, restrained, trustworthy. The interaction density may take cues from
lazygit—fast list navigation, visible state, and keyboard efficiency—while
remaining smaller, clearer, and easier to learn.

## Anti-references

SetTag should not resemble a full terminal IDE, depend on undiscoverable
Vim-only commands, bury common actions in nested screens, or use decorative
terminal effects. It should not make opaque taxonomy transformations, apply
standard genre changes automatically, or rely on color alone to communicate
selection, warnings, or staged writes.

## Design Principles

1. Make the batch understandable before making it writable.
2. Keep the track list primary and reveal detail progressively.
3. Make bulk evidence changes fast, but require explicit per-track genre edits.
4. Stage every edit visibly and keep writing as a separate confirmed action.
5. Preserve expert speed without hiding essential actions or safety boundaries.

## Accessibility & Inclusion

The interface is keyboard-first with persistent, visible shortcuts and
complete focus states. Mouse interaction should work where the terminal
supports it. Selection, errors, and staged changes must remain understandable
without color. The interface should adapt to narrower terminals, honor
`NO_COLOR`, avoid unnecessary motion, and retain a plain non-interactive CLI
path for automation and terminals where a full-screen UI is unsuitable.
