import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
import pytest

from settag.analyzer import EssentiaTaskAnalyzer, _filter_tensorflow_startup_stderr
from settag.catalog import (
    DISCOGS519_MAEST,
    DISCOGS_EFFNET_INSTRUMENT,
    DISCOGS_EFFNET_MOOD_THEME,
)


def test_tensorflow_startup_noise_is_filtered_but_other_stderr_survives(capfd) -> None:
    noisy_output = (
        b"progress"
        b"WARNING: All log messages before absl::InitializeLog() is called "
        b"are written to STDERR\n"
        b"I0000 00:00:1784864848.640802 22343321 "
        b"mlir_graph_optimization_pass.cc:425] "
        b"MLIR V1 optimization pass is not enabled\n"
        b"2026-07-24 07:28:10.984456: W "
        b"external/local_xla/xla/tsl/platform/profile_utils/cpu_utils.cc:145] "
        b"Failed to get CPU frequency: 0 Hz\n"
        b"W0000 00:00:1784910231.731010 25260253 op_level_cost_estimator.cc:743] "
        b'Invalid device specifications for CPU: type: "CPU" model: "0" '
        b"num_cores: 12\n"
        b"real native diagnostic\n"
    )

    with _filter_tensorflow_startup_stderr():
        os.write(2, noisy_output)

    assert capfd.readouterr().err == "progressreal native diagnostic\n"


def test_native_stderr_is_replayed_when_analysis_fails(capfd) -> None:
    def fail_after_writing_native_stderr() -> None:
        os.write(2, b"real native failure detail\n")
        raise RuntimeError("analysis failed")

    with (
        pytest.raises(RuntimeError, match="analysis failed"),
        _filter_tensorflow_startup_stderr(),
    ):
        fail_after_writing_native_stderr()

    assert capfd.readouterr().err == "real native failure detail\n"


def test_task_analyzer_decodes_once_and_shares_effnet_embedding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    metadata = (
        (DISCOGS519_MAEST, "classifier_metadata", ["House", "Techno"]),
        (
            DISCOGS_EFFNET_MOOD_THEME,
            "classifier_metadata",
            ["energetic", "dark"],
        ),
        (
            DISCOGS_EFFNET_INSTRUMENT,
            "classifier_metadata",
            ["drummachine", "synthesizer"],
        ),
    )
    for spec, role, classes in metadata:
        spec.path(tmp_path, role).write_text(
            json.dumps({"classes": classes}),
            encoding="utf-8",
        )
    calls = {"loader": 0, "maest": 0, "effnet": 0, "mood": 0, "instrument": 0}

    class Pool:
        def __init__(self) -> None:
            self.values: dict[str, Any] = {}

        def set(self, key: str, value: Any) -> None:
            self.values[key] = value

    class MonoLoader:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __call__(self) -> np.ndarray:
            calls["loader"] += 1
            return np.asarray([0.0, 0.1])

    class TensorflowPredictMAEST:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __call__(self, _audio: np.ndarray) -> np.ndarray:
            calls["maest"] += 1
            return np.asarray([[1.0], [2.0]])

    class TensorflowPredict:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __call__(self, _pool: Pool) -> dict[str, np.ndarray]:
            return {DISCOGS519_MAEST.classifier_output: np.asarray([[0.8, 0.2], [0.6, 0.4]])}

    class TensorflowPredictEffnetDiscogs:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __call__(self, _audio: np.ndarray) -> np.ndarray:
            calls["effnet"] += 1
            return np.asarray([[10.0], [20.0]])

    class TensorflowPredict2D:
        def __init__(self, *, graphFilename: str, **_kwargs: Any) -> None:
            self.task = "mood" if "moodtheme" in graphFilename else "instrument"

        def __call__(self, embeddings: np.ndarray) -> np.ndarray:
            assert embeddings.tolist() == [[10.0], [20.0]]
            calls[self.task] += 1
            if self.task == "mood":
                return np.asarray([[0.7, 0.1], [0.9, 0.3]])
            return np.asarray([[0.4, 0.8], [0.6, 1.0]])

    essentia = ModuleType("essentia")
    vars(essentia).update(
        {
            "Pool": Pool,
            "log": SimpleNamespace(infoActive=True, warningActive=True),
        }
    )
    standard = ModuleType("essentia.standard")
    vars(standard).update(
        {
            "MonoLoader": MonoLoader,
            "TensorflowPredict": TensorflowPredict,
            "TensorflowPredict2D": TensorflowPredict2D,
            "TensorflowPredictEffnetDiscogs": TensorflowPredictEffnetDiscogs,
            "TensorflowPredictMAEST": TensorflowPredictMAEST,
        }
    )
    monkeypatch.setitem(sys.modules, "essentia", essentia)
    monkeypatch.setitem(sys.modules, "essentia.standard", standard)
    monkeypatch.setattr("settag.analyzer.require_models", lambda *_args: None)
    monkeypatch.setattr("settag.analyzer.require_task_models", lambda *_args: None)
    monkeypatch.setattr(
        "settag.analyzer.installed_manifest",
        lambda _directory, spec: {"id": spec.id},
    )
    monkeypatch.setattr(
        "settag.analyzer.installed_task_manifests",
        lambda _directory, tasks: {task: {"id": f"{task}/v1"} for task in tasks},
    )

    analyzer = EssentiaTaskAnalyzer(
        tmp_path,
        ("genre", "mood-theme", "instrument"),
    )
    result = analyzer.analyze_tasks(tmp_path / "track.wav")

    assert calls == {
        "loader": 1,
        "maest": 1,
        "effnet": 1,
        "mood": 1,
        "instrument": 1,
    }
    assert [item.label for item in result["genre"]] == ["House", "Techno"]
    np.testing.assert_allclose(
        [item.score for item in result["genre"]],
        [0.7, 0.3],
    )
    assert [item.label for item in result["mood-theme"]] == ["Energetic", "Dark"]
    assert [item.label for item in result["instrument"]] == [
        "Synthesizer",
        "Drum Machine",
    ]
