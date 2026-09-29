# MuQ: recovering the DJ's playlist choices

This follow-up replaces the folder-diversity sample with music the DJ put into
named playlists. It evaluates whether embeddings recover the DJ's later choices
from the preceding context of an ordered playlist. It does not enable live
ranking or infer that every alternative not selected was a bad suggestion.

## September 9, 2026 results

Both models completed all 113 tracks. Across the 43 queries, with each of the
four held-out sets weighted equally:

| Method | Later choices among top five | NDCG@5 | Exact-next hit@5 |
| --- | ---: | ---: | ---: |
| Expected random | 0.28 | 0.060 | 4.8% |
| Other-playlist co-membership/frequency | 0.04 | 0.007 | 0.2% |
| BPM distance | 0.73 | 0.161 | 15.2% |
| EffNet | 0.52 | 0.118 | 7.0% |
| MuQ | 0.31 | 0.063 | 2.5% |
| Playlist + EffNet | 0.10 | 0.015 | 0.0% |
| Playlist + MuQ | 0.09 | 0.018 | 0.0% |

The tested MuQ recipe did not improve overall recovery. Its NDCG@5 was higher
than EffNet on `Skyland24` (0.087 versus 0.027), but lower on `Tocksberg-Actual`,
`Debs-50th`, and `DanceWithMe`. Overall it was close to expected random on
remaining-choice recovery and below random on the stricter exact-next measure.
BPM distance was the strongest method on average in this small test.

The playlist-only baseline had sparse transferable evidence: many target tracks
occurred only in the held-out playlist. Its poor result, and the poor rank-fusion
results, do not establish that personal history is unhelpful generally. They
show that this particular co-membership/frequency rule across five reference
playlists did not recover these held-out choices.

This result gives no reason to promote the tested MuQ embedding recipe into
live ranking. It does not rule out other MuQ layers, supervised predictors,
MuQ-MuLan, or other forms of personal/contextual evidence; those were not tested.
The local `playlist-report.md`, `playlist-results.json`, and
`candidate-examples.md` preserve aggregate results, every query, and one example
per set selected independently of scores.

## Frozen inputs

The canonical SetPath database was opened in SQLite read-only mode. The saved
input contains four documented played sets: `Tocksberg-Actual`, `Debs-50th`,
`DanceWithMe`, and `Skyland24`. `Feelings` and `Prog` provide additional curated
candidates and reference memberships. `Prog` describes genre curation; it is not
treated as proof that all of its members would be played in every context.

Their union contains 115 track identities. Two audio paths were unavailable,
leaving 113 tracks and 12.41 hours of audio. One missing track appears in both
`DanceWithMe` and `Skyland24`; the other is in `Prog`. Missing identities remain
in the saved sequences so their removal cannot invent adjacent transitions.
All 113 available tracks have distinct audio-payload hashes.

The candidate universe deliberately contains the held-out playlist's choices as
well as the other curated tracks. Membership in the held-out playlist is hidden
from scoring, but was used to construct this retrospective candidate universe.
Consequently, this is a controlled recovery exercise, not an unbiased estimate
of discovery quality over the full library or historical catalog availability.

## Evaluation fixed before results

For each of the four played sets, hold its entire membership out of the
playlist-only scoring baseline. At each position after at least three entries:

1. Use exactly the previous three saved entries as context. Skip the query if
   any of those three files is unavailable; do not bridge the gap.
2. Remove every track already in the prefix from the common candidate pool.
3. Treat available, not-yet-played members of the remaining set as known positive
   choices. Evaluate the immediate next entry separately when it is available.
4. Rank the same candidates for every method. Use no playlist title or hidden
   future entry in audio or BPM scoring.

This gives 43 continuation queries: 14 for `Tocksberg-Actual`, 10 for
`Debs-50th`, 11 for `DanceWithMe`, and 8 for `Skyland24`. Exact-next evaluation
has one fewer query because the next track is missing at one usable context.

The primary measure is NDCG@5: it rewards putting the DJ's known later choices
near the top. Also report the expected number of later choices in the top five,
recall@5, exact-next hit@5, and exact-next reciprocal rank. Average queries
within each set, then weight the four sets equally. The 43 prefixes are
correlated observations, not 43 independent performances.

## Compared methods

- **Random:** analytic expected metrics for a uniformly random candidate order.
- **Playlist-only:** co-membership with the three context tracks across the
  other five reference playlists, with frequency across those playlists as a
  secondary score. The full held-out playlist is excluded. Unclassified raw
  Rekordbox audition histories are not used.
- **BPM-only:** absolute distance from the median of the three context BPMs.
- **EffNet / MuQ:** mean cosine similarity to the three context embeddings.
  Extraction recipes are unchanged from the [first experiment](muq-experiment.md).
- **Playlist+EffNet / Playlist+MuQ:** equal reciprocal-rank fusion of the
  playlist-only and audio rankings, with a constant of 60. No weight, layer,
  pooling, or fusion constant is tuned against the held-out results.

For tied scores, metrics use the expected value over all tie orderings, and
fusion uses tie midranks. Human-readable candidate lists necessarily show one
illustrative tie ordering; the reported metrics do not depend on that ordering.

## Reproducing the frozen run

Private inputs and outputs are saved under the git-ignored directory
`.context/model-eval/muq-playlists-2026-09-09/`. The manifest contains exact source
hashes, metadata, and exclusions; `playlists.json` preserves original positions;
`protocol.json` records the evaluation rules. The first experiment supplied six
already-computed embeddings per model, reused only when file path and full-file
SHA-256 matched. All other tracks were analysed under the same recipes.

The existing inference runner accepts this manifest and resumes the two JSONL
files. After both are complete:

```sh
.venv/bin/python scripts/muq_playlist_eval.py \
  --run .context/model-eval/muq-playlists-2026-09-09
```

The command exclusively creates `playlist-results.json` and writes
`playlist-report.md`. Preserve an existing result when rerunning; use a new
directory containing the frozen inputs for another evaluation. No SetPath
database or music tags are modified.

## Interpretation limits

Other playlists may have been edited after the evaluated set. This exercise
does not backdate evidence, bypass SetPath's temporal cutoffs, or claim a
time-causal backtest. Saved playlist order may also differ from the exact
performed order. Better recovery is evidence of compatibility with these saved
choices, not evidence of crowd response, transition quality, or general
superiority. Four sets are too few for a confident broad ranking-policy change.
