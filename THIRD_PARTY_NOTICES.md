# Third-party notices

SetTag's own source code is licensed under the GNU Affero General Public
License, version 3 only (`AGPL-3.0-only`). See `LICENSE`.

This document records the separate licensing boundaries that are material to
SetTag's default inference workflow. Third-party packages are installed as
dependencies and retain their own copyright notices and licence files.

## Essentia TensorFlow backend

SetTag uses `essentia-tensorflow`, developed by the Essentia developers and
contributors at the Music Technology Group (MTG), Universitat Pompeu Fabra
(UPF).

- Project: <https://essentia.upf.edu/>
- Source: <https://github.com/MTG/essentia>
- Licence: `AGPL-3.0-only`
- Licensing information:
  <https://essentia.upf.edu/licensing_information.html>

UPF also offers proprietary licensing for applications that cannot comply
with its open-source licensing terms. Prebuilt Essentia distributions can
contain additional native dependencies; those components retain their own
licences and notices.

## External Essentia models

The following model weights and metadata are not part of SetTag and are not
included in its source or Python distributions. `settag models download`
downloads the original files directly from Essentia:

### MAEST embedding model

- Name: `discogs-maest-30s-pw-519l-2`
- Author listed by the upstream metadata: Pablo Alonso
- Weights:
  <https://essentia.upf.edu/models/feature-extractors/maest/discogs-maest-30s-pw-519l-2.pb>
- Metadata:
  <https://essentia.upf.edu/models/feature-extractors/maest/discogs-maest-30s-pw-519l-2.json>

### Discogs519 classification model

- Name: `genre_discogs519-discogs-maest-30s-pw-519l-1`
- Author listed by the upstream metadata: Pablo Alonso
- Weights:
  <https://essentia.upf.edu/models/classification-heads/genre_discogs519/genre_discogs519-discogs-maest-30s-pw-519l-1.pb>
- Metadata:
  <https://essentia.upf.edu/models/classification-heads/genre_discogs519/genre_discogs519-discogs-maest-30s-pw-519l-1.json>

UPF publicly offers its pretrained models for non-commercial use and makes
proprietary licensing available on request. Its public documentation currently
identifies the exact Creative Commons variant inconsistently:

- the dedicated licensing page states `CC BY-NC-ND 4.0`:
  <https://essentia.upf.edu/licensing_information.html#licensing-essentia-models>
- the model catalogue states `CC BY-NC-SA 4.0`:
  <https://essentia.upf.edu/models.html>
- the model repository's licence file uses the ND name and legal text, but
  says adaptation is allowed and links to the SA legal code:
  <https://essentia.upf.edu/models/LICENSE>

The pinned model metadata does not include a licence field, and there is no
model-specific licence file alongside either pinned model. The two referenced
Creative Commons licences are:

- `CC BY-NC-ND 4.0`:
  <https://creativecommons.org/licenses/by-nc-nd/4.0/>
- `CC BY-NC-SA 4.0`:
  <https://creativecommons.org/licenses/by-nc-sa/4.0/>

SetTag downloads and uses the original model files without modifying them.
Downloading the files separately does not change their terms. Both publicly
stated variants limit the public grant to non-commercial use. Until UPF
provides model-specific clarification, SetTag does not assert permission to
redistribute or publish adapted model files. Professional, business, or other
revenue-generating use is not clearly permitted and may require separate
permission from UPF.

The upstream metadata requests citation of:

> Pablo Alonso-Jiménez, Xavier Serra, and Dmitry Bogdanov. "Efficient
> Supervised Training of Audio Transformers for Music Representation
> Learning." Proceedings of the International Society for Music Information
> Retrieval Conference (ISMIR), 2023.

## TensorFlow runtime

The models use the TensorFlow format, and prebuilt `essentia-tensorflow`
packages may include TensorFlow runtime libraries.

- Project: <https://www.tensorflow.org/>
- Source: <https://github.com/tensorflow/tensorflow>
- Copyright: The TensorFlow Authors
- Licence: Apache License 2.0
- Licence text: <https://github.com/tensorflow/tensorflow/blob/master/LICENSE>

TensorFlow's Apache licence covers the TensorFlow software. It does not cover
or override the separate, currently inconsistent public terms for the Essentia
model weights and metadata.

## Other Python dependencies

SetTag declares the following runtime dependencies. They are installed
separately and retain the licence files shipped in their own distributions.

| Dependency | Relationship | Licence | Project |
| --- | --- | --- | --- |
| Mutagen | direct | `GPL-2.0-or-later` | <https://github.com/quodlibet/mutagen> |
| NumPy | direct | `BSD-3-Clause` and licences for bundled components | <https://numpy.org/> |
| Textual | direct | MIT | <https://github.com/Textualize/textual> |
| tomli | direct on Python 3.10 only | MIT | <https://github.com/hukkin/tomli> |
| Rich | transitive through Textual | MIT | <https://github.com/Textualize/rich> |
| markdown-it-py | transitive through Textual and Rich | MIT | <https://github.com/executablebooks/markdown-it-py> |
| mdit-py-plugins | transitive through Textual | MIT | <https://github.com/executablebooks/mdit-py-plugins> |
| mdurl | transitive through markdown-it-py | MIT | <https://github.com/executablebooks/mdurl> |
| linkify-it-py | transitive through markdown-it-py | MIT | <https://github.com/tsutsu3/linkify-it-py> |
| platformdirs | transitive through Textual | MIT | <https://github.com/tox-dev/platformdirs> |
| Pygments | transitive through Textual and Rich | `BSD-2-Clause` | <https://pygments.org/> |
| typing-extensions | transitive through Textual | `PSF-2.0` | <https://github.com/python/typing_extensions> |

This list does not replace the notices included with those packages or with
their transitive and native dependencies.
