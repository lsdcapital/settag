"""Render truthful, reproducible SetTag app images for the marketing site."""

from __future__ import annotations

import asyncio
import tempfile
import wave
from collections.abc import Mapping, Sequence
from pathlib import Path

from mutagen.id3 import ID3, TCON
from mutagen.wave import WAVE

from settag.policy import Prediction
from settag.tags import OWNED_DESCRIPTIONS, GenreState
from settag.tasks import AnalysisTask
from settag.tui import SetTagApp
from settag.workflow import (
    AnalysisBatch,
    MetadataBatch,
    MetadataTrack,
    planned_write_for_track,
    prepare_track,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "site" / "public"

DEMO_GENRES = {
    "Cassia — Palm Reader.wav": "Breakbeat",
    "Marine Layer — Sundial.wav": "Afro House",
    "Night Vent — Kestrel.wav": None,
    "Slow Tide — Ferrous.wav": "Tech House",
}

DEMO_RESULTS: dict[str, Mapping[AnalysisTask, Sequence[Prediction]]] = {
    "Cassia — Palm Reader.wav": {
        "genre": (
            Prediction("Electronic---Breaks", 0.61),
            Prediction("Electronic---Electro", 0.42),
        ),
        "mood-theme": (Prediction("energetic", 0.69),),
        "instrument": (Prediction("drummachine", 0.57),),
    },
    "Marine Layer — Sundial.wav": {
        "genre": (
            Prediction("Electronic---Deep House", 0.44),
            Prediction("Electronic---Tech House", 0.43),
        ),
        "mood-theme": (Prediction("deep", 0.53),),
        "instrument": (Prediction("bass", 0.51),),
    },
    "Night Vent — Kestrel.wav": {
        "genre": (
            Prediction("Electronic---Techno", 0.72),
            Prediction("Electronic---Dub Techno", 0.58),
        ),
        "mood-theme": (Prediction("dark", 0.64),),
        "instrument": (Prediction("synthesizer", 0.62),),
    },
    "Slow Tide — Ferrous.wav": {
        "genre": (
            Prediction("Electronic---Tech House", 0.67),
            Prediction("Electronic---House", 0.39),
        ),
        "mood-theme": (Prediction("groovy", 0.59),),
        "instrument": (Prediction("percussion", 0.54),),
    },
}


class DemoAnalyzer:
    """Deterministic model adapter used only to render the real app shell."""

    model_manifests: Mapping[AnalysisTask, Mapping[str, object]] = {
        task: {
            "schema": "settag.models/v1",
            "id": f"demo/{task}/v1",
            "files": {},
        }
        for task in ("genre", "mood-theme", "instrument")
    }

    def analyze_tasks(
        self,
        path: Path,
    ) -> Mapping[AnalysisTask, Sequence[Prediction]]:
        return DEMO_RESULTS[path.name]


def _write_demo_track(path: Path, genre: str | None) -> None:
    rate = 8_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * rate * 31)

    if genre is None:
        return
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TCON(encoding=3, text=[genre]))
    audio.save()


def _metadata(path: Path, genre: str | None) -> MetadataTrack:
    return MetadataTrack(
        path=path,
        genre_state=GenreState(
            standard=(genre,) if genre is not None else (),
            settag=(),
        ),
        owned=dict.fromkeys(OWNED_DESCRIPTIONS),
        stored_predictions=(),
        status="not_analyzed",
        analyzed_at=None,
        duration_seconds=31.0,
    )


def _analyze(paths: Sequence[Path]) -> AnalysisBatch:
    analyzer = DemoAnalyzer()
    planned = tuple(
        planned_write_for_track(
            prepare_track(
                path,
                analyzer=analyzer,
                top=5,
                threshold=0.10,
            )
        )
        for path in paths
    )
    return AnalysisBatch(planned=planned, failures=())


async def _capture(
    metadata: MetadataBatch,
    *,
    size: tuple[int, int],
    filename: str,
) -> None:
    app = SetTagApp(
        source=Path("/Music/demo-crate"),
        initial_metadata=metadata,
        analysis_loader=lambda paths, _progress, _cancel: _analyze(paths),
        analysis_tasks=("genre", "mood-theme", "instrument"),
    )
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("r")
        for _ in range(120):
            await pilot.pause(0.05)
            if app.phase == "review" and not app.analysis_running:
                break
        if app.phase != "review" or app.analysis_running:
            raise RuntimeError("SetTag did not reach the review screen")
        failures = [
            entry.analysis_error.description
            for entry in app.entries
            if entry.analysis_error is not None
        ]
        if failures:
            raise RuntimeError("Demo analysis failed: " + "; ".join(failures))
        app.save_screenshot(filename=filename, path=str(OUTPUT_DIR))


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="settag-site-") as directory:
        demo_dir = Path(directory)
        tracks = []
        for filename, genre in DEMO_GENRES.items():
            path = (demo_dir / filename).resolve()
            _write_demo_track(path, genre)
            tracks.append(_metadata(path, genre))
        metadata = MetadataBatch(tracks=tuple(tracks), failures=())
        await _capture(
            metadata,
            size=(140, 22),
            filename="settag-review-wide.svg",
        )
        await _capture(
            metadata,
            size=(50, 22),
            filename="settag-review-narrow.svg",
        )


if __name__ == "__main__":
    asyncio.run(main())
