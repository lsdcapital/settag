"""Audio quality notes read from the container header, without decoding audio.

These catch what the header can prove: a low bitrate, a low sample rate or bit
depth, and a file whose contents do not match its extension. A 320 kbps file
transcoded from 128 kbps looks clean here; finding that needs the spectrum (see
docs/quality-check.md). Notes are report-only, since no tag edit improves audio.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3, BitrateMode
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

# Constant-bitrate MP3 below this is audibly worse on a club system; 256 and 320
# are what stores sell.
MIN_CBR_KBPS = 256
# VBR MP3 and AAC report an average, and V0 or iTunes 256 VBR can average 180-240
# kbps on sparse material, so only clearly low averages are noted.
MIN_VBR_KBPS = 160
MIN_SAMPLE_RATE = 44_100
MIN_BITS_PER_SAMPLE = 16

_EXPECTED_CONTAINERS: dict[str, type] = {
    ".mp3": MP3,
    ".flac": FLAC,
    ".wav": WAVE,
    ".wave": WAVE,
    ".aif": AIFF,
    ".aiff": AIFF,
    ".m4a": MP4,
    ".m4b": MP4,
    ".mp4": MP4,
}


def header_quality_notes(path: Path, audio: Any) -> tuple[str, ...]:
    """Notes for one file, given the mutagen object its tags were read from."""
    expected = _EXPECTED_CONTAINERS.get(path.suffix.lower())
    if expected is not None and not isinstance(audio, expected):
        # The header was parsed as the wrong format, so its rates are not to be trusted.
        found = type(audio).__name__
        return (f"contents read as {found}, not {path.suffix.lower().lstrip('.').upper()}",)

    info = audio.info
    notes: list[str] = []
    kbps = round((getattr(info, "bitrate", 0) or 0) / 1000)
    if isinstance(audio, MP3):
        variable = info.bitrate_mode in {BitrateMode.VBR, BitrateMode.ABR}
        if kbps < (MIN_VBR_KBPS if variable else MIN_CBR_KBPS):
            notes.append(f"low bitrate ({kbps} kbps{' average' if variable else ''})")
    elif isinstance(audio, MP4) and getattr(info, "codec", "") != "alac":
        if kbps < MIN_VBR_KBPS:
            notes.append(f"low bitrate ({kbps} kbps average)")

    sample_rate = getattr(info, "sample_rate", 0) or 0
    if sample_rate and sample_rate < MIN_SAMPLE_RATE:
        notes.append(f"low sample rate ({sample_rate:,} Hz)")
    bits = getattr(info, "bits_per_sample", 0) or 0
    if bits and bits < MIN_BITS_PER_SAMPLE:
        notes.append(f"{bits}-bit audio")
    return tuple(notes)
