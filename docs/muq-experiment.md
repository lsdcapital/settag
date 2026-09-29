# MuQ versus EffNet: offline retrieval experiment

The experiment compares full-track sonic neighbours in a fixed local collection.
It does not change SetTag's analysis backends, music tags, or SetPath ranking.
Model downloads and the PyTorch environment are separate from the normal install.

The [playlist-based follow-up](muq-playlist-experiment.md) tests recovery of the
DJ's saved choices. It found no overall advantage for the tested MuQ recipe.

## September 9, 2026 pilot

Both models completed all 100 selected tracks: 9.53 hours of audio across 26
folders, selected from 897 discovered audio files. The machine was an Apple M3
Pro with 18 GiB of unified memory. EffNet ran on CPU and MuQ on MPS.

| Measurement | EffNet | MuQ |
| --- | ---: | ---: |
| Inference and pooling | 134.4 s | 507.2 s |
| Decode and initial source check | 69.0 s | 47.4 s |
| Peak process RSS, excluding unaccounted GPU allocations | 1.37 GiB | 3.22 GiB |
| Distinct tracks appearing in top-five lists | 89 | 90 |
| Neighbours sharing the query's folder | 14.6% | 14.6% |

Mean top-five overlap was **31.6%**, or 1.58 shared neighbours per query. MuQ's
inference took about 3.77 times as long in this run. All 100 source-file hashes
were verified unchanged after inference, and 40 playlists were verified for 20
blinded queries. The 13 targeted experiment and embedding tests passed.

These are measured computational results, not a musical-quality verdict. Human
listening judgments are pending. The local report and listening materials are in
`.context/model-eval/muq-2026-09-09/`. The pinned MuQ revision was
`0562a57814f6f8bbd9fdea0a25921a2fce1a841a`.

## Method

- Select 100 tracks using a fixed random seed and round-robin sampling across
  library folders. Exclude the Samples and Acapella folders, tracks shorter than
  one minute or longer than 15 minutes, and duplicate audio-payload hashes.
- This is a diversity sample, not a sample proportional to the library. Folder
  labels can describe acquisition sources rather than genres. Neither folder
  agreement nor crossing folder boundaries measures musical usefulness.
- EffNet uses the existing SetTag model, its native whole-track patch extraction,
  mean pooling, and final L2 normalization. It reads mono audio at 16 kHz.
- MuQ uses `OpenMuQ/MuQ-large-msd-iter`, FP32, and the last hidden state. Decode the
  original audio independently at 24 kHz using Essentia MonoLoader, quality 4.
  Process consecutive 10-second chunks without overlap. Process the final partial
  chunk at its actual length; merge it into the preceding chunk if it is shorter
  than one second. Average all output frames, then normalize once with L2. This
  weights chunks by frame count, rather than giving a short tail equal weight.
- Pooling and layer choice are baseline hypotheses. The result compares these
  specific pipelines; it cannot establish an inherent advantage for either model.
- Retrieve the five highest cosine similarities within each model's space,
  excluding the query itself. Do not compare raw coordinates across models.
- Select up to 20 listening queries across the full range of sampled folders,
  independently of retrieval results. Randomize model-to-A/B assignment per query.
  Each M3U8 playlist contains the seed followed by its five neighbours.
- Judge sonic resemblance and usefulness as a next track separately. Tempo/key
  compatibility, transition segments, intention, and listening history are not
  part of this retrieval experiment.

The runner checks source hashes before and after inference. Outputs flush per
track and can resume under the same recipe. It rejects inconsistent coverage and
source identities before comparison. Evidence uses an experimental format rather
than claiming compatibility with SetPath's current `audio-embedding/v1` contract:
the latter does not identify this chunk/tail recipe.

## Running

From the SetTag repository, with its normal environment and cached EffNet weights:

```sh
run=.context/model-eval/muq-2026-09-09
mkdir -p "$run"
uv venv "$run/venv" --python .venv/bin/python
uv pip install --python "$run/venv/bin/python" \
  'muq==0.1.0' 'transformers<5' 'huggingface-hub<1' \
  'essentia-tensorflow==2.1b6.dev1389' 'mutagen>=1.47,<2'
curl -fsSL https://huggingface.co/api/models/OpenMuQ/MuQ-large-msd-iter \
  -o "$run/model-info.json"
uv pip freeze --python "$run/venv/bin/python" > "$run/requirements.lock.txt"
uv pip freeze --python .venv/bin/python > "$run/effnet-requirements.lock.txt"

.venv/bin/python scripts/muq_experiment.py select \
  --run "$run" --library /path/to/music --count 100
.venv/bin/python scripts/muq_experiment.py effnet --run "$run"
"$run/venv/bin/python" scripts/muq_experiment.py muq --run "$run" --device auto --limit 1
"$run/venv/bin/python" scripts/muq_experiment.py muq --run "$run" --device auto
.venv/bin/python scripts/muq_experiment.py report --run "$run"
```

Use a new run directory for a new selection or recipe. To reproduce an existing
run, retain its manifest, model-info file, recipe files, and dependency locks;
install the locked dependencies rather than resolving the broad constraints
again. The runner downloads the exact Hugging Face revision recorded in
`model-info.json`, storing the checkpoint in the run directory. Explicit `mps`
requires Apple GPU access; `cpu` is also available. Device changes require a new
recipe/run. The smoke track is retained when the remaining tracks run.

Both released MuQ models have CC BY-NC 4.0 weights; the code is MIT. EffNet's
existing model restrictions also apply. The experiment runs inference locally.

## Artifacts and interpretation

All private paths, vectors, checkpoint files, and listening materials live under
the git-ignored `.context/model-eval/` directory:

- `manifest.json`: selected tracks, source hashes, metadata, exclusions, and method.
- `effnet.jsonl`, `muq.jsonl`: one vector and timings per successful track.
- `*-recipe.json`: checkpoint identity, preprocessing, pooling, device, versions.
- `results.json`: neighbours, overlap, folder agreement, and runtime summaries.
- `listening/review.md`: blinded listening instructions and playlist links.
- `listening/ratings.json`: blank judgments for A, B, tie, or neither, plus notes.
- `blind-key.json`: model identities; open only after completing judgments.

Inference timings include model execution, transfers back to CPU, and pooling.
Decode timings include the pre-inference source check. Startup/download time is
separate and appears in the logs. Some timing observations may overlap activity
from the other backend; these are practical run measurements, not controlled
hardware benchmarks. Peak process RSS is recorded in bytes; it is not a complete
measurement of Apple GPU or total unified-memory usage.

Overlap measures how different the candidate lists are, not whether either is
better. Human judgments remain necessary before concluding retrieval quality or
promoting the evidence to SetPath. Historical playlist replay can reject newly
created evidence because of observation-time cutoffs; this experiment does not
backdate observations or claim historical ranking uplift.

Sources: [official MuQ repository](https://github.com/tencent-ailab/MuQ),
[released checkpoint](https://huggingface.co/OpenMuQ/MuQ-large-msd-iter).
