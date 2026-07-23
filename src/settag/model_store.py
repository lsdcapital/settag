from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from settag.catalog import DISCOGS519_MAEST, ModelFile, ModelSpec
from settag.hashing import sha256_file


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
    return [item for item in spec.files if not (model_dir / item.filename).is_file()]


def require_models(
    model_dir: Path,
    spec: ModelSpec = DISCOGS519_MAEST,
) -> None:
    missing = missing_files(model_dir, spec)
    if not missing:
        return

    names = ", ".join(item.filename for item in missing)
    raise MissingModelsError(
        f"Missing model files in {model_dir}: {names}. "
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
        "license: Essentia models are offered under CC BY-NC-ND 4.0 for "
        "non-commercial use; see https://essentia.upf.edu/licensing_information.html",
        file=sys.stderr,
    )

    for item in spec.files:
        destination = model_dir / item.filename
        if destination.is_file() and not force:
            print(f"exists: {destination}", file=sys.stderr)
            continue

        temporary = destination.with_suffix(destination.suffix + ".part")
        print(f"download: {item.url}", file=sys.stderr)
        try:
            with urlopen(item.url) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            temporary.replace(destination)
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
        "files": {
            item.role: {
                "name": item.filename,
                "sha256": sha256_file(model_dir / item.filename),
                "url": item.url,
            }
            for item in spec.files
        },
    }
