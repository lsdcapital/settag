import json
import logging
import sys
import wave
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from mutagen.id3 import ID3, TCON
from mutagen.wave import WAVE

from settag.cli import (
    _analyze_one,
    main,
)
from settag.plans import planned_write_record, stage_file_genre
from settag.policy import Prediction
from settag.workflow import planned_write_for_track, prepare_track


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


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


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
    assert tags["TXXX:SETTAG_GENRE"].text == ["Electronic---Deep House"]


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
    assert "  SetTag genres: none -> Electronic---Deep House score 0.720" in messages
    assert "  dry run: 6 SetTag fields would change; nothing written" in messages
    debug_record = json.loads(caplog.records[-1].getMessage())
    assert len(debug_record["predictions"]) == 2
    assert debug_record["selected"] == [{"label": "Electronic---Deep House", "score": 0.72}]


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

    result = main([str(path)])
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

    result = main(["run", str(path), "--no-tui"])
    stderr = capsys.readouterr().err

    assert result == 0
    assert "SetTag dry run" in stderr
    assert "Dry run only; nothing was written." in stderr
    assert WAVE(path).tags is None


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
    assert plan_text.startswith('{"schema":"settag.plan/v2","path":')
    assert "predictions" not in plan
    assert plan["file_genre"] == []
    assert plan["target_file_genre"] is None
    assert plan["selected"] == [{"label": "Electronic---Deep House", "score": 0.72}]
    assert plan["changes"]["settag"][0] == "Genre labels: 0 → 1"
    assert plan["changes"]["file_genre"] is None
    assert plan["metadata_format"] == "id3"
    assert set(plan) == {
        "schema",
        "path",
        "source",
        "file_genre",
        "target_file_genre",
        "selected",
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
    assert tags["TXXX:SETTAG_GENRE"].text == ["Electronic---Deep House"]


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
    assert "Electronic---Deep House  score 0.720" in captured.out
    assert "Metadata changes (6)" in captured.out
    assert "Genre labels: 0 → 1" in captured.out
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
    assert tags["TXXX:SETTAG_GENRE"].text == ["Electronic---Deep House"]


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
    changes = record["changes"]
    assert isinstance(changes, dict)
    record["changes"] = changes["settag"]
    plan_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert main(["preview", str(plan_path)]) == 0
    assert "Electronic---Deep House" in capsys.readouterr().out


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
    assert schemas == ["settag.plan-error/v1", "settag.plan/v2"]

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
