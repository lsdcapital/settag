import json
import logging
import os
import sys
import wave
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.id3 import COMM, ID3, TCON, TXXX
from mutagen.wave import WAVE

from settag import __version__
from settag.catalog import DISCOGS519_MAEST
from settag.cli import (
    _analyze_one,
    main,
)
from settag.cli.args import build_parser
from settag.journal import JournalError, WriteJournal
from settag.plans import (
    PLAN_SCHEMA,
    PlanError,
    planned_write_from_record,
    planned_write_record,
    stage_default_file_genre,
    stage_file_genre,
    standard_genre_from_model_label,
)
from settag.policy import Prediction
from settag.state import WorkbenchStore
from settag.tags import PROVENANCE_SCHEMA, apply_metadata_tags, task_evidence_from_owned
from settag.tasks import AnalysisTask
from settag.workflow import (
    AnalysisBatch,
    Analyzer,
    analyze_paths,
    apply_prepared,
    planned_write_for_track,
    preflight_plan,
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
    model_manifests: dict[AnalysisTask, dict[str, object]] = {
        "instrument": {
            "schema": "settag.models/v1",
            "id": "essentia/instrument-effnet/v1",
            "files": {
                "embedding": {"name": "effnet.pb", "sha256": "a" * 64},
                "classifier": {"name": "instrument.pb", "sha256": "b" * 64},
            },
        },
    }

    def analyze_tasks(
        self,
        path: Path,
    ) -> dict[AnalysisTask, list[Prediction]]:
        return {
            "instrument": [
                Prediction("synthesizer", 0.81),
                Prediction("drummachine", 0.62),
            ]
        }


class FakeMultiTaskAnalyzer:
    backend_version = "test"
    model_manifests: dict[AnalysisTask, dict[str, object]] = {
        "genre": {"schema": "settag.models/v1", "id": "fake/genre/v1", "files": {}},
        "mood-theme": {"schema": "settag.models/v1", "id": "fake/moodtheme/v1", "files": {}},
    }

    def analyze_tasks(
        self,
        path: Path,
    ) -> dict[AnalysisTask, list[Prediction]]:
        return {
            "genre": [Prediction("Electronic---Deep House", 0.72)],
            "mood-theme": [Prediction("Deep", 0.53), Prediction("Corporate", 0.12)],
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
def test_house_model_labels_preserve_subgenre(label: str) -> None:
    assert standard_genre_from_model_label(label) == label.rsplit("---", 1)[-1]


def test_standard_genre_preserves_other_children() -> None:
    assert standard_genre_from_model_label("Electronic---Witch House") == "Witch House"
    assert standard_genre_from_model_label("Electronic---Techno") == "Techno"


def _silent_wav(path: Path, *, seconds: float = 35.0) -> None:
    """Write a silent WAV.

    The default is long enough to clear the genre model's 30s window, so a
    fixture is a track rather than a sample. Pass a shorter value to build one.
    """
    rate = 8_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * int(rate * seconds))


def _add_genre(path: Path, genre: str) -> None:
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TCON(encoding=3, text=[genre]))
    audio.save()


def _write_analysis(
    path: Path,
    analyzer: Analyzer,
    *,
    top: int = 5,
    threshold: float = 0.10,
) -> None:
    """Write one analysis through the plan path, the only remaining route to disk."""
    track = prepare_track(path, analyzer=analyzer, top=top, threshold=threshold)
    apply_prepared(preflight_plan([planned_write_for_track(track)]))


def test_analyze_dry_run_emits_plan_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    output = StringIO()

    _analyze_one(
        path,
        analyzer=FakeAnalyzer(),
        top=5,
        threshold=0.10,
        output=output,
    )

    record = json.loads(output.getvalue())
    audio = WAVE(path)
    assert record["tag_plan"]["format"] == "id3"
    assert record["tag_plan"]["changes"][0]["field"] == "TXXX:SETTAG_GENRE"
    assert "write" not in record
    assert audio.tags is None


def test_analysis_record_reports_evidence_and_selection(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    output = StringIO()

    _analyze_one(
        path,
        analyzer=FakeAnalyzer(),
        top=5,
        threshold=0.10,
        output=output,
    )

    record = json.loads(output.getvalue())
    assert record["schema"] == "settag.analysis/v3"
    assert record["tasks"]["genre"]["evidence"] == [
        {"label": "Electronic---Deep House", "score": 0.72},
        {"label": "Electronic---House", "score": 0.05},
    ]
    assert record["tasks"]["genre"]["selected"] == [
        {"label": "Electronic---Deep House", "score": 0.72},
    ]
    assert WAVE(path).tags is None


def test_score_cutoff_and_top_do_not_remove_portable_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    output = StringIO()

    _analyze_one(
        path,
        analyzer=FakeAnalyzer(),
        top=1,
        threshold=0.80,
        output=output,
    )
    _write_analysis(path, FakeAnalyzer(), top=1, threshold=0.80)

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

    _write_analysis(path, FakeAnalyzer())
    genre_tags = WAVE(path).tags
    assert genre_tags is not None
    genre_before = list(genre_tags["TXXX:SETTAG_GENRE"].text)

    output = StringIO()
    _analyze_one(
        path,
        analyzer=FakeInstrumentAnalyzer(),
        top=5,
        threshold=0.10,
        output=output,
    )
    _write_analysis(path, FakeInstrumentAnalyzer())

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
    assert provenance["schema"] == PROVENANCE_SCHEMA
    assert set(provenance["tasks"]) == {"genre", "instrument"}
    assert provenance["tasks"]["instrument"]["model"]["files"]["embedding"]["sha256"] == ("a" * 64)
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

    monkeypatch.setattr("settag.cli.commands.EssentiaGenreAnalyzer", unexpected_genre)
    monkeypatch.setattr(
        "settag.cli.commands.EssentiaTaskAnalyzer",
        lambda _model_dir, tasks, **_options: FakeInstrumentAnalyzer(),
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
        analyzer=FakeAnalyzer(),
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
            analyzer=FakeAnalyzer(),
            top=5,
            threshold=0.10,
            output=None,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert "  file genre tag: Existing genre (unchanged)" in messages
    assert (
        "  SetTag genres: none -> Electronic---Deep House score 0.720, "
        "Electronic---House score 0.050"
    ) in messages
    assert (
        "  dry run: SetTag analysis bundle would change (7 internal fields); nothing written"
    ) in messages
    debug_record = json.loads(caplog.records[-1].getMessage())
    genre = debug_record["tasks"]["genre"]
    assert len(genre["predictions"]) == 2
    assert genre["evidence"] == genre["predictions"]
    assert genre["selected"] == [{"label": "Electronic---Deep House", "score": 0.72}]


def test_version_flag_reports_the_version_stamped_into_tags(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"settag {__version__}"


def test_unhandled_ctrl_c_exits_without_a_traceback(monkeypatch, capsys) -> None:
    def interrupt(_args) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("settag.cli.commands._run_inspect", interrupt)

    result = main(["inspect", "unused"])

    assert result == 130
    assert capsys.readouterr().err.endswith("settag: interrupted\n")


def test_inspect_reads_existing_tags_without_running_the_model(tmp_path: Path, capsys) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    _add_genre(path, "Existing genre")
    _write_analysis(path, FakeAnalyzer())

    result = main(["inspect", str(path)])
    stderr = capsys.readouterr().err

    assert result == 0
    assert "file genre tag: Existing genre" in stderr
    assert "genre: 2 labels" in stderr
    assert "1. Electronic---Deep House  0.720" in stderr
    assert "2. Electronic---House       0.050" in stderr
    assert "model: model/v1" in stderr


def test_inspect_reports_every_analyzed_task_with_its_own_model(
    tmp_path: Path,
    capsys,
) -> None:
    """Every task SetTag wrote is reported, not just genre.

    ``inspect`` read ``SETTAG_GENRE*`` and the singular ``SETTAG_MODEL`` only,
    so a multi-task file looked single-task and named one of its two models.
    """
    path = tmp_path / "track.wav"
    _silent_wav(path)
    _add_genre(path, "Afro House")
    _write_analysis(path, FakeMultiTaskAnalyzer())

    result = main(["inspect", str(path)])
    stderr = capsys.readouterr().err

    assert result == 0
    assert "file genre tag: Afro House" in stderr
    assert "genre: 1 label" in stderr
    assert "mood-theme: 2 labels" in stderr
    assert "1. Deep       0.530" in stderr
    assert "2. Corporate  0.120" in stderr
    assert "model: fake/genre/v1" in stderr
    assert "model: fake/moodtheme/v1" in stderr


def test_inspect_reports_a_file_without_settag_metadata(tmp_path: Path, capsys) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    _add_genre(path, "Afro House")

    result = main(["inspect", str(path)])
    stderr = capsys.readouterr().err

    assert result == 0
    assert "file genre tag: Afro House" in stderr
    assert "SetTag metadata: none" in stderr


def test_inspect_falls_back_to_labels_when_scores_are_unreadable(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    _write_analysis(path, FakeAnalyzer())
    audio = WAVE(path)
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TXXX(encoding=3, desc="SETTAG_GENRE_SCORES", text=["not json"]))
    audio.save()

    result = main(["inspect", str(path)])
    stderr = capsys.readouterr().err

    assert result == 0
    assert "scores are unreadable; showing labels only" in stderr
    assert "1. Electronic---Deep House" in stderr


def test_hygiene_plain_mode_reports_suggestions_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=["electronicfresh.com"]))
    audio.save()

    result = main(["hygiene", str(path), "--no-tui"])
    stderr = capsys.readouterr().err

    assert result == 0
    assert "SetTag hygiene scan" in stderr
    assert "Tracks with findings: 1" in stderr
    assert "Cleanup suggestions:  1" in stderr
    assert "Comment: electronicfresh.com" in stderr
    assert "Reason: contains a web address" in stderr
    tags = WAVE(path).tags
    assert tags is not None
    assert tags.get("COMM::eng") is not None


def test_hygiene_tty_opens_its_own_model_free_review_app(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=["electronicfresh.com"]))
    audio.save()

    class FakeHygieneApp:
        def __init__(self, **kwargs) -> None:
            assert kwargs["source"] == path
            assert kwargs["paths"] == [path.resolve()]
            assert kwargs["batch"] is None

        def run(self):
            return SimpleNamespace(status=0, message="Hygiene review closed.")

    monkeypatch.setattr(sys, "stdin", TtyStringIO())
    monkeypatch.setattr(sys, "stdout", TtyStringIO())
    monkeypatch.setattr("settag.cli.commands.HygieneApp", FakeHygieneApp)

    assert main(["hygiene", str(path)]) == 0


def test_path_shorthand_runs_a_noninteractive_dry_run_without_writing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    monkeypatch.setattr(sys, "stdin", StringIO(""))
    monkeypatch.setattr(
        "settag.cli.commands.EssentiaGenreAnalyzer",
        lambda _model_dir, **_options: FakeAnalyzer(),
    )

    result = main([str(path), "--tasks", "genre"])
    captured = capsys.readouterr()

    assert result == 0
    assert "SetTag dry run" in captured.err
    assert "Deep House" in captured.err
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
    monkeypatch.setattr(
        "settag.cli.commands.EssentiaGenreAnalyzer",
        lambda _model_dir, **_options: FakeAnalyzer(),
    )

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

    def construct_analyzer(_model_dir, **_options):
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
    monkeypatch.setattr("settag.cli.commands.EssentiaGenreAnalyzer", construct_analyzer)
    monkeypatch.setattr("settag.cli.commands.SetTagApp", FakeApp)

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

    def construct_analyzer(_model_dir, tasks, **_options):
        constructed_tasks.append(tasks)
        return FakeInstrumentAnalyzer()

    class FakeSubprocessAnalysisLoader:
        def __init__(self, model_dir, tasks, *, top, threshold, sample) -> None:
            self.analyzer = construct_analyzer(model_dir, tasks)
            self.sample = sample
            self.top = top
            self.threshold = threshold

        def start(self) -> None:
            nonlocal loader_started
            loader_started = True

        def __call__(self, paths, on_progress, should_cancel) -> AnalysisBatch:
            return analyze_paths(
                paths,
                analyzer=self.analyzer,
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
        "settag.cli.commands.SubprocessAnalysisLoader",
        FakeSubprocessAnalysisLoader,
    )
    monkeypatch.setattr("settag.cli.commands.SetTagApp", FakeApp)

    result = main(["run", str(path), "--config", str(config_path)])

    assert result == 0
    assert constructed_tasks == [("instrument",)]
    assert loader_started is True
    assert loader_closed is True


def test_run_reads_no_config_when_every_option_is_given(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The config file is consulted only for what the flags left unset.

    Supplying every option it could answer means a config this build cannot
    parse never has to be read, so it cannot block the run.
    """
    path = tmp_path / "track.wav"
    config_path = tmp_path / "broken.toml"
    _silent_wav(path)
    config_path.write_text("[analysis\n", encoding="utf-8")
    monkeypatch.setattr(
        "settag.cli.commands.EssentiaGenreAnalyzer",
        lambda _model_dir, **_options: FakeAnalyzer(),
    )

    result = main(
        [
            "run",
            str(path),
            "--no-tui",
            "--tasks",
            "genre",
            "--genre-sample",
            "full",
            "--config",
            str(config_path),
        ]
    )

    assert result == 0


def test_run_still_reads_config_when_one_option_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "track.wav"
    config_path = tmp_path / "broken.toml"
    _silent_wav(path)
    config_path.write_text("[analysis\n", encoding="utf-8")
    monkeypatch.setattr(
        "settag.cli.commands.EssentiaGenreAnalyzer",
        lambda _model_dir, **_options: FakeAnalyzer(),
    )

    result = main(["run", str(path), "--no-tui", "--tasks", "genre", "--config", str(config_path)])

    assert result == 2


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
            analyzer=PinnedFakeAnalyzer(),
            top=5,
            threshold=0.10,
        )
    )
    WorkbenchStore(state_path).save(plan)
    analyzer_constructed = False

    def construct_analyzer(_model_dir, **_options):
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
    monkeypatch.setattr("settag.cli.commands.EssentiaGenreAnalyzer", construct_analyzer)
    monkeypatch.setattr("settag.cli.commands.SetTagApp", FakeApp)

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
            "--genre-sample",
            "full",
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
            analyzer=PinnedFakeAnalyzer(),
            top=5,
            threshold=0.10,
        )
    )
    store = WorkbenchStore(state_path)
    store.save(plan)
    apply_metadata_tags(path, plan.desired)

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
    monkeypatch.setattr("settag.cli.commands.SetTagApp", FakeApp)

    assert (
        main(
            [
                "run",
                str(path),
                "--state-db",
                str(state_path),
                "--tasks",
                "genre",
                "--genre-sample",
                "full",
            ]
        )
        == 0
    )
    model = plan.desired["SETTAG_MODEL"]
    config = plan.desired["SETTAG_CONFIG_SHA256"]
    assert model is not None
    assert config is not None
    assert (
        store.load(
            [path],
            expected_model_ids={"genre": model[0]},
            expected_config_sha256=config[0],
        )
        == {}
    )


def test_compact_plan_is_human_readable_and_applies_after_one_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "track.wav"
    plan_path = tmp_path / "plan.jsonl"
    _silent_wav(path)
    monkeypatch.setattr(
        "settag.cli.commands.EssentiaGenreAnalyzer",
        lambda _model_dir, **_options: FakeAnalyzer(),
    )

    analyzed = main(["analyze", str(path), "--plan", str(plan_path)])
    analyze_stderr = capsys.readouterr().err
    plan_text = plan_path.read_text(encoding="utf-8")
    plan = json.loads(plan_text)

    assert analyzed == 0
    assert "Review plan created" in analyze_stderr
    assert "Tracks analyzed:  1" in analyze_stderr
    assert f"Preview: uv run settag preview {plan_path}" in analyze_stderr
    assert f"Apply:   uv run settag apply {plan_path}" in analyze_stderr
    assert plan_text.startswith(f'{{"schema":"{PLAN_SCHEMA}","path":')
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
        "genre_edit_source",
        "evidence",
        "selected",
        "tasks",
        "metadata",
        "metadata_format",
        "provenance",
        "changes",
        "notices",
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
    monkeypatch.setattr(
        "settag.cli.commands.EssentiaGenreAnalyzer",
        lambda _model_dir, **_options: FakeAnalyzer(),
    )

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
    assert "Enrichment evidence (7 internal field changes)" in captured.out
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
        analyzer=FakeAnalyzer(),
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


@pytest.mark.parametrize(
    "schema",
    ["settag.plan/v1", "settag.plan/v2", "settag.plan/v3"],
)
def test_superseded_plan_schemas_are_rejected(
    tmp_path: Path,
    capsys,
    schema: str,
) -> None:
    path = tmp_path / "track.wav"
    plan_path = tmp_path / "legacy.jsonl"
    _silent_wav(path)
    track = prepare_track(
        path,
        analyzer=FakeAnalyzer(),
        top=5,
        threshold=0.10,
    )
    record = planned_write_record(planned_write_for_track(track))
    record["schema"] = schema
    plan_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert main(["preview", str(plan_path)]) == 2
    stderr = capsys.readouterr().err
    assert f"unsupported schema {schema!r}" in stderr
    assert f"expected {PLAN_SCHEMA!r}" in stderr


def test_plan_rejects_unranked_evidence(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    track = prepare_track(
        path,
        analyzer=FakeAnalyzer(),
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
    monkeypatch.setattr(
        "settag.cli.commands.EssentiaGenreAnalyzer",
        lambda _model_dir, **_options: FakeAnalyzer(),
    )

    assert main(["analyze", str(tmp_path), "--plan", str(plan_path)]) == 0
    capsys.readouterr()
    _add_genre(second, "Changed elsewhere")

    result = main(["apply", str(plan_path), "--yes"])
    stderr = capsys.readouterr().err

    assert result == 2
    # An outside tag edit is caught by the genre comparison rather than by a
    # digest, which is what lets the message name what actually drifted.
    assert "file genre tag changed" in stderr
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
        "settag.cli.commands.EssentiaGenreAnalyzer",
        lambda _model_dir, **_options: PartiallyFailingAnalyzer(),
    )

    analyzed = main(["analyze", str(tmp_path), "--plan", str(plan_path)])
    capsys.readouterr()
    schemas = [
        json.loads(line)["schema"] for line in plan_path.read_text(encoding="utf-8").splitlines()
    ]

    assert analyzed == 1
    assert schemas == ["settag.plan-error/v1", PLAN_SCHEMA]

    applied = main(["apply", str(plan_path), "--yes"])
    stderr = capsys.readouterr().err

    assert applied == 2
    assert "plan contains an analysis error" in stderr
    assert "No files were written." in stderr
    assert WAVE(good).tags is None


def test_analyze_rejects_an_unknown_write_flag(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["analyze", str(tmp_path), "--write"])

    assert caught.value.code == 2
    assert "unrecognized arguments: --write" in capsys.readouterr().err


def _retag_outside_settag(path: Path, genre: str) -> None:
    """Change the file the way another tagger would, after SetTag wrote it."""
    audio = WAVE(path)
    tags = audio.tags
    assert tags is not None
    tags.delall("TCON")
    tags.add(TCON(encoding=3, text=[genre]))
    audio.save()


def _apply_one_track(tmp_path: Path, *, genre: tuple[str, ...] = ("Deep House",)) -> Path:
    """Write one track through the CLI so the journal has a batch to undo."""
    path = tmp_path / "track.wav"
    plan_path = tmp_path / "plan.jsonl"
    _silent_wav(path)
    planned = stage_file_genre(
        planned_write_for_track(
            prepare_track(path, analyzer=FakeAnalyzer(), top=5, threshold=0.10)
        ),
        genre,
    )
    plan_path.write_text(
        json.dumps(planned_write_record(planned)) + "\n",
        encoding="utf-8",
    )
    assert main(["apply", str(plan_path), "--yes"]) == 0
    return path


def test_undo_list_is_empty_before_anything_is_written(capsys) -> None:
    assert main(["undo", "--list"]) == 0

    assert "No SetTag writes have been journaled yet." in capsys.readouterr().err


def test_undo_reports_nothing_to_undo_before_anything_is_written(capsys) -> None:
    assert main(["undo", "--yes"]) == 0

    assert "There is nothing to undo" in capsys.readouterr().err


def test_a_cli_write_is_listed_and_can_be_undone(tmp_path: Path, capsys) -> None:
    path = _apply_one_track(tmp_path)
    capsys.readouterr()

    assert main(["undo", "--list"]) == 0
    listing = capsys.readouterr().err
    assert "1 track, 1 file genre edit" in listing

    assert main(["undo", "--yes"]) == 0
    capsys.readouterr()

    tags = WAVE(path).tags
    assert tags is None or "TCON" not in tags
    assert tags is None or "TXXX:SETTAG_GENRE" not in tags


def test_undo_dry_run_changes_nothing(tmp_path: Path, capsys) -> None:
    path = _apply_one_track(tmp_path)
    capsys.readouterr()

    assert main(["undo", "--dry-run"]) == 0
    output = capsys.readouterr().err

    assert "Dry run; no files were changed." in output
    tags = WAVE(path).tags
    assert tags is not None
    assert tags["TCON"].text == ["Deep House"]


def test_undo_names_an_unknown_batch(tmp_path: Path, capsys) -> None:
    _apply_one_track(tmp_path)
    capsys.readouterr()

    assert main(["undo", "no-such-batch"]) == 1

    assert "No write batch named no-such-batch" in capsys.readouterr().err


def test_undo_refuses_a_file_that_changed_and_force_overrides(tmp_path: Path, capsys) -> None:
    path = _apply_one_track(tmp_path)
    capsys.readouterr()
    _retag_outside_settag(path, "Techno")

    assert main(["undo", "--yes"]) == 1
    blocked = capsys.readouterr().err
    assert "file changed after SetTag wrote it" in blocked
    assert "Restore anyway with --force." in blocked

    assert main(["undo", "--yes", "--force"]) == 0
    capsys.readouterr()
    tags = WAVE(path).tags
    assert tags is None or "TCON" not in tags


def test_undo_requires_a_terminal_or_yes(tmp_path: Path, capsys, monkeypatch) -> None:
    _apply_one_track(tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", StringIO())

    assert main(["undo"]) == 2

    assert "requires an interactive terminal or --yes" in capsys.readouterr().err


def test_a_cli_write_points_at_the_command_that_reverts_it(tmp_path: Path, capsys) -> None:
    _apply_one_track(tmp_path)

    assert "Revert with: settag undo " in capsys.readouterr().err


def test_run_sample_flag_overrides_the_configured_sample(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "track.wav"
    config_path = tmp_path / "config.toml"
    _silent_wav(path)
    config_path.write_text('[analysis]\ngenre_sample = "spaced"\n', encoding="utf-8")
    constructed: list[str] = []

    def construct_analyzer(_model_dir, *, sample):
        constructed.append(sample)
        return FakeAnalyzer()

    monkeypatch.setattr("settag.cli.commands.EssentiaGenreAnalyzer", construct_analyzer)

    assert (
        main(
            [
                "run",
                str(path),
                "--no-tui",
                "--tasks",
                "genre",
                "--config",
                str(config_path),
                "--genre-sample",
                "full",
            ]
        )
        == 0
    )

    assert constructed == ["full"]


def test_run_falls_back_to_the_configured_sample(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "track.wav"
    config_path = tmp_path / "config.toml"
    _silent_wav(path)
    config_path.write_text('[analysis]\ngenre_sample = "spaced"\n', encoding="utf-8")
    constructed: list[str] = []

    def construct_analyzer(_model_dir, *, sample):
        constructed.append(sample)
        return FakeAnalyzer()

    monkeypatch.setattr("settag.cli.commands.EssentiaGenreAnalyzer", construct_analyzer)

    assert (
        main(["run", str(path), "--no-tui", "--tasks", "genre", "--config", str(config_path)]) == 0
    )

    assert constructed == ["spaced"]


def test_analyze_records_the_sample_it_ran_under(tmp_path: Path, monkeypatch) -> None:
    """The recorded config must describe the analyzer that produced the evidence."""
    path = tmp_path / "track.wav"
    output_path = tmp_path / "record.jsonl"
    _silent_wav(path)

    class SampledFakeAnalyzer(FakeAnalyzer):
        sample = "spaced"

    monkeypatch.setattr(
        "settag.cli.commands.EssentiaGenreAnalyzer",
        lambda _model_dir, **_options: SampledFakeAnalyzer(),
    )

    assert main(["analyze", str(path), "--output", str(output_path)]) == 0

    record = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["config"]["evidence"]["genre_sample"] == "spaced"


def test_sample_flag_rejects_an_unknown_strategy(tmp_path: Path, capsys) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)

    with pytest.raises(SystemExit):
        main(["analyze", str(path), "--genre-sample", "half"])

    assert "unknown audio sample 'half'" in capsys.readouterr().err


def test_inspect_no_scores_keeps_every_field_but_drops_the_ranked_lines(
    tmp_path: Path,
    capsys,
) -> None:
    """A whole taxonomy per task is unreadable across a directory."""
    path = tmp_path / "track.wav"
    _silent_wav(path)
    _add_genre(path, "Afro House")
    _write_analysis(path, FakeMultiTaskAnalyzer())

    assert main(["inspect", str(path), "--no-scores"]) == 0
    quiet = capsys.readouterr().err
    assert main(["inspect", str(path)]) == 0
    full = capsys.readouterr().err

    for expected in ("file genre tag: Afro House", "genre: 1 label", "mood-theme: 2 labels"):
        assert expected in quiet
    assert "model: fake/moodtheme/v1" in quiet
    assert "1. Deep" not in quiet
    assert "1. Deep" in full
    assert len(quiet.splitlines()) < len(full.splitlines())


def test_undo_reports_a_journal_that_cannot_be_marked_reverted(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """The files are restored before the journal is touched, so this is not a failed undo."""
    path = _apply_one_track(tmp_path)
    capsys.readouterr()

    def refuse(self, batch_id: str) -> None:
        raise JournalError("journal is read-only")

    monkeypatch.setattr(WriteJournal, "mark_reverted", refuse)

    assert main(["undo", "--yes"]) == 0
    output = capsys.readouterr().err
    assert "restored and verified" in output
    assert "The journal could not be updated: journal is read-only" in output
    tags = WAVE(path).tags
    assert tags is None or "TCON" not in tags


def test_run_reads_the_config_named_by_settag_config(tmp_path: Path, capsys) -> None:
    """The parser default follows SETTAG_CONFIG at build time, which is what isolates tests."""
    args = build_parser().parse_args(["run", str(tmp_path)])

    assert args.config == Path(os.environ["SETTAG_CONFIG"])


def test_partial_undo_leaves_the_batch_open_for_a_forced_retry(tmp_path: Path, capsys) -> None:
    """Marking a half-restored batch reverted would hide the files that still carry the write."""
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    plan_path = tmp_path / "plan.jsonl"
    lines = []
    for path in (first, second):
        _silent_wav(path)
        planned = stage_file_genre(
            planned_write_for_track(
                prepare_track(path, analyzer=FakeAnalyzer(), top=5, threshold=0.10)
            ),
            ("Deep House",),
        )
        lines.append(json.dumps(planned_write_record(planned)))
    plan_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main(["apply", str(plan_path), "--yes"]) == 0
    capsys.readouterr()
    _retag_outside_settag(second, "Techno")

    assert main(["undo", "--yes"]) == 0
    output = capsys.readouterr().err

    assert "Done. 1 file restored and verified." in output
    assert "1 file was skipped, so this write stays listed" in output
    assert "--force" in output
    batch = WriteJournal(Path(os.environ["SETTAG_JOURNAL_DB"])).latest()
    assert batch is not None
    assert batch.reverted_at is None
    first_tags = WAVE(first).tags
    assert first_tags is None or "TCON" not in first_tags
    second_tags = WAVE(second).tags
    assert second_tags is not None
    assert second_tags["TCON"].text == ["Techno"]


def test_embedding_export_is_explicit_and_does_not_write_tags(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "track.wav"
    _silent_wav(path)
    before = path.read_bytes()
    output = tmp_path / "embeddings.jsonl"

    class EmbeddingAnalyzer(FakeInstrumentAnalyzer):
        embedding = {"vector": [0.6, 0.8], "dimensions": 2}

    def create(_directory, _tasks, **options):
        assert options["export_embeddings"] is True
        return EmbeddingAnalyzer()

    monkeypatch.setattr("settag.cli.commands.EssentiaTaskAnalyzer", create)
    assert main(["analyze", str(path), "--tasks", "instrument", "--embeddings", str(output)]) == 0
    record = json.loads(output.read_text())
    assert record["schema"] == "audio-embedding/v1"
    assert record["embedding"]["vector"] == [0.6, 0.8]
    assert path.read_bytes() == before
    previous = output.read_bytes()
    assert main(["analyze", str(path), "--tasks", "instrument", "--embeddings", str(output)]) == 2
    assert output.read_bytes() == previous


def test_genre_only_embedding_export_is_rejected_before_loading(tmp_path: Path) -> None:
    assert main(["analyze", str(tmp_path), "--embeddings", str(tmp_path / "out.jsonl")]) == 2


@pytest.mark.parametrize("scan", ["metadata", "duplicates", "all"])
def test_hygiene_cli_scan_modes(tmp_path: Path, capsys, scan: str) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _silent_wav(first)
    _silent_wav(second)
    assert main(["hygiene", str(tmp_path), "--no-tui", "--scan", scan]) == 0
    output = capsys.readouterr().err
    assert ("Duplicate groups:" in output) == (scan != "metadata")
    assert ("Metadata clean:" in output) == (scan != "duplicates")
    if scan != "metadata":
        assert "Duplicate groups:     1" in output
