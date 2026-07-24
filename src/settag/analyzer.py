from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np

from settag.catalog import DISCOGS519_MAEST, MODEL_SPECS_BY_TASK, ModelSpec
from settag.model_store import (
    installed_manifest,
    installed_task_manifests,
    require_models,
    require_task_models,
)
from settag.policy import Prediction, rank_predictions
from settag.tasks import AnalysisTask, ordered_tasks


class AnalyzerError(RuntimeError):
    pass


_TENSORFLOW_STARTUP_NOISE = (
    re.compile(
        rb"WARNING: All log messages before absl::InitializeLog\(\) is called "
        rb"are written to STDERR\r?\n"
    ),
    re.compile(
        rb"I\d{4} [^\r\n]*mlir_graph_optimization_pass\.cc:\d+\] "
        rb"MLIR V1 optimization pass is not enabled"
        rb"(?: in compiling SavedModel\.)?\r?\n"
    ),
    re.compile(
        rb"\d{4}-\d{2}-\d{2} [^\r\n]*profile_utils/cpu_utils\.cc:\d+\] "
        rb"Failed to get CPU frequency: 0 Hz\r?\n"
    ),
)


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
        self._tensorflow_startup_pending = True

    def analyze(self, path: Path) -> list[Prediction]:
        if self._tensorflow_startup_pending:
            with _filter_tensorflow_startup_stderr():
                predictions = self._analyze(path)
            self._tensorflow_startup_pending = False
            return predictions
        return self._analyze(path)

    def _analyze(self, path: Path) -> list[Prediction]:
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


class EssentiaEffnetAnalyzer:
    def __init__(
        self,
        model_dir: Path,
        tasks: Sequence[AnalysisTask],
    ) -> None:
        selected = tuple(task for task in ordered_tasks(tasks) if task != "genre")
        if not selected:
            raise ValueError("EffNet analyzer requires mood-theme or instrument")
        require_task_models(model_dir, selected)
        self.model_dir = model_dir
        self.tasks = selected
        self.model_manifests = installed_task_manifests(model_dir, selected)
        self.backend_version = _package_version("essentia-tensorflow")

        try:
            from essentia import log
            from essentia.standard import (
                MonoLoader,
                TensorflowPredict2D,
                TensorflowPredictEffnetDiscogs,
            )
        except ImportError as error:
            raise AnalyzerError(
                "Essentia TensorFlow bindings are unavailable. Run `uv sync` "
                "or install the `essentia-tensorflow` dependency."
            ) from error

        log.infoActive = False
        log.warningActive = False
        self._loader_type = MonoLoader
        embedding_spec = MODEL_SPECS_BY_TASK[selected[0]]
        self._sample_rate = embedding_spec.sample_rate
        self._embedding_model = TensorflowPredictEffnetDiscogs(
            graphFilename=str(embedding_spec.path(model_dir, "embedding")),
            output=embedding_spec.embedding_output,
        )
        self._heads: dict[AnalysisTask, tuple[Any, list[str], str]] = {}
        for task in selected:
            spec = MODEL_SPECS_BY_TASK[task]
            metadata_path = spec.path(model_dir, "classifier_metadata")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            labels = metadata.get("classes")
            if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
                raise AnalyzerError(f"Invalid classifier metadata: {metadata_path}")
            outputs = metadata.get("schema", {}).get("outputs", [])
            output_name = next(
                (
                    item.get("name")
                    for item in outputs
                    if isinstance(item, dict) and item.get("output_purpose") == "predictions"
                ),
                spec.classifier_output,
            )
            if not isinstance(output_name, str):
                raise AnalyzerError(f"Invalid classifier output metadata: {metadata_path}")
            self._heads[task] = (
                TensorflowPredict2D(
                    graphFilename=str(spec.path(model_dir, "classifier")),
                    output=output_name,
                ),
                labels,
                output_name,
            )
        self._tensorflow_startup_pending = True

    def analyze_tasks(self, path: Path) -> dict[AnalysisTask, list[Prediction]]:
        if self._tensorflow_startup_pending:
            with _filter_tensorflow_startup_stderr():
                predictions = self._analyze_tasks(path)
            self._tensorflow_startup_pending = False
            return predictions
        return self._analyze_tasks(path)

    def _analyze_tasks(self, path: Path) -> dict[AnalysisTask, list[Prediction]]:
        audio = self._loader_type(
            filename=str(path),
            sampleRate=self._sample_rate,
            resampleQuality=4,
        )()
        embeddings = self._embedding_model(audio)
        results: dict[AnalysisTask, list[Prediction]] = {}
        for task, (model, labels, _) in self._heads.items():
            raw = np.asarray(model(embeddings), dtype=float)
            if raw.size % len(labels) != 0:
                raise AnalyzerError(
                    f"{task} classifier returned {raw.size} values for {len(labels)} labels"
                )
            activations = raw.reshape(-1, len(labels)).mean(axis=0)
            results[task] = rank_predictions(labels, activations.tolist())
        return results


class EssentiaTaskAnalyzer:
    """Load only the explicitly selected task families and expose one task result map."""

    def __init__(self, model_dir: Path, tasks: Sequence[AnalysisTask]) -> None:
        self.tasks = ordered_tasks(tasks)
        if not self.tasks:
            raise ValueError("at least one analysis task is required")
        self._genre = EssentiaGenreAnalyzer(model_dir) if "genre" in self.tasks else None
        effnet_tasks = tuple(task for task in self.tasks if task != "genre")
        self._effnet = (
            EssentiaEffnetAnalyzer(model_dir, effnet_tasks)
            if effnet_tasks
            else None
        )
        manifests: dict[AnalysisTask, dict[str, object]] = {}
        if self._genre is not None:
            manifests["genre"] = self._genre.model_manifest
        if self._effnet is not None:
            manifests.update(self._effnet.model_manifests)
        self.model_manifests = manifests
        self.backend_version = (
            self._genre.backend_version
            if self._genre is not None
            else self._effnet.backend_version
            if self._effnet is not None
            else "unknown"
        )

    def analyze_tasks(self, path: Path) -> dict[AnalysisTask, list[Prediction]]:
        results: dict[AnalysisTask, list[Prediction]] = {}
        if self._genre is not None:
            results["genre"] = self._genre.analyze(path)
        if self._effnet is not None:
            results.update(self._effnet.analyze_tasks(path))
        return results


@contextmanager
def _filter_tensorflow_startup_stderr() -> Iterator[None]:
    """Remove known harmless TensorFlow startup lines from native stderr."""
    sys.stderr.flush()
    saved_stderr = os.dup(2)
    try:
        with tempfile.TemporaryFile() as captured:
            os.dup2(captured.fileno(), 2)
            try:
                yield
            finally:
                sys.stderr.flush()
                os.dup2(saved_stderr, 2)
                captured.seek(0)
                output = captured.read()
                for pattern in _TENSORFLOW_STARTUP_NOISE:
                    output = pattern.sub(b"", output)
                while output:
                    written = os.write(saved_stderr, output)
                    output = output[written:]
    finally:
        os.close(saved_stderr)


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
