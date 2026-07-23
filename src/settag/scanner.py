from __future__ import annotations

from pathlib import Path


class UnsupportedInputError(ValueError):
    pass


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

    return sorted(
        candidate.resolve()
        for candidate in resolved.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_EXTENSIONS
    )
