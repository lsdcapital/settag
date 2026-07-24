# Audio fixtures

`tagged.flac` and `tagged.m4a` contain 250 milliseconds of silence with an
ordinary title (`Original title`) and genre (`Existing genre`). They contain no
SetTag-owned metadata.

The real-container tests copy these files into pytest's temporary directory
before exercising SetTag's Mutagen writers. This keeps FFmpeg out of the test
and runtime dependency sets while testing actual FLAC Vorbis comments and MP4
freeform atoms.

The fixtures were generated with:

```sh
ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 0.25 \
  -metadata title="Original title" \
  -metadata genre="Existing genre" \
  -c:a flac tagged.flac

ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 0.25 \
  -metadata title="Original title" \
  -metadata genre="Existing genre" \
  -c:a aac tagged.m4a
```
