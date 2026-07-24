import os

import pytest

from settag.analyzer import _filter_tensorflow_startup_stderr


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
        b"real native diagnostic\n"
    )

    with _filter_tensorflow_startup_stderr():
        os.write(2, noisy_output)

    assert capfd.readouterr().err == "progressreal native diagnostic\n"


def test_native_stderr_is_replayed_when_analysis_fails(capfd) -> None:
    with (
        pytest.raises(RuntimeError, match="analysis failed"),
        _filter_tensorflow_startup_stderr(),
    ):
        os.write(2, b"real native failure detail\n")
        raise RuntimeError("analysis failed")

    assert capfd.readouterr().err == "real native failure detail\n"
