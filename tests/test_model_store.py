import hashlib
from io import BytesIO
from pathlib import Path

from settag.catalog import ModelFile, ModelSpec
from settag.model_store import (
    default_model_dir,
    download_models,
    installed_manifest,
    missing_files,
)


def test_default_model_dir_respects_xdg_cache_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    assert default_model_dir() == tmp_path / "settag" / "models"


def test_download_validates_digest_and_records_pinned_manifest(tmp_path: Path) -> None:
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

    manifest = download_models(
        tmp_path,
        spec=spec,
        urlopen=lambda _url: BytesIO(content),
    )

    assert missing_files(tmp_path, spec) == []
    assert manifest == installed_manifest(tmp_path, spec)
    assert manifest["id"] == "example/model/v1"
    assert manifest["license"] == "test license"
    assert manifest["files"]["embedding"]["sha256"] == digest


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
