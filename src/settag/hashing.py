from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

CHUNK_SIZE = 1024 * 1024

# One (offset, length) window of a file that holds audio rather than metadata.
ByteRange = tuple[int, int]

_MP4_CONTAINER_ATOMS = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_audio(path: Path) -> str:
    """Hash the audio payload, ignoring the metadata wrapped around it.

    Whole-file SHA-256 cannot answer "is this the same recording?" for a tool
    whose entire job is writing tags: every SetTag write changes the file's
    bytes without touching a sample. That made one digest carry two different
    questions — "did the audio change?" and "are these the exact bytes I read?"
    — and answer the first one wrong.

    This digest covers only the samples, so it survives a tag write and a
    rename while still changing on a re-encode, an edit, or a truncation. That
    makes it usable as a track's identity, which is what lets a moved file be
    found again rather than reported missing.

    A container this cannot parse falls back to hashing the whole file. That is
    the old behaviour, so an unrecognized format is no worse off than before.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        for offset, length in audio_ranges(handle, size):
            handle.seek(offset)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                digest.update(chunk)
    return digest.hexdigest()


def audio_ranges(handle: BinaryIO, size: int) -> list[ByteRange]:
    """Locate the audio payload inside an open file.

    Dispatch reads the container's magic bytes rather than the file extension,
    because the extension is the one part of a file a rename can lie about.
    """
    start = _after_id3v2(handle, size)
    handle.seek(start)
    magic = handle.read(12)

    if magic[:4] == b"fLaC":
        return _flac_ranges(handle, start + 4, size)
    if magic[:4] == b"RIFF" and magic[8:12] == b"WAVE":
        return _chunk_ranges(handle, start + 12, size, big_endian=False, wanted=(b"fmt ", b"data"))
    if magic[:4] == b"FORM" and magic[8:12] in (b"AIFF", b"AIFC"):
        return _chunk_ranges(handle, start + 12, size, big_endian=True, wanted=(b"COMM", b"SSND"))
    if magic[4:8] == b"ftyp":
        return _mp4_ranges(handle, start, size)

    # Anything else is treated as an ID3-wrapped elementary stream, which is
    # what an MP3 is once its tags are peeled off either end.
    end = _before_trailers(handle, size)
    return [(start, end - start)] if end > start else []


def _after_id3v2(handle: BinaryIO, size: int) -> int:
    """Return the offset just past a leading ID3v2 tag, or 0 when there is none."""
    handle.seek(0)
    header = handle.read(10)
    if len(header) < 10 or header[:3] != b"ID3":
        return 0
    length = _syncsafe(header[6:10])
    # Bit 4 of the flag byte adds a ten-byte footer that is part of the tag.
    footer = 10 if header[5] & 0x10 else 0
    return min(size, 10 + length + footer)


def _before_trailers(handle: BinaryIO, size: int) -> int:
    """Return the offset where trailing metadata begins.

    ID3v1, APEv2, and Lyrics3 all append themselves to the end of a stream and
    can stack, so this peels repeatedly rather than checking once.
    """
    end = size
    while True:
        peeled = _peel_trailer(handle, end)
        if peeled == end:
            return end
        end = peeled


def _peel_trailer(handle: BinaryIO, end: int) -> int:
    if end >= 128:
        handle.seek(end - 128)
        if handle.read(3) == b"TAG":
            return end - 128
    if end >= 32:
        handle.seek(end - 32)
        footer = handle.read(32)
        if footer[:8] == b"APETAGEX":
            length = int.from_bytes(footer[12:16], "little")
            flags = int.from_bytes(footer[20:24], "little")
            # The footer counts itself; a header is present only when bit 31 is set.
            total = length + (32 if flags & 0x80000000 else 0)
            if 0 < total <= end:
                return end - total
    return end


def _flac_ranges(handle: BinaryIO, start: int, size: int) -> list[ByteRange]:
    offset = start
    while offset + 4 <= size:
        handle.seek(offset)
        header = handle.read(4)
        if len(header) < 4:
            break
        last = bool(header[0] & 0x80)
        length = int.from_bytes(header[1:4], "big")
        offset += 4 + length
        if last:
            break
    end = _before_trailers(handle, size)
    return [(offset, end - offset)] if end > offset else []


def _chunk_ranges(
    handle: BinaryIO,
    start: int,
    size: int,
    *,
    big_endian: bool,
    wanted: tuple[bytes, ...],
) -> list[ByteRange]:
    """Collect the payloads of the named chunks in a RIFF or IFF container.

    The format chunk is hashed alongside the samples so that two files with
    identical sample bytes but different sample rates do not collide.
    """
    order = "big" if big_endian else "little"
    ranges: list[ByteRange] = []
    offset = start
    while offset + 8 <= size:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) < 8:
            break
        name = header[:4]
        # A streamed file can carry a placeholder length; clamp rather than trust it.
        length = min(int.from_bytes(header[4:8], order), size - offset - 8)
        if name in wanted and length > 0:
            ranges.append((offset + 8, length))
        # Chunks are padded to an even boundary, and the pad byte is not payload.
        offset += 8 + length + (length % 2)
    return ranges


def _mp4_ranges(handle: BinaryIO, start: int, size: int) -> list[ByteRange]:
    """Collect every ``mdat`` payload, which is where MP4 keeps its samples.

    Metadata lives in ``moov``, so skipping everything else is what makes an
    M4A tag write invisible to this digest.
    """
    ranges: list[ByteRange] = []
    offset = start
    while offset + 8 <= size:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) < 8:
            break
        length = int.from_bytes(header[:4], "big")
        name = header[4:8]
        payload = offset + 8
        if length == 1:
            extended = handle.read(8)
            if len(extended) < 8:
                break
            length = int.from_bytes(extended, "big")
            payload += 8
        elif length == 0:
            length = size - offset
        if length < 8 or offset + length > size:
            break
        if name == b"mdat" and payload < offset + length:
            ranges.append((payload, offset + length - payload))
        offset += length
    return ranges


def _syncsafe(value: bytes) -> int:
    result = 0
    for byte in value:
        result = (result << 7) | (byte & 0x7F)
    return result
