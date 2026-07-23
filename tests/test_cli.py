import json
import logging
import wave
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from mutagen.id3 import ID3, TCON
from mutagen.wave import WAVE

from settag.cli import _analyze_one
from settag.policy import Prediction


class FakeAnalyzer:
    spec = SimpleNamespace(id="model/v1")
    model_manifest = {"id": "model/v1", "files": {}}
    backend_version = "test"

    def analyze(self, path: Path) -> list[Prediction]:
        return [
            Prediction("Electronic---Deep House", 0.72),
            Prediction("Electronic---House", 0.05),
        ]


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
    assert "  standard genre: Existing genre (unchanged)" in messages
    assert "  SetTag genres: none -> Electronic---Deep House 72.0%" in messages
    assert "  dry run: 6 SetTag fields would change; nothing written" in messages
    debug_record = json.loads(caplog.records[-1].getMessage())
    assert len(debug_record["predictions"]) == 2
    assert debug_record["selected"] == [{"label": "Electronic---Deep House", "score": 0.72}]
