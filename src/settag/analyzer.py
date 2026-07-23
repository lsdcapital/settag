from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
from typing import Any

import numpy as np

from settag.catalog import DISCOGS519_MAEST, ModelSpec
from settag.model_store import installed_manifest, require_models
from settag.policy import Prediction, rank_predictions


class AnalyzerError(RuntimeError):
    pass


class EssentiaGenreAnalyzer:
    def __init__(
        self,
        model_dir: Path,
        spec: ModelSpec = DISCOGS519_MAEST,
    ) -> None:
        require_models(model_dir, spec)
        self.model_dir = model_dir
        self.spec = spec

        metadata_path = spec.path(model_dir, "classifier_metadata")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        labels = metadata.get("classes")
        if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
            raise AnalyzerError(f"Invalid classifier metadata: {metadata_path}")
        self.labels: list[str] = labels

        try:
            from essentia import Pool, log
            from essentia.standard import (
                MonoLoader,
                TensorflowPredict,
                TensorflowPredictMAEST,
            )
        except ImportError as error:
            raise AnalyzerError(
                "Essentia TensorFlow bindings are unavailable. Run `uv sync` "
                "or install the `essentia-tensorflow` dependency."
            ) from error

        # TensorflowPredictMAEST otherwise emits one internal network warning
        # per patch, which can flood stderr with hundreds of lines per track.
        log.infoActive = False
        log.warningActive = False

        self._pool_type = Pool
        self._loader_type = MonoLoader
        self._embedding_model = TensorflowPredictMAEST(
            graphFilename=str(spec.path(model_dir, "embedding")),
            output=spec.embedding_output,
        )
        self._classifier_model = TensorflowPredict(
            graphFilename=str(spec.path(model_dir, "classifier")),
            inputs=[spec.classifier_input],
            outputs=[spec.classifier_output],
        )
        self.model_manifest = installed_manifest(model_dir, spec)
        self.backend_version = _package_version("essentia-tensorflow")

    def analyze(self, path: Path) -> list[Prediction]:
        audio = self._loader_type(
            filename=str(path),
            sampleRate=self.spec.sample_rate,
            resampleQuality=4,
        )()
        embeddings = self._embedding_model(audio)

        pool = self._pool_type()
        pool.set(self.spec.classifier_input, embeddings)
        output: dict[str, Any] = self._classifier_model(pool)
        raw = np.asarray(output[self.spec.classifier_output], dtype=float)

        if raw.size % len(self.labels) != 0:
            raise AnalyzerError(
                f"Classifier returned {raw.size} values for {len(self.labels)} labels"
            )

        activations = raw.reshape(-1, len(self.labels)).mean(axis=0)
        return rank_predictions(self.labels, activations.tolist())


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
