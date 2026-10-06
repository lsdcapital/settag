# Read-only audio quality prototype

This experiment is separate from the interactive app and all tag-writing paths.
It reads a track or directory, measures where each file's high frequencies stop,
and writes a local JSON/Markdown report. It does not run a model or change tags.

## Run

```sh
.venv/bin/python scripts/quality_check.py ~/Music/Library --output /tmp/quality
```

A directory with more than `--limit` tracks (default 200) is sampled at random with
`--seed`. The output directory contains `report.json` and `report.md`. Reports
contain local file paths and tags; keep personal reports outside the repository.

## Method

Lossy encoders discard content above a lowpass that depends on the bitrate:
about 16 kHz at 128 kbps, 18.5 kHz at 192 kbps, 19.5-20 kHz at 256 kbps or V0,
and 20-20.5 kHz at 320 kbps. Transcoding to a higher bitrate or to FLAC keeps the
original wall, so the wall reveals the weakest encode a file has been through.

SetTag's analysis audio cannot show this: it is decoded mono at 16 kHz, which keeps
nothing above 8 kHz. The check decodes again at the file's own sample rate, averages
the power spectrum of six 10-second windows across the middle 80% of the track, and
looks above 12 kHz for a wall that is both steep (14 dB within 300 Hz) and deep (16 dB
below the level just under it, measured against the loudest stretch above it). The
thresholds were tuned against known transcodes; see Validation.

A file is a suspect when:

- a lossless container (FLAC, WAV, AIFF, ALAC) has a wall at or below 20.4 kHz;
- a lossy file of 256 kbps or more has a wall below 18.8 kHz;
- a CBR lossy file is below 256 kbps, or a VBR or AAC file averages below 160 kbps;
- the sample rate is below 44.1 kHz or the bit depth is below 16.

A wall is a heuristic, not proof. Some masters lowpass on purpose, and old analogue
transfers can roll off steeply. The report suggests a source bitrate for each wall,
which is a guess from common LAME and iTunes lowpass points.

The encoder string is reported but never counts as evidence. ffmpeg's `Lavf` marker
looked like a sign of stream rips, but every Beatport download in the pilot library
carries it.

## Results

Synthetic set (pink noise, encoded with LAME, sox and ffmpeg): all 13 files were
classified as intended. The set had a 128 kbps file re-encoded at 320, a 192 kbps file
re-encoded at 320, FLACs decoded from 128 and 320 kbps MP3s, a 22.05 kHz WAV, a 128
kbps MP3 as suspects, and a FLAC, a WAV, a 320 kbps MP3, a V0 MP3, a 256 kbps AAC, an
ffmpeg 320 kbps MP3 and a WAV with a gentle 6 kHz rolloff as clean.

Pilot library (897 tracks: 828 MP3 at 320 kbps, 59 AIFF, the rest FLAC, WAV, ALAC and
AAC), first run with the original 18/20 dB thresholds: 23 suspects, 2.6%. Twenty-one are 320 kbps MP3s with a wall between 13.7 and
18.7 kHz, mostly older pop, rock, dance and trance tracks, plus two Beatport purchases
at 17.5 and 18.5 kHz. The others are a genuine 128 kbps MP3 and a `.wav` that is
flagged for the wrong reason: it is a WAV with an ID3 tag prepended, which mutagen reads
as a 32 kHz MP3 and ffmpeg refuses to open. That file is broken, just not lossy. Of the 320
kbps MP3s, 661 show a wall at 19.8-20.1 kHz, the expected LAME 320 lowpass, and 129 show no wall at all. Checking took 0.43 s per track on an Apple-silicon
Mac, read-only and single-threaded.

Spectrograms of the 22 decodable suspects (ffmpeg `showspectrumpic`, one minute from
60 s) all show a flat, hard ceiling at the reported wall, with nothing above it for the
whole window: the lossy signature, not a natural rolloff. Two ceilings jump mid-track,
which suggests edits spliced from sources of different quality. This was checked by
eye by Claude, not by the library owner, so it is a second look, not ground truth.

## Validation against known transcodes

`scripts/quality_validate.py` measures the error rates directly. It takes lossless
tracks that pass the check, cuts a 90-second excerpt from the middle of each, and
builds seven files per track with LAME and ffmpeg: three that should pass (the excerpt,
320 CBR, V0) and four that should be flagged (320 from a 128 or 192 source, FLAC from a
128 or 320 source). The truth of every file is known by construction.

```sh
.venv/bin/python scripts/quality_validate.py ~/Music/Library --output /tmp/validate
```

The first run used 40 AIFF sources at the original thresholds (18 dB steep, 20 dB
deep, 20.8 kHz lossless limit): no false positives in 120 clean files, 89% of fakes
caught. Every miss had a real wall just under the thresholds.

To tune, the script saves every file's band levels (`levels.npy`) beside
`validate.json`, so `find_cliff` can be rerun with other thresholds without
re-encoding. The second run used all 65 lossless tracks in the pilot library, 455
files. Thresholds were chosen on one half of the sources and checked on the other:

| Steep / drop, lossless limit | Held-out clean flagged | Held-out fakes caught | All fakes caught |
| --- | --- | --- | --- |
| 18 / 20 dB, 20.8 kHz (original) | 0 of 96 | 118 of 128 | 236 of 260 |
| 12 / 12 dB, 20.8 kHz | 3 of 96 | 128 of 128 | 260 of 260 |
| 14 / 16 dB, 20.8 kHz | 1 of 96 | 128 of 128 | 257 of 260 |
| **14 / 16 dB, 20.4 kHz (chosen)** | **0 of 96** | **128 of 128** | **257 of 260** |

The lowest thresholds looked perfect on the tuning half and failed on the held-out
half, so they were overfitted. The clean files they flagged were lossless sources
with walls at 20.6 and 20.8 kHz, where a 48 to 44.1 kHz resampling filter sits, and
one at 19.8 kHz. Lowering the lossless limit to 20.4 kHz clears the first two, while
every 320-to-FLAC fake still measures 19.8-20.1 kHz. The 20.4 kHz limit was chosen
after seeing the whole set, so that part is not held out.

The 19.8 kHz source ("In This Life", an AIFF) measures 13 dB steep and passes at 14
dB, narrowly. Its wall matches LAME 320, so it may be a real lossy-sourced AIFF rather
than a false positive; the run cannot tell.

On the pilot library the tuned check keeps all 23 earlier suspects and adds three 320
kbps MP3s with walls at 16.0, 17.3 and 18.4 kHz, 26 in all. Their spectrograms show
the same hard ceiling.

## Before this moves into SetTag

- Check "In This Life" against a second copy to settle whether a 19.8 kHz wall in a
  lossless file is a transcode or a master.
- Validate with more than one encoder: every fake here came from LAME, while stores
  and rippers also use iTunes AAC, FhG and ffmpeg's encoders.
- Decide whether a wall in a Beatport purchase is a mislabelled lossy master (worth
  reporting to the store) or a mastering choice.
- In the app it would be a read-only report column and a `settag quality` command,
  not a staged write: no tag edit fixes a transcode. A matching Beatport link would
  give the user a place to buy a proper copy.
