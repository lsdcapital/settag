from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelFile:
    role: str
    filename: str
    url: str


@dataclass(frozen=True)
class ModelSpec:
    id: str
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
    embedding_output="PartitionedCall/Identity_12",
    classifier_input="embeddings",
    classifier_output="PartitionedCall/Identity_1",
    sample_rate=16_000,
    files=(
        ModelFile(
            role="embedding",
            filename="discogs-maest-30s-pw-519l-2.pb",
            url=f"{BASE_URL}/feature-extractors/maest/discogs-maest-30s-pw-519l-2.pb",
        ),
        ModelFile(
            role="embedding_metadata",
            filename="discogs-maest-30s-pw-519l-2.json",
            url=f"{BASE_URL}/feature-extractors/maest/discogs-maest-30s-pw-519l-2.json",
        ),
        ModelFile(
            role="classifier",
            filename="genre_discogs519-discogs-maest-30s-pw-519l-1.pb",
            url=f"{BASE_URL}/classification-heads/genre_discogs519/"
            "genre_discogs519-discogs-maest-30s-pw-519l-1.pb",
        ),
        ModelFile(
            role="classifier_metadata",
            filename="genre_discogs519-discogs-maest-30s-pw-519l-1.json",
            url=f"{BASE_URL}/classification-heads/genre_discogs519/"
            "genre_discogs519-discogs-maest-30s-pw-519l-1.json",
        ),
    ),
)
