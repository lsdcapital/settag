"""Tests for the audio digest that gives a track an identity.

The property under test throughout is that the digest tracks the samples and
nothing else: it must survive the metadata edits SetTag itself performs, and it
must still change when the audio does. Whole-file SHA-256 gets the first half
wrong, which is what made a renamed or retagged file look like a missing one.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest
from mutagen.id3 import ID3, TIT2, TPE1

from settag.hashing import sha256_audio, sha256_file


def _silent_wav(path: Path, *, seconds: float = 2.0, rate: int = 8_000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * int(rate * seconds))


def test_tag_write_leaves_the_audio_digest_alone(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    before_audio = sha256_audio(path)
    before_file = sha256_file(path)

    tags = ID3()
    tags.add(TPE1(encoding=3, text=["An Artist"]))
    tags.add(TIT2(encoding=3, text=["A Title"]))
    tags.save(path)

    assert sha256_file(path) != before_file, "expected the tag write to change the file"
    assert sha256_audio(path) == before_audio


def test_audio_digest_changes_with_the_samples(tmp_path: Path) -> None:
    shorter = tmp_path / "shorter.wav"
    longer = tmp_path / "longer.wav"
    _silent_wav(shorter, seconds=2.0)
    _silent_wav(longer, seconds=3.0)

    assert sha256_audio(shorter) != sha256_audio(longer)


def test_audio_digest_changes_with_the_sample_rate(tmp_path: Path) -> None:
    """The format chunk is covered, so identical bytes at a different rate differ.

    Without it, a resampled file whose payload happened to match would be
    treated as the same recording.
    """
    slow = tmp_path / "slow.wav"
    fast = tmp_path / "fast.wav"
    _silent_wav(slow, seconds=1.0, rate=8_000)
    _silent_wav(fast, seconds=1.0, rate=8_000)
    assert sha256_audio(slow) == sha256_audio(fast)

    _silent_wav(fast, seconds=1.0, rate=16_000)
    assert sha256_audio(slow) != sha256_audio(fast)


def test_audio_digest_ignores_trailing_metadata_chunks(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    before = sha256_audio(path)

    # A trailing RIFF chunk, which is where WAV keeps things like cue points.
    with path.open("ab") as handle:
        handle.write(b"cue " + struct.pack("<I", 4) + b"\0\0\0\0")

    assert sha256_audio(path) == before


def test_audio_digest_detects_truncated_samples(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    before = sha256_audio(path)

    with path.open("r+b") as handle:
        handle.truncate(path.stat().st_size // 2)

    assert sha256_audio(path) != before


def test_id3v2_prefix_does_not_shift_the_digest(tmp_path: Path) -> None:
    """An MP3-style leading tag is skipped rather than hashed.

    Growing an ID3v2 tag moves every audio byte later in the file, so a digest
    that included it would change on a tag write even though nothing else did.
    """
    bare = tmp_path / "bare.mp3"
    tagged = tmp_path / "tagged.mp3"
    frames = b"\xff\xfb\x90\x00" + b"\x00" * 1024
    bare.write_bytes(frames)

    tags = ID3()
    tags.add(TIT2(encoding=3, text=["A Title"]))
    tagged.write_bytes(frames)
    tags.save(tagged)

    assert tagged.stat().st_size > bare.stat().st_size
    assert sha256_audio(tagged) == sha256_audio(bare)


def test_id3v1_trailer_does_not_shift_the_digest(tmp_path: Path) -> None:
    path = tmp_path / "track.mp3"
    frames = b"\xff\xfb\x90\x00" + b"\x00" * 1024
    path.write_bytes(frames)
    before = sha256_audio(path)

    trailer = b"TAG" + b"A Title".ljust(30, b"\0") + b"\0" * 95
    assert len(trailer) == 128
    with path.open("ab") as handle:
        handle.write(trailer)

    assert sha256_audio(path) == before


def test_unparsable_container_falls_back_to_the_whole_file(tmp_path: Path) -> None:
    """An unrecognized format is no worse off than before this existed."""
    path = tmp_path / "mystery.bin"
    path.write_bytes(b"not a container at all")

    assert sha256_audio(path) == sha256_file(path)


@pytest.mark.parametrize("seconds", [0.05, 2.0])
def test_audio_digest_is_stable_across_reads(tmp_path: Path, seconds: float) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path, seconds=seconds)

    assert sha256_audio(path) == sha256_audio(path)
