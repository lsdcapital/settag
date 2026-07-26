from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelFile:
    role: str
    filename: str
    url: str
    sha256: str


@dataclass(frozen=True)
class ModelSpec:
    """One classifier head plus the embedding model it runs on.

    ``vocabulary`` names the label taxonomy the head emits. A consumer reading
    SetTag's tags sees labels but cannot tell which taxonomy produced them, and
    the field names do not carry that information: ``SETTAG_MOOD_THEME`` stays
    spelled the same if this head is ever swapped for one with a different
    label set. Only the producer knows, so SetTag declares it rather than
    leaving consumers to infer it from a field name and silently treat two
    taxonomies as one.
    """

    id: str
    license: str
    vocabulary: str
    embedding_output: str
    classifier_input: str
    classifier_output: str
    sample_rate: int
    files: tuple[ModelFile, ...]

    def file(self, role: str) -> ModelFile:
        try:
            return next(item for item in self.files if item.role == role)
        except StopIteration as error:
            raise KeyError(f"Unknown model file role: {role}") from error

    def path(self, model_dir: Path, role: str) -> Path:
        return model_dir / self.file(role).filename


BASE_URL = "https://essentia.upf.edu/models"

DISCOGS519_MAEST = ModelSpec(
    id="essentia/genre-discogs519-maest/v1",
    license="CC BY-NC-ND 4.0",
    vocabulary="discogs519",
    embedding_output="PartitionedCall/Identity_12",
    classifier_input="embeddings",
    classifier_output="PartitionedCall/Identity_1",
    sample_rate=16_000,
    files=(
        ModelFile(
            role="embedding",
            filename="discogs-maest-30s-pw-519l-2.pb",
            url=f"{BASE_URL}/feature-extractors/maest/discogs-maest-30s-pw-519l-2.pb",
            sha256="92783feb21187443d058b4f16d7a76f47888d43fbdc7a28e8bcc8e024603bd20",
        ),
        ModelFile(
            role="embedding_metadata",
            filename="discogs-maest-30s-pw-519l-2.json",
            url=f"{BASE_URL}/feature-extractors/maest/discogs-maest-30s-pw-519l-2.json",
            sha256="83240aa553ffb491b0ec5a24565eb612553e5f38da5207403c25b890c5b34acd",
        ),
        ModelFile(
            role="classifier",
            filename="genre_discogs519-discogs-maest-30s-pw-519l-1.pb",
            url=f"{BASE_URL}/classification-heads/genre_discogs519/"
            "genre_discogs519-discogs-maest-30s-pw-519l-1.pb",
            sha256="0f5d61d9b62e4a27dac058926e986eb424dca0fdb920c066ab53158229cff498",
        ),
        ModelFile(
            role="classifier_metadata",
            filename="genre_discogs519-discogs-maest-30s-pw-519l-1.json",
            url=f"{BASE_URL}/classification-heads/genre_discogs519/"
            "genre_discogs519-discogs-maest-30s-pw-519l-1.json",
            sha256="07015a89f1a0e9b7cdceb63933783023d85f3ac4b36ce5c1b5488bd1fbad2304",
        ),
    ),
)

DISCOGS_EFFNET_MOOD_THEME = ModelSpec(
    id="essentia/mtg-jamendo-moodtheme-discogs-effnet/v2",
    license="CC BY-NC-ND 4.0",
    vocabulary="mtg-jamendo-moodtheme",
    embedding_output="PartitionedCall:1",
    classifier_input="model/Placeholder",
    classifier_output="model/Sigmoid",
    sample_rate=16_000,
    files=(
        ModelFile(
            role="embedding",
            filename="discogs-effnet-bs64-1.pb",
            url=f"{BASE_URL}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
            sha256="3ed9af50d5367c0b9c795b294b00e7599e4943244f4cbd376869f3bfc87721b1",
        ),
        ModelFile(
            role="embedding_metadata",
            filename="discogs-effnet-bs64-1.json",
            url=f"{BASE_URL}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.json",
            sha256="a35003202384735c33154e20264267f9941705218a7b93202b655a1d408d4ff6",
        ),
        ModelFile(
            role="classifier",
            filename="mtg_jamendo_moodtheme-discogs-effnet-1.pb",
            url=f"{BASE_URL}/classification-heads/mtg_jamendo_moodtheme/"
            "mtg_jamendo_moodtheme-discogs-effnet-1.pb",
            sha256="03f2b047020aee4ab39f8880da7bdae2a36d06a1508d656c6d424ad4d6de07a9",
        ),
        ModelFile(
            role="classifier_metadata",
            filename="mtg_jamendo_moodtheme-discogs-effnet-1.json",
            url=f"{BASE_URL}/classification-heads/mtg_jamendo_moodtheme/"
            "mtg_jamendo_moodtheme-discogs-effnet-1.json",
            sha256="d62cd90263e4d613fa7fcce7a831e339450394794af63685f96e065c1a896ab0",
        ),
    ),
)

DISCOGS_EFFNET_INSTRUMENT = ModelSpec(
    id="essentia/mtg-jamendo-instrument-discogs-effnet/v2",
    license="CC BY-NC-ND 4.0",
    vocabulary="mtg-jamendo-instrument",
    embedding_output="PartitionedCall:1",
    classifier_input="model/Placeholder",
    classifier_output="model/Sigmoid",
    sample_rate=16_000,
    files=(
        ModelFile(
            role="embedding",
            filename="discogs-effnet-bs64-1.pb",
            url=f"{BASE_URL}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
            sha256="3ed9af50d5367c0b9c795b294b00e7599e4943244f4cbd376869f3bfc87721b1",
        ),
        ModelFile(
            role="embedding_metadata",
            filename="discogs-effnet-bs64-1.json",
            url=f"{BASE_URL}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.json",
            sha256="a35003202384735c33154e20264267f9941705218a7b93202b655a1d408d4ff6",
        ),
        ModelFile(
            role="classifier",
            filename="mtg_jamendo_instrument-discogs-effnet-1.pb",
            url=f"{BASE_URL}/classification-heads/mtg_jamendo_instrument/"
            "mtg_jamendo_instrument-discogs-effnet-1.pb",
            sha256="2e8c3003c722e098da371b6a1f7ad0ce62fac0dcfc09c7c7997d430941196c2a",
        ),
        ModelFile(
            role="classifier_metadata",
            filename="mtg_jamendo_instrument-discogs-effnet-1.json",
            url=f"{BASE_URL}/classification-heads/mtg_jamendo_instrument/"
            "mtg_jamendo_instrument-discogs-effnet-1.json",
            sha256="7d02204c6451b5615e2968ec6364bbae3b915c886e608f05f00d3a38dc5177c4",
        ),
    ),
)

MODEL_SPECS_BY_TASK = {
    "genre": DISCOGS519_MAEST,
    "mood-theme": DISCOGS_EFFNET_MOOD_THEME,
    "instrument": DISCOGS_EFFNET_INSTRUMENT,
}
