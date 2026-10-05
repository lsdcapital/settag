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
looks above 12 kHz for a wall that is both steep (18 dB within 300 Hz) and deep (20 dB
below the level just under it, measured against the loudest stretch above it).

A file is a suspect when:

- a lossless container (FLAC, WAV, AIFF, ALAC) has a wall at or below 20.8 kHz;
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
AAC): 23 suspects, 2.6%. Twenty-one are 320 kbps MP3s with a wall between 13.7 and
18.7 kHz, mostly older pop, rock, dance and trance tracks, plus two Beatport purchases
at 17.5 and 18.5 kHz. The others are a genuine 128 kbps MP3 and one low-sample-rate
file. Of the 320 kbps MP3s, 661 show a wall at 19.8-20.1 kHz, the expected LAME 320
lowpass, and 129 show no wall at all. Checking took 0.43 s per track on an Apple-silicon
Mac, read-only and single-threaded.

The suspects have not yet been confirmed in a spectrogram, so there is no measured
false-positive rate. `manual_verdict` in `report.json` is where to record each call.

## Before this moves into SetTag

- Confirm the 23 suspects in Spek and record the false-positive rate here.
- Decide whether a wall in a Beatport purchase is a mislabelled lossy master (worth
  reporting to the store) or a mastering choice.
- In the app it would be a read-only report column and a `settag quality` command,
  not a staged write: no tag edit fixes a transcode. A matching Beatport link would
  give the user a place to buy a proper copy.
