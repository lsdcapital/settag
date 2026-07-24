import json
import logging
import sys
import wave
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.id3 import ID3, TCON
from mutagen.wave import WAVE

from settag.catalog import DISCOGS519_MAEST
from settag.cli import (
    _analyze_one,
    main,
)
from settag.plans import (
    PlanError,
    planned_write_from_record,
    planned_write_record,
    stage_default_file_genre,
    stage_file_genre,
    standard_genre_from_model_label,
)
from settag.policy import Prediction
from settag.state import WorkbenchStore
from settag.tags import apply_owned_tags, task_evidence_from_owned
from settag.workflow import (
    AnalysisBatch,
    analyze_paths,
    planned_write_for_track,
    prepare_track,
)


class FakeAnalyzer:
    spec = SimpleNamespace(id="model/v1")
    model_manifest = {"id": "model/v1", "files": {}}
    backend_version = "test"

    def analyze(self, path: Path) -> list[Prediction]:
        return [
            Prediction("Electronic---Deep House", 0.72),
            Prediction("Electronic---House", 0.05),
        ]


class PartiallyFailingAnalyzer(FakeAnalyzer):
    def analyze(self, path: Path) -> list[Prediction]:
        if path.name == "bad.wav":
            raise RuntimeError("analysis failed")
        return super().analyze(path)


class PinnedFakeAnalyzer(FakeAnalyzer):
    spec = SimpleNamespace(id=DISCOGS519_MAEST.id)


class FakeInstrumentAnalyzer:
    backend_version = "test"
    model_manifests = {
        "instrument": {
            "schema": "settag.models/v1",
            "id": "essentia/instrument-effnet/v1",
            "files": {
                "embedding": {"name": "effnet.pb", "sha256": "a" * 64},
                "classifier": {"name": "instrument.pb", "sha256": "b" * 64},
            },
        },
    }

    def analyze_tasks(self, path: Path) -> dict[str, list[Prediction]]:
        return {
            "instrument": [
                Prediction("synthesizer", 0.81),
                Prediction("drummachine", 0.62),
            ]
        }


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    "label",
    [
        "Electronic---Deep House",
        "Electronic---Progressive House",
        "Electronic---Tech House",
        "Electronic---Tropical House",
    ],
)
def test_house_model_labels_roll_up_to_standard_house(label: str) -> None:
    assert standard_genre_from_model_label(label) == "House"


def test_standard_genre_rollup_does_not_guess_from_house_suffix() -> None:
    assert standard_genre_from_model_label("Electronic---Witch House") == "Witch House"
    assert standard_genre_from_model_label("Electronic---Techno") == "Techno"


def _silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 80)


def _add_genre(path: Path, genre: str) -> None:
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TCON(encoding=3, text=[genre]))
    audio.save()


def test_analyze_dry_run_emits_plan_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    output = StringIO()

    _analyze_one(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
        write=False,
        output=output,
    )

    record = json.loads(output.getvalue())
    audio = WAVE(path)
    assert record["tag_plan"]["format"] == "id3"
    assert record["tag_plan"]["changes"][0]["field"] == "TXXX:SETTAG_GENRE"
    assert record["write"] == {"requested": False, "status": "not_requested"}
    assert audio.tags is None


def test_analyze_write_applies_exact_planned_fields(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    output = StringIO()

    _analyze_one(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
        write=True,
        output=output,
    )

    record = json.loads(output.getvalue())
    tags = WAVE(path).tags
    assert record["write"]["requested"] is True
    assert record["write"]["status"] == "written"
    assert record["write"]["result_sha256"] != record["source"]["sha256"]
    assert tags is not None
    assert tags["TXXX:SETTAG_GENRE"].text == [
        "Electronic---Deep House",
        "Electronic---House",
    ]
    assert record["schema"] == "settag.analysis/v2"
    assert record["tasks"]["genre"]["evidence"] == [
        {"label": "Electronic---Deep House", "score": 0.72},
        {"label": "Electronic---House", "score": 0.05},
    ]
    assert record["tasks"]["genre"]["selected"] == [
        {"label": "Electronic---Deep House", "score": 0.72},
    ]


def test_score_cutoff_and_top_do_not_remove_portable_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    output = StringIO()

    _analyze_one(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=1,
        threshold=0.80,
        write=True,
        output=output,
    )

    record = json.loads(output.getvalue())
    tags = WAVE(path).tags
    assert record["tasks"]["genre"]["selected"] == []
    assert record["tasks"]["genre"]["evidence"] == [
        {"label": "Electronic---Deep House", "score": 0.72},
        {"label": "Electronic---House", "score": 0.05},
    ]
    assert tags is not None
    assert tags["TXXX:SETTAG_GENRE"].text == [
        "Electronic---Deep House",
        "Electronic---House",
    ]


def test_instrument_only_run_preserves_genre_and_publishes_task_provenance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    _add_genre(path, "Existing genre")

    _analyze_one(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
        write=True,
        output=StringIO(),
    )
    genre_tags = WAVE(path).tags
    assert genre_tags is not None
    genre_before = list(genre_tags["TXXX:SETTAG_GENRE"].text)

    output = StringIO()
    _analyze_one(
        path,
        analyzer=FakeInstrumentAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
        write=True,
        output=output,
    )

    tags = WAVE(path).tags
    record = json.loads(output.getvalue())
    assert tags is not None
    assert tags["TCON"].text == ["Existing genre"]
    assert tags["TXXX:SETTAG_GENRE"].text == genre_before
    assert tags["TXXX:SETTAG_INSTRUMENT"].text == [
        "synthesizer",
        "drummachine",
    ]
    provenance = json.loads(tags["TXXX:SETTAG_PROVENANCE"].text[0])
    assert provenance["schema"] == "settag.provenance/v2"
    assert set(provenance["tasks"]) == {"genre", "instrument"}
    assert provenance["tasks"]["instrument"]["model"]["files"]["embedding"]["sha256"] == (
        "a" * 64
    )
    assert set(record["tasks"]) == {"instrument"}
    assert "genre" not in record["tasks"]


def test_explicit_instrument_task_does_not_construct_genre_analyzer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "track.wav"
    output = tmp_path / "analysis.jsonl"
    _silent_wav(path)

    def unexpected_genre(_model_dir):
        raise AssertionError("instrument-only analysis must not load MAEST")

    monkeypatch.setattr("settag.cli.EssentiaGenreAnalyzer", unexpected_genre)
    monkeypatch.setattr(
        "settag.cli.EssentiaTaskAnalyzer",
        lambda _model_dir, tasks: FakeInstrumentAnalyzer(),
    )

    result = main(
        [
            "analyze",
            str(path),
            "--tasks",
            "instrument",
            "--output",
            str(output),
        ]
    )

    record = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert set(record["tasks"]) == {"instrument"}
    assert WAVE(path).tags is None


def test_default_file_genre_never_replaces_an_existing_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    _add_genre(path, "Existing genre")
    track = prepare_track(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
    )

    planned = stage_default_file_genre(planned_write_for_track(track))

    assert planned.file_genre == ("Existing genre",)
    assert planned.target_file_genre is None


def test_analyze_without_output_logs_summary_and_complete_debug_record(
    tmp_path: Path,
    caplog,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    _add_genre(path, "Existing genre")

    with caplog.at_level(logging.DEBUG, logger="settag"):
        _analyze_one(
            path,
            analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
            top=5,
            threshold=0.10,
            write=False,
            output=None,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert "  file genre tag: Existing genre (unchanged)" in messages
    assert (
        "  SetTag genres: none -> Electronic---Deep House score 0.720, "
        "Electronic---House score 0.050"
    ) in messages
    assert (
        "  dry run: SetTag analysis bundle would change "
        "(7 internal fields); nothing written"
    ) in messages
    debug_record = json.loads(caplog.records[-1].getMessage())
    genre = debug_record["tasks"]["genre"]
    assert len(genre["predictions"]) == 2
    assert genre["evidence"] == genre["predictions"]
    assert genre["selected"] == [{"label": "Electronic---Deep House", "score": 0.72}]


def test_unhandled_ctrl_c_exits_without_a_traceback(monkeypatch, capsys) -> None:
    def interrupt(_args) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("settag.cli._run_inspect", interrupt)

    result = main(["inspect", "unused"])

    assert result == 130
    assert capsys.readouterr().err.endswith("settag: interrupted\n")


def test_inspect_reads_existing_tags_without_running_the_model(tmp_path: Path, capsys) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    _add_genre(path, "Existing genre")
    _analyze_one(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
        write=True,
        output=StringIO(),
    )

    result = main(["inspect", str(path)])
    stderr = capsys.readouterr().err

    assert result == 0
    assert "file genre tag: Existing genre" in stderr
    assert "Electronic---Deep House score 0.720" in stderr
    assert "SetTag model: model/v1" in stderr


def test_path_shorthand_runs_a_noninteractive_dry_run_without_writing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    monkeypatch.setattr(sys, "stdin", StringIO(""))
    monkeypatch.setattr("settag.cli.EssentiaGenreAnalyzer", lambda _model_dir: FakeAnalyzer())

    result = main([str(path), "--tasks", "genre"])
    captured = capsys.readouterr()

    assert result == 0
    assert "SetTag dry run" in captured.err
    assert "Electronic---Deep House" in captured.err
    assert "Dry run only; nothing was written." in captured.err
    assert "\x1b[" not in captured.err
    assert WAVE(path).tags is None


def test_no_tui_forces_the_plain_dry_run(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    monkeypatch.setattr("settag.cli.EssentiaGenreAnalyzer", lambda _model_dir: FakeAnalyzer())

    result = main(["run", str(path), "--no-tui", "--tasks", "genre"])
    stderr = capsys.readouterr().err

    assert result == 0
    assert "SetTag dry run" in stderr
    assert "Dry run only; nothing was written." in stderr
    assert WAVE(path).tags is None


def test_interactive_default_reads_metadata_without_constructing_analyzer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    analyzer_constructed = False
    inspected_paths: list[Path] = []

    def construct_analyzer(_model_dir):
        nonlocal analyzer_constructed
        analyzer_constructed = True
        raise AssertionError("the analyzer must stay unloaded in the library view")

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            self.metadata_loader = kwargs["metadata_loader"]
            assert callable(kwargs["persist_plan"])
            assert callable(kwargs["discard_plans"])

        def run(self):
            batch = self.metadata_loader(
                lambda _completed, _total, inspected: inspected_paths.append(inspected)
            )
            assert [track.path for track in batch.tracks] == [path]
            return SimpleNamespace(status=0, message="Nothing was written.")

    monkeypatch.setattr(sys, "stdin", TtyStringIO())
    monkeypatch.setattr(sys, "stdout", TtyStringIO())
    monkeypatch.setattr("settag.cli.EssentiaGenreAnalyzer", construct_analyzer)
    monkeypatch.setattr("settag.cli.SetTagApp", FakeApp)

    result = main(
        [
            "run",
            str(path),
            "--state-db",
            str(tmp_path / "state.sqlite3"),
        ]
    )

    assert result == 0
    assert inspected_paths == [path]
    assert analyzer_constructed is False


def test_interactive_run_uses_configured_tasks_and_task_analyzer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "track.wav"
    config_path = tmp_path / "config.toml"
    _silent_wav(path)
    config_path.write_text(
        '[analysis]\ntasks = ["instrument"]\n',
        encoding="utf-8",
    )
    constructed_tasks = []
    loader_started = False
    loader_closed = False

    def construct_analyzer(_model_dir, tasks):
        constructed_tasks.append(tasks)
        return FakeInstrumentAnalyzer()

    class FakeSubprocessAnalysisLoader:
        def __init__(self, model_dir, tasks, *, top, threshold) -> None:
            self.analyzer = construct_analyzer(model_dir, tasks)
            self.top = top
            self.threshold = threshold

        def start(self) -> None:
            nonlocal loader_started
            loader_started = True

        def __call__(self, paths, on_progress, should_cancel) -> AnalysisBatch:
            return analyze_paths(
                paths,
                analyzer=self.analyzer,  # type: ignore[arg-type]
                top=self.top,
                threshold=self.threshold,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )

        def close(self) -> None:
            nonlocal loader_closed
            loader_closed = True

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            assert kwargs["analysis_tasks"] == ("instrument",)
            self.analysis_loader = kwargs["analysis_loader"]

        def run(self):
            assert loader_started is True
            batch = self.analysis_loader(
                (path,),
                lambda _completed, _total, _path: None,
                lambda: False,
            )
            assert len(batch.planned) == 1
            evidence = task_evidence_from_owned(batch.planned[0].desired)
            assert [item.label for item in evidence["instrument"]] == [
                "synthesizer",
                "drummachine",
            ]
            return SimpleNamespace(status=0, message="Nothing was written.")

    monkeypatch.setattr(sys, "stdin", TtyStringIO())
    monkeypatch.setattr(sys, "stdout", TtyStringIO())
    monkeypatch.setattr(
        "settag.cli.SubprocessAnalysisLoader",
        FakeSubprocessAnalysisLoader,
    )
    monkeypatch.setattr("settag.cli.SetTagApp", FakeApp)

    result = main(["run", str(path), "--config", str(config_path)])

    assert result == 0
    assert constructed_tasks == [("instrument",)]
    assert loader_started is True
    assert loader_closed is True


def test_run_tasks_override_does_not_read_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "track.wav"
    config_path = tmp_path / "broken.toml"
    _silent_wav(path)
    config_path.write_text("[analysis\n", encoding="utf-8")
    monkeypatch.setattr(
        "settag.cli.EssentiaGenreAnalyzer",
        lambda _model_dir: FakeAnalyzer(),
    )

    result = main(
        [
            "run",
            str(path),
            "--no-tui",
            "--tasks",
            "genre",
            "--config",
            str(config_path),
        ]
    )

    assert result == 0


def test_interactive_default_restores_ready_workbench_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "track.wav"
    state_path = tmp_path / "state.sqlite3"
    _silent_wav(path)

    plan = planned_write_for_track(
        prepare_track(
            path,
            analyzer=PinnedFakeAnalyzer(),  # type: ignore[arg-type]
            top=5,
            threshold=0.10,
        )
    )
    WorkbenchStore(state_path).save(plan)
    analyzer_constructed = False

    def construct_analyzer(_model_dir):
        nonlocal analyzer_constructed
        analyzer_constructed = True
        raise AssertionError("a cached review must not construct the analyzer")

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            self.metadata_loader = kwargs["metadata_loader"]

        def run(self):
            batch = self.metadata_loader(lambda _done, _total, _path: None)
            assert len(batch.tracks) == 1
            track = batch.tracks[0]
            assert track.cache_status == "ready"
            assert track.cached_plan is not None
            assert track.cached_plan.evidence == plan.evidence
            assert track.cached_plan.selected == ()
            return SimpleNamespace(status=0, message="Nothing was written.")

    monkeypatch.setattr(sys, "stdin", TtyStringIO())
    monkeypatch.setattr(sys, "stdout", TtyStringIO())
    monkeypatch.setattr("settag.cli.EssentiaGenreAnalyzer", construct_analyzer)
    monkeypatch.setattr("settag.cli.SetTagApp", FakeApp)

    result = main(
        [
            "run",
            str(path),
            "--state-db",
            str(state_path),
            "--score-cutoff",
            "0.80",
            "--tasks",
            "genre",
        ]
    )

    assert result == 0
    assert analyzer_constructed is False


def test_current_embedded_metadata_supersedes_workbench_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "track.wav"
    state_path = tmp_path / "state.sqlite3"
    _silent_wav(path)
    plan = planned_write_for_track(
        prepare_track(
            path,
            analyzer=PinnedFakeAnalyzer(),  # type: ignore[arg-type]
            top=5,
            threshold=0.10,
        )
    )
    store = WorkbenchStore(state_path)
    store.save(plan)
    apply_owned_tags(path, plan.desired)

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            self.metadata_loader = kwargs["metadata_loader"]

        def run(self):
            batch = self.metadata_loader(lambda _done, _total, _path: None)
            track = batch.tracks[0]
            assert track.status == "current"
            assert track.cached_plan is None
            assert track.cache_status is None
            return SimpleNamespace(status=0, message="Nothing was written.")

    monkeypatch.setattr(sys, "stdin", TtyStringIO())
    monkeypatch.setattr(sys, "stdout", TtyStringIO())
    monkeypatch.setattr("settag.cli.SetTagApp", FakeApp)

    assert (
        main(
            [
                "run",
                str(path),
                "--state-db",
                str(state_path),
                "--tasks",
                "genre",
            ]
        )
        == 0
    )
    model = plan.desired["SETTAG_MODEL"]
    config = plan.desired["SETTAG_CONFIG_SHA256"]
    assert model is not None
    assert config is not None
    assert store.load(
        [path],
        expected_model_id=model[0],
        expected_config_sha256=config[0],
    ) == {}


def test_compact_plan_is_human_readable_and_applies_after_one_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    plan_path = tmp_path / "plan.jsonl"
    _silent_wav(path)
    monkeypatch.setattr("settag.cli.EssentiaGenreAnalyzer", lambda _model_dir: FakeAnalyzer())

    analyzed = main(["analyze", str(path), "--plan", str(plan_path)])
    analyze_stderr = capsys.readouterr().err
    plan_text = plan_path.read_text(encoding="utf-8")
    plan = json.loads(plan_text)

    assert analyzed == 0
    assert "Review plan created" in analyze_stderr
    assert "Tracks analyzed:  1" in analyze_stderr
    assert f"Preview: uv run settag preview {plan_path}" in analyze_stderr
    assert f"Apply:   uv run settag apply {plan_path}" in analyze_stderr
    assert plan_text.startswith('{"schema":"settag.plan/v4","path":')
    assert "predictions" not in plan
    assert plan["file_genre"] == []
    assert plan["target_file_genre"] is None
    assert plan["evidence"] == [
        {"label": "Electronic---Deep House", "score": 0.72},
        {"label": "Electronic---House", "score": 0.05},
    ]
    assert plan["selected"] == [{"label": "Electronic---Deep House", "score": 0.72}]
    assert plan["changes"]["settag"][0] == "Genre labels: 0 → 2"
    assert plan["changes"]["file_genre"] is None
    assert plan["metadata_format"] == "id3"
    assert set(plan) == {
        "schema",
        "path",
        "source",
        "file_genre",
        "target_file_genre",
        "evidence",
        "selected",
        "tasks",
        "metadata",
        "metadata_format",
        "provenance",
        "changes",
    }
    assert WAVE(path).tags is None

    monkeypatch.setattr(sys, "stdin", TtyStringIO("y\n"))
    applied = main(["apply", str(plan_path)])
    stderr = capsys.readouterr().err
    tags = WAVE(path).tags

    assert applied == 0
    assert "Batch write plan" in stderr
    assert "Tracks reviewed:        1" in stderr
    assert "Apply this exact plan to 1 file? [y] yes  [n] no > " in stderr
    assert "Done. 1 file written and verified." in stderr
    assert tags is not None
    assert tags["TXXX:SETTAG_GENRE"].text == [
        "Electronic---Deep House",
        "Electronic---House",
    ]


def test_preview_renders_a_saved_plan_without_external_json_tools_or_writing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    plan_path = tmp_path / "plan.jsonl"
    _silent_wav(path)
    monkeypatch.setattr("settag.cli.EssentiaGenreAnalyzer", lambda _model_dir: FakeAnalyzer())

    assert main(["analyze", str(path), "--plan", str(plan_path)]) == 0
    capsys.readouterr()

    result = main(["preview", str(plan_path)])
    captured = capsys.readouterr()

    assert result == 0
    assert captured.err == ""
    assert "SetTag batch plan" in captured.out
    assert "Track 1 of 1" in captured.out
    assert path.name in captured.out
    assert "File genre tag\n  None (will not be changed)" in captured.out
    assert "Suggested candidate: Electronic---Deep House (model score 0.720)" in captured.out
    assert "SetTag model evidence" in captured.out
    assert "Electronic---Deep House  score 0.720  selected" in captured.out
    assert "Electronic---House       score 0.050  available" in captured.out
    assert "SetTag analysis bundle (7 internal field changes)" in captured.out
    assert "Genre labels: 0 → 2" in captured.out
    assert "This preview reads only the saved plan" in captured.out
    assert f"uv run settag apply {plan_path}" in captured.out
    assert WAVE(path).tags is None


def test_saved_plan_can_stage_and_apply_a_standard_genre_edit(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    plan_path = tmp_path / "plan.jsonl"
    _silent_wav(path)
    track = prepare_track(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
    )
    planned = stage_file_genre(
        planned_write_for_track(track),
        ("Deep House",),
    )
    plan_path.write_text(
        json.dumps(planned_write_record(planned)) + "\n",
        encoding="utf-8",
    )

    assert main(["preview", str(plan_path)]) == 0
    preview = capsys.readouterr().out
    assert "None → Deep House (staged)" in preview
    assert "File genre: None → Deep House" in preview

    assert main(["apply", str(plan_path), "--yes"]) == 0
    capsys.readouterr()
    tags = WAVE(path).tags
    assert tags is not None
    assert tags["TCON"].text == ["Deep House"]
    assert tags["TXXX:SETTAG_GENRE"].text == [
        "Electronic---Deep House",
        "Electronic---House",
    ]


def test_legacy_evidence_only_plan_remains_readable(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    plan_path = tmp_path / "legacy.jsonl"
    _silent_wav(path)
    track = prepare_track(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
    )
    record = planned_write_record(planned_write_for_track(track))
    record["schema"] = "settag.plan/v1"
    del record["target_file_genre"]
    del record["evidence"]
    changes = record["changes"]
    assert isinstance(changes, dict)
    changes["settag"][0] = "Genre labels: 0 → 1"
    record["changes"] = changes["settag"]
    plan_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert main(["preview", str(plan_path)]) == 0
    assert "Electronic---Deep House" in capsys.readouterr().out


def test_v2_plan_remains_readable_as_its_selected_evidence(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    plan_path = tmp_path / "v2.jsonl"
    _silent_wav(path)
    track = prepare_track(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
    )
    record = planned_write_record(planned_write_for_track(track))
    record["schema"] = "settag.plan/v2"
    del record["evidence"]
    changes = record["changes"]
    assert isinstance(changes, dict)
    changes["settag"][0] = "Genre labels: 0 → 1"
    plan_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert main(["preview", str(plan_path)]) == 0
    preview = capsys.readouterr().out
    assert "Electronic---Deep House" in preview
    assert "Electronic---House" not in preview


def test_v3_plan_rejects_unranked_evidence(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    track = prepare_track(
        path,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        top=5,
        threshold=0.10,
    )
    record = planned_write_record(planned_write_for_track(track))
    evidence = record["evidence"]
    assert isinstance(evidence, list)
    evidence.reverse()

    with pytest.raises(PlanError, match="descending score order"):
        planned_write_from_record(record)


def test_batch_apply_aborts_all_writes_when_any_source_is_stale(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    plan_path = tmp_path / "plan.jsonl"
    _silent_wav(first)
    _silent_wav(second)
    monkeypatch.setattr("settag.cli.EssentiaGenreAnalyzer", lambda _model_dir: FakeAnalyzer())

    assert main(["analyze", str(tmp_path), "--plan", str(plan_path)]) == 0
    capsys.readouterr()
    _add_genre(second, "Changed elsewhere")

    result = main(["apply", str(plan_path), "--yes"])
    stderr = capsys.readouterr().err

    assert result == 2
    assert "source SHA-256 changed" in stderr
    assert "No files were written." in stderr
    assert WAVE(first).tags is None
    second_tags = WAVE(second).tags
    assert second_tags is not None
    assert second_tags.get("TXXX:SETTAG_GENRE") is None


def test_batch_apply_rejects_a_partial_plan_with_analysis_errors(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    good = tmp_path / "good.wav"
    bad = tmp_path / "bad.wav"
    plan_path = tmp_path / "plan.jsonl"
    _silent_wav(good)
    _silent_wav(bad)
    monkeypatch.setattr(
        "settag.cli.EssentiaGenreAnalyzer",
        lambda _model_dir: PartiallyFailingAnalyzer(),
    )

    analyzed = main(["analyze", str(tmp_path), "--plan", str(plan_path)])
    capsys.readouterr()
    schemas = [
        json.loads(line)["schema"]
        for line in plan_path.read_text(encoding="utf-8").splitlines()
    ]

    assert analyzed == 1
    assert schemas == ["settag.plan-error/v1", "settag.plan/v4"]

    applied = main(["apply", str(plan_path), "--yes"])
    stderr = capsys.readouterr().err

    assert applied == 2
    assert "plan contains an analysis error" in stderr
    assert "No files were written." in stderr
    assert WAVE(good).tags is None


def test_plan_cannot_be_combined_with_immediate_writing(tmp_path: Path, capsys) -> None:
    result = main(
        [
            "analyze",
            str(tmp_path),
            "--plan",
            str(tmp_path / "plan.jsonl"),
            "--write",
        ]
    )

    assert result == 2
    assert "--plan is a dry-run artifact" in capsys.readouterr().err
