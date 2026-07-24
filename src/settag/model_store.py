from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import BinaryIO

from settag.catalog import DISCOGS519_MAEST, MODEL_SPECS_BY_TASK, ModelFile, ModelSpec
from settag.hashing import sha256_file
from settag.tasks import AnalysisTask, ordered_tasks


def default_model_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "settag" / "models"


DEFAULT_MODEL_DIR = default_model_dir()

UrlOpen = Callable[[str], BinaryIO]


class MissingModelsError(RuntimeError):
    pass


def missing_files(
    model_dir: Path,
    spec: ModelSpec = DISCOGS519_MAEST,
) -> list[ModelFile]:
    return [
        item
        for item in spec.files
        if not (path := model_dir / item.filename).is_file()
        or sha256_file(path) != item.sha256
    ]


def require_models(
    model_dir: Path,
    spec: ModelSpec = DISCOGS519_MAEST,
) -> None:
    missing = missing_files(model_dir, spec)
    if not missing:
        return

    names = ", ".join(item.filename for item in missing)
    raise MissingModelsError(
        f"Missing or invalid model files in {model_dir}: {names}. "
        f"Run `settag models download --model-dir {model_dir}`."
    )


def _default_urlopen(url: str) -> BinaryIO:
    return urllib.request.urlopen(url)  # noqa: S310 - URLs are fixed in the model catalogue.


def download_models(
    model_dir: Path,
    *,
    spec: ModelSpec = DISCOGS519_MAEST,
    force: bool = False,
    urlopen: UrlOpen = _default_urlopen,
) -> dict[str, object]:
    model_dir.mkdir(parents=True, exist_ok=True)
    print(
        "license: UPF publicly offers these Essentia models for non-commercial use, "
        "but its documentation conflicts on the exact CC variant. Professional or "
        "revenue-generating use may require separate permission; see "
        "https://essentia.upf.edu/licensing_information.html and "
        "https://essentia.upf.edu/models.html",
        file=sys.stderr,
    )

    for item in spec.files:
        destination = model_dir / item.filename
        if (
            destination.is_file()
            and not force
            and sha256_file(destination) == item.sha256
        ):
            print(f"exists: {destination}", file=sys.stderr)
            continue

        temporary = destination.with_suffix(destination.suffix + ".part")
        print(f"download: {item.url}", file=sys.stderr)
        try:
            with urlopen(item.url) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            actual = sha256_file(temporary)
            if actual != item.sha256:
                raise RuntimeError(
                    f"SHA-256 mismatch for {item.filename}: "
                    f"expected {item.sha256}, got {actual}"
                )
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    manifest = installed_manifest(model_dir, spec)
    manifest_path = model_dir / "installed.json"
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_manifest.replace(manifest_path)
    return manifest


def installed_manifest(
    model_dir: Path,
    spec: ModelSpec = DISCOGS519_MAEST,
) -> dict[str, object]:
    require_models(model_dir, spec)
    return {
        "schema": "settag.models/v1",
        "id": spec.id,
        "license": spec.license,
        "files": {
            item.role: {
                "name": item.filename,
                "sha256": item.sha256,
                "size_bytes": (model_dir / item.filename).stat().st_size,
                "url": item.url,
            }
            for item in spec.files
        },
    }


def task_specs(tasks: Iterable[AnalysisTask]) -> tuple[tuple[AnalysisTask, ModelSpec], ...]:
    return tuple((task, MODEL_SPECS_BY_TASK[task]) for task in ordered_tasks(tasks))


def missing_task_files(
    model_dir: Path,
    tasks: Iterable[AnalysisTask],
) -> dict[AnalysisTask, list[ModelFile]]:
    return {
        task: missing
        for task, spec in task_specs(tasks)
        if (missing := missing_files(model_dir, spec))
    }


def require_task_models(model_dir: Path, tasks: Iterable[AnalysisTask]) -> None:
    missing = missing_task_files(model_dir, tasks)
    if not missing:
        return
    details = "; ".join(
        f"{task}: {', '.join(item.filename for item in files)}"
        for task, files in missing.items()
    )
    requested = ",".join(ordered_tasks(tasks))
    raise MissingModelsError(
        f"Missing model files in {model_dir}: {details}. "
        f"Run `settag models download --tasks {requested} --model-dir {model_dir}`."
    )


def installed_task_manifests(
    model_dir: Path,
    tasks: Iterable[AnalysisTask],
) -> dict[AnalysisTask, dict[str, object]]:
    require_task_models(model_dir, tasks)
    return {
        task: installed_manifest(model_dir, spec)
        for task, spec in task_specs(tasks)
    }


def download_task_models(
    model_dir: Path,
    tasks: Iterable[AnalysisTask],
    *,
    force: bool = False,
    urlopen: UrlOpen = _default_urlopen,
) -> dict[str, object]:
    selected = task_specs(tasks)
    model_dir.mkdir(parents=True, exist_ok=True)
    print(
        "license: UPF publicly offers these Essentia models for non-commercial use, "
        "but its documentation conflicts on the exact CC variant. Professional or "
        "revenue-generating use may require separate permission; see "
        "https://essentia.upf.edu/licensing_information.html and "
        "https://essentia.upf.edu/models.html",
        file=sys.stderr,
    )

    files_by_name: dict[str, ModelFile] = {}
    for _, spec in selected:
        for item in spec.files:
            files_by_name.setdefault(item.filename, item)

    for item in files_by_name.values():
        destination = model_dir / item.filename
        if (
            destination.is_file()
            and not force
            and sha256_file(destination) == item.sha256
        ):
            print(f"exists: {destination}", file=sys.stderr)
            continue
        temporary = destination.with_suffix(destination.suffix + ".part")
        print(f"download: {item.url}", file=sys.stderr)
        try:
            with urlopen(item.url) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            actual = sha256_file(temporary)
            if actual != item.sha256:
                raise RuntimeError(
                    f"SHA-256 mismatch for {item.filename}: "
                    f"expected {item.sha256}, got {actual}"
                )
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    manifests = installed_task_manifests(model_dir, ordered_tasks(tasks))
    manifest = {
        "schema": "settag.models/v2",
        "tasks": manifests,
    }
    manifest_path = model_dir / "installed.json"
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_manifest.replace(manifest_path)
    return manifest
