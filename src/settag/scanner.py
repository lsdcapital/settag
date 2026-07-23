from __future__ import annotations

from pathlib import Path


class UnsupportedInputError(ValueError):
    pass


def scan_mp3(path: Path) -> list[Path]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Input does not exist: {resolved}")

    if resolved.is_file():
        if resolved.suffix.lower() != ".mp3":
            raise UnsupportedInputError(f"Only MP3 files are supported: {resolved}")
        return [resolved]

    if not resolved.is_dir():
        raise UnsupportedInputError(f"Input is neither a file nor directory: {resolved}")

    return sorted(
        candidate.resolve()
        for candidate in resolved.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() == ".mp3"
    )
