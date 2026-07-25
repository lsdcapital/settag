import hashlib
from io import BytesIO
from pathlib import Path
from typing import cast

from settag.catalog import ModelFile, ModelSpec
from settag.model_store import (
    default_model_dir,
    download_task_models,
    installed_manifest,
    missing_files,
)


def test_default_model_dir_respects_xdg_cache_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    assert default_model_dir() == tmp_path / "settag" / "models"


def test_download_validates_digest_and_records_pinned_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    content = b"pinned model"
    digest = hashlib.sha256(content).hexdigest()
    model_file = ModelFile(
        role="embedding",
        filename="model.pb",
        url="https://models.example/model.pb",
        sha256=digest,
    )
    spec = ModelSpec(
        id="example/model/v1",
        license="test license",
        embedding_output="embedding",
        classifier_input="input",
        classifier_output="output",
        sample_rate=16_000,
        files=(model_file,),
    )
    monkeypatch.setattr("settag.model_store.MODEL_SPECS_BY_TASK", {"genre": spec})

    manifest = download_task_models(
        tmp_path,
        ("genre",),
        urlopen=lambda _url: BytesIO(content),
    )

    assert missing_files(tmp_path, spec) == []
    assert manifest["schema"] == "settag.models/v2"
    raw_tasks = manifest["tasks"]
    assert isinstance(raw_tasks, dict)
    tasks = cast(dict[str, object], raw_tasks)
    raw_genre = tasks["genre"]
    assert raw_genre == installed_manifest(tmp_path, spec)
    assert isinstance(raw_genre, dict)
    assert all(isinstance(key, str) for key in raw_genre)
    genre = cast(dict[str, object], raw_genre)
    assert genre["id"] == "example/model/v1"
    assert genre["license"] == "test license"
    raw_files = genre["files"]
    assert isinstance(raw_files, dict)
    assert all(isinstance(key, str) for key in raw_files)
    files = cast(dict[str, object], raw_files)
    raw_embedding = files["embedding"]
    assert isinstance(raw_embedding, dict)
    assert all(isinstance(key, str) for key in raw_embedding)
    embedding = cast(dict[str, object], raw_embedding)
    assert embedding["sha256"] == digest


def test_wrong_digest_is_reported_as_missing(tmp_path: Path) -> None:
    path = tmp_path / "model.pb"
    path.write_bytes(b"wrong model")
    spec = ModelSpec(
        id="example/model/v1",
        license="test license",
        embedding_output="embedding",
        classifier_input="input",
        classifier_output="output",
        sample_rate=16_000,
        files=(
            ModelFile(
                role="embedding",
                filename=path.name,
                url="https://models.example/model.pb",
                sha256="0" * 64,
            ),
        ),
    )

    assert missing_files(tmp_path, spec) == [spec.files[0]]
