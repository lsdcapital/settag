import { createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/')({ component: Home })

const REPO = 'https://github.com/lsdcapital/settag'

/** Illustrative staged changes. Track names are invented, not real releases. */
type Staged = {
  file: string
  field: string
  from: string
  to: string
  confidence: number
}

const STAGED: Staged[] = [
  {
    file: 'Sundial — Marine Layer.flac',
    field: 'genre',
    from: 'House',
    to: 'Deep House',
    confidence: 0.82,
  },
  {
    file: 'Kestrel — Night Vent.aiff',
    field: 'genre',
    from: 'not set',
    to: 'Techno',
    confidence: 0.74,
  },
  {
    file: 'Ferrous — Slow Tide.mp3',
    field: 'mood',
    from: 'not set',
    to: 'dark, hypnotic',
    confidence: 0.61,
  },
]

function Meter({ value }: { value: number }) {
  const filled = Math.round(value * 10)
  return (
    <span className="meter" aria-label={`confidence ${value.toFixed(2)}`}>
      <span className="meter__fill" aria-hidden="true">
        {'█'.repeat(filled)}
      </span>
      <span className="meter__rest" aria-hidden="true">
        {'█'.repeat(10 - filled)}
      </span>
      <span className="meter__value">{value.toFixed(2)}</span>
    </span>
  )
}

function Home() {
  return (
    <>
      <header className="bar">
        <div className="shell bar__inner">
          <span className="bar__mark">settag</span>
          <span className="bar__meta">v0.1.0 · AGPL-3.0 · Python 3.10–3.14</span>
        </div>
      </header>

      <main>
        <section className="shell hero">
          <p className="eyebrow">Local analysis for DJ libraries</p>
          <h1 className="hero__title">Nothing gets written until you say so.</h1>
          <p className="hero__lead">
            SetTag runs Essentia&rsquo;s MAEST model over your tracks on your own machine, proposes
            genre, mood, and instrument tags, and holds every change in a staging list. You read the
            diff. You decide what lands.
          </p>

          <section className="ledger" aria-label="Example staged changes">
            <div className="ledger__head">
              <span className="ledger__label">Staged changes</span>
              <span className="ledger__path">~/Music/crate</span>
            </div>
            <ul className="ledger__rows">
              {STAGED.map((change) => (
                <li className="row" key={change.file}>
                  <span className="row__file">{change.file}</span>
                  <div className="row__change">
                    <span className="row__field">{change.field}</span>
                    <span className="row__from">{change.from}</span>
                    <span className="row__arrow" aria-hidden="true">
                      →
                    </span>
                    <span className="row__to">{change.to}</span>
                    <Meter value={change.confidence} />
                  </div>
                </li>
              ))}
              <li className="row">
                <span className="row__file">Palm Reader — Cassia.m4a</span>
                <div className="row__change">
                  <span className="row__field">genre</span>
                  <span className="row__from">Breakbeat</span>
                  <span className="row__hold">already correct, left alone</span>
                </div>
              </li>
            </ul>
            <div className="ledger__foot">
              <span className="ledger__written">0 files written</span>
              <span aria-hidden="true">·</span>
              <span>3 changes staged</span>
              <span aria-hidden="true">·</span>
              <span>1 unchanged</span>
            </div>
          </section>

          <div className="command">
            <code className="command__line">
              <span className="command__prompt">$ </span>uv tool install settag
            </code>
            <span className="command__note">macOS and Linux x86_64. No Windows.</span>
          </div>
        </section>

        <section className="band band--sunk">
          <div className="shell">
            <h2 className="section-label">How a run goes</h2>
            <div className="pipeline">
              <span className="pipeline__step">scan</span>
              <span className="pipeline__link" aria-hidden="true">
                ──
              </span>
              <span className="pipeline__step">analyze</span>
              <span className="pipeline__link" aria-hidden="true">
                ──
              </span>
              <span className="pipeline__step">stage</span>
              <span className="pipeline__link" aria-hidden="true">
                ──
              </span>
              <span className="pipeline__step">review</span>
              <span className="pipeline__link" aria-hidden="true">
                ──
              </span>
              <span className="pipeline__step pipeline__step--gated">write, on approval</span>
            </div>
            <p className="pipeline__note">
              Analysis and writing are separate steps, and only the last one touches your files.
              Re-running is cheap: SetTag records what it analyzed and with which settings, so
              unchanged tracks are skipped and changed ones are marked stale.
            </p>
          </div>
        </section>

        <section className="band">
          <div className="shell split">
            <div>
              <h2 className="block__title">What it reads</h2>
              <p className="block__body">
                MP3, AIFF, and WAV through ID3. FLAC through Vorbis comments. M4A, M4B, and MP4
                through MP4 atoms.
              </p>
              <p className="block__body">
                Tags are written with Mutagen in each container&rsquo;s native scheme, so the files
                stay readable by Rekordbox, Serato, and anything else that reads standard metadata.
              </p>
            </div>
            <div>
              <h2 className="block__title">What it runs</h2>
              <p className="block__body">
                MAEST for genre by default. Discogs-EffNet heads add mood, theme, and instrument
                evidence when you ask for them.
              </p>
              <p className="block__body">
                Inference happens on your machine. Model files download once into{' '}
                <code>~/.cache/settag/models</code> and are checked against pinned SHA-256 digests
                before anything loads. Nothing about your library leaves the machine.
              </p>
            </div>
          </div>
        </section>

        <section className="band band--sunk">
          <div className="shell">
            <h2 className="section-label">Install</h2>
            <ol className="steps">
              <li>
                <code className="steps__line">$ uv tool install settag</code>
                <span className="steps__gloss">Needs Python 3.10–3.14.</span>
              </li>
              <li>
                <code className="steps__line">$ settag models download</code>
                <span className="steps__gloss">
                  Fetches the genre model. Add{' '}
                  <code>--tasks genre,mood-theme,instrument</code> for the rest.
                </span>
              </li>
              <li>
                <code className="steps__line">$ settag ~/Music/crate</code>
                <span className="steps__gloss">
                  Opens the review app. Pass a single file or a whole directory.
                </span>
              </li>
            </ol>

            <div className="table-scroll">
              <table className="table">
                <caption className="section-label">Platform support</caption>
                <thead>
                  <tr>
                    <th scope="col">Platform</th>
                    <th scope="col">Requirement</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>macOS, Apple Silicon</td>
                    <td>macOS 15 (Sequoia) or newer</td>
                  </tr>
                  <tr>
                    <td>macOS, Intel</td>
                    <td>macOS 14 (Sonoma) or newer; macOS 15 on Python 3.14</td>
                  </tr>
                  <tr>
                    <td>Linux, x86_64</td>
                    <td>glibc 2.17 or newer</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p className="unsupported">
              Windows and Linux on ARM cannot run SetTag. The analysis backend,{' '}
              <code>essentia-tensorflow</code>, has never published wheels for either, and there is
              no source build worth attempting.
            </p>
          </div>
        </section>

        <section className="band">
          <div className="shell">
            <h2 className="section-label">Before you use this at work</h2>
            <div className="notice">
              <p className="notice__body">
                SetTag itself is AGPL-3.0-only. The models are not SetTag&rsquo;s to license. UPF
                offers the Essentia model weights for non-commercial use, but its own documentation
                names the Creative Commons variant inconsistently, and the model metadata does not
                specify one at all.
              </p>
              <p className="notice__body">
                Personal, educational, and research use likely falls within those terms.
                Professional or revenue-generating use is not clearly permitted and may need
                separate permission from UPF, or a different analysis backend. Downloading the
                models yourself does not change their terms.
              </p>
              <p className="notice__body">
                <a href="https://essentia.upf.edu/licensing_information.html">
                  Essentia licensing information
                </a>{' '}
                · <a href={`${REPO}/blob/main/THIRD_PARTY_NOTICES.md`}>Third-party notices</a>
              </p>
            </div>
          </div>
        </section>
      </main>

      <footer className="shell foot">
        <a href={REPO}>Source</a>
        <a href={`${REPO}/issues`}>Issues</a>
        <a href="https://pypi.org/project/settag/">PyPI</a>
        <a href={`${REPO}/blob/main/LICENSE`}>AGPL-3.0-only</a>
      </footer>
    </>
  )
}
