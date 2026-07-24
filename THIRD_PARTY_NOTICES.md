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

Essentia states that its pretrained models are available under Creative
Commons Attribution-NonCommercial-NoDerivatives 4.0 International
(`CC BY-NC-ND 4.0`) for non-commercial use, with proprietary licensing
available on request:

- Essentia model licensing:
  <https://essentia.upf.edu/licensing_information.html#licensing-essentia-models>
- Licence deed: <https://creativecommons.org/licenses/by-nc-nd/4.0/>
- Legal code:
  <https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode.en>

SetTag downloads and uses the original model files without modifying them.
Downloading the files separately does not change their licence. The model
licence requires attribution, limits the licensed use to non-commercial
purposes, and does not permit sharing adapted model material.

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
or override the separate `CC BY-NC-ND 4.0` terms for the Essentia model
weights and metadata.

## Other Python dependencies

SetTag declares the following runtime dependencies. They are installed
separately and retain the licence files shipped in their own distributions.

| Dependency | Relationship | Licence | Project |
| --- | --- | --- | --- |
| Mutagen | direct | `GPL-2.0-or-later` | <https://github.com/quodlibet/mutagen> |
| NumPy | direct | `BSD-3-Clause` and licences for bundled components | <https://numpy.org/> |
| Textual | direct | MIT | <https://github.com/Textualize/textual> |
| Rich | transitive through Textual | MIT | <https://github.com/Textualize/rich> |

This list does not replace the notices included with those packages or with
their transitive and native dependencies.
