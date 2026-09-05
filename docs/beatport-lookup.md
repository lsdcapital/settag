# Read-only Beatport lookup prototype

This experiment is separate from the interactive app and all tag-writing paths.
It reads an explicitly selected MP3 sample, fetches public Beatport metadata, and
writes a local JSON/Markdown report. It does not run audio inference or change tags.

## Run

Create a JSON array of absolute MP3 paths, then run from the project environment:

```sh
.venv/bin/python scripts/beatport_lookup.py --paths sample.json --output /tmp/beatport-pilot
```

The default limit is 30 unique files; `--limit` permits 1–50. The output directory
contains `report.json`, `report.md`, and a public-page cache. `--offline` repeats
the comparison using cached responses only. Reports contain local file paths and
tags; keep personal reports outside the repository.

## Identity policy

- A stored `BEATPORT_TRACK_ID` leads to that track's page. Otherwise search by ISRC
  when available, then artist/title. Identifiers never override contradictory metadata.
- Compare complete normalized titles and artist sets, not substrings or score bonuses.
- Compare named remix/version text separately. An unmarked title can match Original
  Mix when all other checks pass; Extended, Radio, Club, VIP, edits, and named remixes
  remain distinct. Conflicting remixers are a hard rejection.
- Both durations must be present, finite and positive, and within five seconds.
- Missing or conflicting evidence remains unresolved. Multiple plausible releases
  remain ambiguous, even when their genre strings agree. Do not choose the first result.
- Fetch the exact track page before proposing a single candidate's primary genre.
  Detail parsing is scoped to that track ID and ignores recommendations and charts.
- Report the original genre, stored model predictions, candidates, reasons, and
  source URLs. A proposal is not an assertion of measured correctness or a staged write.

## Access and resource limits

The provider reads structured JSON embedded in public search and detail pages.
It uses standard HTTPS with certificate verification and an identifying User-Agent;
it does not impersonate a browser or solve access challenges. There is no API key,
login, audio upload, or third-party search engine. Search terms leave the computer.

Requests are serial, spaced at least one second apart, with a 15-second timeout,
an 8 MiB response cap, and 60 HTTP attempts per run. Server errors get at most two
retries with backoff. HTTP 401/403/429 stops the batch. An unavailable response
format also stops it and saves one diagnostic public page. Later tracks are marked
`not_attempted`; neither failures nor unattempted tracks count as catalog misses.

Successfully parsed pages are cached for seven days. Cached content is parsed and
matched again; failed responses are not cached as empty search results. This reduces
repeat requests while allowing parser and matching fixes to be tested offline.

## Limits before promotion

The public response format is not a supported API contract. The parser has been
adapted to observed search and track-detail data, but site changes can break it.
Strict matching deliberately misses harmless title/credit variations. ISRC search
can return no results even when title search finds the recording. Multiple releases
can share an ISRC and recording while carrying different Beatport genre labels.

This prototype does not infer the intended release from album, label, release year,
or popularity; those would need independent evaluation. It does not paginate search
results, so unresolved does not mean absent from Beatport. It proposes the primary
genre only. It has no automatic source-priority promotion and no audio-model fallback.

Measure proposal coverage, ambiguity, incorrect identities, and manually verified
genre agreement separately. A small hand-selected sample is not a library-wide
accuracy estimate. Use cached report evidence to review false matches before adding
any application UI or write integration.

The implementation is original. CuePoint, DJ Tagger, and Beatport Genre Sync were
reviewed for workflow ideas and failure cases; their matching or writing code was
not incorporated. Software licensing does not confer Beatport catalog/service rights.

## First pilot (2026-09-05)

Selected 30 real MP3s: four previously checked references, 16 recognizable tracks
selected with a fixed random seed, one blank-genre track found in the folder, and
nine other randomly selected tracks. This is a deliberately small, nonrepresentative
sample. No music tags were written.

- 12 single-release proposals: seven agree with the existing genre, five differ.
- 10 ambiguous results: multiple releases passed the identity checks.
- Eight unresolved: no candidate passed every required check.
- Final online pass: 121.486 seconds, 46 HTTP requests, four cache hits from parser
  development. This is not a cold-cache benchmark or a throughput guarantee.
- A cache-only replay produced the same decisions with zero HTTP requests.

The public search and detail formats differed from the integrations reviewed:
search credits include both artists and remixers, with role fields; current detail
data uses `track_length_ms` and a track-specific query key. The prototype was adapted
against observed responses before completing this pass. An initial curl request
received HTTP 403; the standard HTTPS prototype subsequently worked. Access remains
variable and no challenge was bypassed.

An instructive case is Return to Oz (ARTBAT Remix): search returns releases with
the same ISRC and duration but different genre labels, including Melodic House &
Techno and Dance / Pop. ISRC agreement establishes neither the intended release nor
the correct genre for the library. The result remains ambiguous.

These counts measure coverage and agreement with current tags, not externally
validated precision. The report leaves manual accuracy unset. Before promotion,
review the 12 proposed identities and the ambiguous releases independently.

## Authenticated follow-up (2026-09-05)

A temporary personal-account OAuth experiment successfully accessed Beatport's
catalog API. Credentials were entered through a hidden prompt, kept in process
memory, and discarded when the experiment exited; none were added to repository
configuration or reports. The catalog adapter was temporary, not an authentication
feature added to the application.

The same 30 tracks and identity rules produced identical statuses and proposed
track IDs/genres: 12 proposals, 10 ambiguous, eight unresolved. Authenticated lookup
took 136.350 seconds for 50 fresh requests and zero cache hits, excluding login and
a separate one-request access probe. Both experiments used a one-second request
pause. The public-page run had four cache hits, so these are not controlled speed
benchmarks; no speed, coverage, or accuracy improvement was demonstrated.

The benefit established by this trial is working structured catalog access without
HTML parsing or access failures during the run. Authentication did not resolve
multiple-release genre disagreements. This personal documentation-client route
does not establish a supported third-party API agreement or future availability.

## Unified enrichment

Open `settag /path/to/music`, select tracks and press **R — Enrich**. One background
operation combines catalog lookup and audio analysis, displays progress, and saves
results in the local workbench for review. Existing audio evidence is reused when
its model/configuration is current. Audio analysis runs when it is missing or stale.
Missing model files do not prevent useful catalog results from reaching review.

`settag enrich /path/to/music` opens that same app. `--no-tui` performs the same
combined operation as a plain dry run. The temporary catalog-only command and its
required plan/report/cache arguments have been removed. The original pilot script
remains a research tool, and `analyze` remains an advanced raw model export tool.

Beatport pages are cached for seven days in `~/.cache/settag/beatport`; set
`SETTAG_BEATPORT_CACHE` to use a different directory. Each selection has a 300-attempt
request budget, serial requests, one-second spacing and bounded request timeouts.
An access failure stops further catalog requests for the selection, while audio
results remain available. Starting another selection allows a retry. Cancellation
is checked between tracks, sources and release-detail requests; an in-flight request
finishes under its timeout. Completed results remain reviewable.

Fresh, verified catalog matches take priority in the proposed standard Genre field.
All labels from matching releases are staged as multiple genre values. An existing
Beatport-supported genre stays first; otherwise stored release order determines the
first value without implying confidence. Manual choices remain intact. Without a
verified match, model suggestions only fill blank genres. Existing model evidence
is preserved. Review shows multiple Beatport genres, source links and source failures. No tags are written until the normal Write
action; verified writes and undo use the same implementation as before.

Catalog evidence is stored in `SETTAG_BEATPORT` JSON (ID3 TXXX, Vorbis custom field,
or MP4 `----:com.lsdcapital.settag:BEATPORT`):

- `schema`: `settag.beatport/v1`.
- `status`: `consensus` or `conflicting_genres` across verified matching releases.
- `agreed_genres`: labels present on every verified matching release.
- `alternative_genres`: other observed labels; equally retained in the proposed genre values.
- `sources`: exact track IDs, URLs, titles, credits, mixes, durations, ISRCs and genres.

Every plausible identity match must pass exact detail-page verification before an
outcome is staged. A failed detail lookup cannot turn disagreement into consensus.
Agreement applies to the matches found; lookup does not claim exhaustive catalog
coverage or musical accuracy. Repeating identical enrichment produces no new catalog
tag changes. Request observation times live in the page cache. Source notices persist
with the workbench plan, and subsequent audio analysis preserves the catalog field.

Beatport receives artist/title or ISRC search text; audio is not uploaded. The public
provider is used without login; the authenticated experiment remains separate.
SetPath does not yet import or rank with this field.

## Enrichment contract v2

`SETTAG_ENRICHMENT` now stores `settag.enrichment/v2` separately from existing audio
provenance. The application version remains 0.2.1; this is not an application release.
Older model-only or unversioned enrichment results need enrichment, but compatible
audio predictions are retained, including valid pending workbench results.

The record contains `audio` (`complete` or `unavailable`) and a `catalog` check:
`status` (`matched`, `no_match`, or `unavailable`), `checked_at` (UTC epoch seconds for
a completed check), and an optional failure reason. “No match” means a completed
lookup found no verified match, not that an access failure proved catalog absence.

“Analysis: Current” now requires compatible audio evidence, the current enrichment
contract, and a completed catalog check less than seven days old. Failed sources
show “Partial” and remain eligible for retry. Missing/older contracts and expired
checks show “Needs enrichment.” Matching catalog checks also require their stored
source evidence to remain readable. Fresh catalog checks can be reused while only
missing/stale audio is refreshed. Fresh audio is reused while catalog checks refresh.

The catalog timestamp is the oldest successful page observation used by the lookup,
including cached pages; cache replay does not reset its freshness. Source disagreements
are still separate from freshness: conflicting genres can be freshly checked and
still need genre review. Verified catalog genres are preferred; standard genre edits still require Write.
