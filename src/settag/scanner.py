from __future__ import annotations

from pathlib import Path


class UnsupportedInputError(ValueError):
    pass


# Marker in the name of the temporary copy a tag write is built in. It keeps the
# audio suffix so container detection matches the original, which means a copy
# abandoned by a hard kill would otherwise look like one more track to scan.
WRITE_TEMPORARY_MARKER = ".settag-part"

SUPPORTED_EXTENSIONS = frozenset(
    {
        ".aif",
        ".aiff",
        ".flac",
        ".m4a",
        ".m4b",
        ".mp3",
        ".mp4",
        ".wav",
        ".wave",
    }
)


def scan_audio(path: Path) -> list[Path]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Input does not exist: {resolved}")

    if resolved.is_file():
        if resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise UnsupportedInputError(
                f"Unsupported audio extension for {resolved}. Supported: {supported}"
            )
        return [resolved]

    if not resolved.is_dir():
        raise UnsupportedInputError(f"Input is neither a file nor directory: {resolved}")

    # A set, because two symlinks to one file resolve to the same track, and queueing it
    # twice makes the second planned write stale the moment the first lands.
    return sorted(
        {
            candidate.resolve()
            for candidate in resolved.rglob("*")
            if candidate.suffix.lower() in SUPPORTED_EXTENSIONS
            and WRITE_TEMPORARY_MARKER not in candidate.name
            # AppleDouble files macOS leaves on FAT and exFAT USB drives: `._track.mp3`
            # holds Finder metadata, not audio, and would fail every run.
            and not candidate.name.startswith("._")
            and _is_file(candidate)
        }
    )


def _is_file(path: Path) -> bool:
    # Before Python 3.14, a file in a directory that can be listed but not traversed
    # raises EACCES here, which would abort the whole scan over one unreadable entry.
    try:
        return path.is_file()
    except OSError:
        return False
