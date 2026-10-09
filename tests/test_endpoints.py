from __future__ import annotations

import json

import pytest

from beam_hermes.common import BeamToolError
from beam_hermes.endpoints import build_spec, parse_endpoint


def error_kind(raw: str) -> str:
    with pytest.raises(BeamToolError) as caught:
        parse_endpoint(raw)
    return caught.value.kind


@pytest.mark.parametrize(
    ("raw", "scheme", "bucket", "key"),
    [
        ("s3://src/data.tar", "s3", "src", "data.tar"),
        ("r2://dst/dir/data.tar", "r2", "dst", "dir/data.tar"),
        ("s3c://backup/", "s3c", "backup", ""),
        ("hippius://bucket/a/b/c.bin", "hippius", "bucket", "a/b/c.bin"),
        ("S3://src/key", "s3", "src", "key"),
    ],
)
def test_parses_bucket_schemes(raw: str, scheme: str, bucket: str, key: str) -> None:
    endpoint = parse_endpoint(raw)
    assert (endpoint.scheme, endpoint.bucket, endpoint.key, endpoint.prefix) == (scheme, bucket, key, "")


@pytest.mark.parametrize(
    "raw", ["gs://b/k", "gcs://b/k", "azure://c/k", "http://h/k", "https://h/k", "ftp://h/k"]
)
def test_rejects_unsupported_schemes(raw: str) -> None:
    assert error_kind(raw) == "unsupported_scheme"


@pytest.mark.parametrize(
    "raw", ["s3://b/k?region=x", "s3://b/k#frag", "s3:///k", "r2://b/k?env=1x", "r2://b/k?env="]
)
def test_rejects_invalid_uris(raw: str) -> None:
    assert error_kind(raw) == "invalid_uri"


@pytest.mark.parametrize("raw", ["", "   ", None, 42])
def test_rejects_non_strings(raw: object) -> None:
    with pytest.raises(BeamToolError) as caught:
        parse_endpoint(raw)
    assert caught.value.kind == "invalid_uri"


def test_env_prefix_is_upper_cased_and_stripped_from_display() -> None:
    endpoint = parse_endpoint("s3://bucket/key?env=dst")
    assert endpoint.prefix == "DST_"
    assert endpoint.uri == "s3://bucket/key"


@pytest.mark.parametrize(
    ("raw", "repo_type", "repo_id", "revision", "path", "location"),
    [
        (
            "hf://org/repo/weights/model.bin",
            "model",
            "org/repo",
            "main",
            "weights/model.bin",
            "hf://org/repo@main/weights/model.bin",
        ),
        (
            "hf://datasets/org/corpus@v2/data.parquet",
            "dataset",
            "org/corpus",
            "v2",
            "data.parquet",
            "hf://datasets/org/corpus@v2/data.parquet",
        ),
        (
            "hf://spaces/org/app/app.py",
            "space",
            "org/app",
            "main",
            "app.py",
            "hf://spaces/org/app@main/app.py",
        ),
        (
            "hf://org/repo@refs/pr/3/file.bin",
            "model",
            "org/repo",
            "refs/pr/3",
            "file.bin",
            "hf://org/repo@refs/pr/3/file.bin",
        ),
        (
            "hf://org/repo@refs/convert/parquet/x.parquet",
            "model",
            "org/repo",
            "refs/convert/parquet",
            "x.parquet",
            "hf://org/repo@refs/convert/parquet/x.parquet",
        ),
    ],
)
def test_parses_hugging_face(
    raw: str, repo_type: str, repo_id: str, revision: str, path: str, location: str
) -> None:
    endpoint = parse_endpoint(raw)
    assert (endpoint.hf_repo_type, endpoint.hf_repo_id, endpoint.hf_revision, endpoint.key) == (
        repo_type,
        repo_id,
        revision,
        path,
    )
    assert endpoint.location == location


@pytest.mark.parametrize("raw", ["hf://org", "hf://datasets/org", "hf://org@main/file"])
def test_rejects_hugging_face_without_org_and_repo(raw: str) -> None:
    assert error_kind(raw) == "invalid_uri"


def test_build_spec_reads_scheme_variables(r2_env: dict[str, str]) -> None:
    spec = build_spec("r2://src/data.tar", ["r2://dst/copy.tar"])
    assert spec.source.credentials == ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ACCOUNT_ID"]
    assert spec.credential_variables() == ["R2_ACCESS_KEY_ID", "R2_ACCOUNT_ID", "R2_SECRET_ACCESS_KEY"]
    described = json.dumps(spec.describe())
    for value in r2_env.values():
        assert value not in described


def test_env_prefix_reads_prefixed_variables(monkeypatch: pytest.MonkeyPatch, r2_env: dict[str, str]) -> None:
    monkeypatch.setenv("DST_AWS_ACCESS_KEY_ID", "dst-key-id")
    monkeypatch.setenv("DST_AWS_SECRET_ACCESS_KEY", "dst-secret")
    spec = build_spec("r2://src/data.tar", ["s3://dst/data.tar?env=DST"])
    assert spec.destinations[0].credentials == ["DST_AWS_ACCESS_KEY_ID", "DST_AWS_SECRET_ACCESS_KEY"]
    assert spec.destination_models[0].access_key_id == "dst-key-id"
    assert spec.destination_models[0].region == "us-east-1"
    assert "AWS_ACCESS_KEY_ID" not in spec.credential_variables()


def test_missing_variable_is_named(r2_env: dict[str, str]) -> None:
    with pytest.raises(BeamToolError) as caught:
        build_spec("r2://src/data.tar", ["s3://dst/data.tar?env=DST"])
    assert caught.value.kind == "missing_env"
    assert caught.value.details["variable"] == "DST_AWS_ACCESS_KEY_ID"


def test_r2_endpoint_url_replaces_account_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://example.r2.cloudflarestorage.com")
    spec = build_spec("r2://src/a.bin", ["r2://dst/a.bin"])
    assert "R2_ACCOUNT_ID" not in spec.credential_variables()


def test_destination_folder_receives_source_name(r2_env: dict[str, str]) -> None:
    spec = build_spec("r2://src/dir/data.tar", ["r2://dst/backups/", "r2://dst2"])
    assert [d.key for d in spec.destinations] == ["backups/data.tar", "data.tar"]


def test_source_must_name_one_object(r2_env: dict[str, str]) -> None:
    with pytest.raises(BeamToolError) as caught:
        build_spec("r2://src/dir/", ["r2://dst/x"])
    assert caught.value.kind == "invalid_uri"


def test_duplicate_destinations_are_rejected(r2_env: dict[str, str]) -> None:
    with pytest.raises(BeamToolError) as caught:
        build_spec("r2://src/a.bin", ["r2://dst/a.bin", "r2://dst/"])
    assert caught.value.kind == "duplicate_destination"


def test_needs_a_destination(r2_env: dict[str, str]) -> None:
    with pytest.raises(BeamToolError) as caught:
        build_spec("r2://src/a.bin", [])
    assert caught.value.kind == "invalid_argument"


def test_hippius_and_hugging_face_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPIUS_API_TOKEN", "hippius-token")
    monkeypatch.setenv("HF_TOKEN", "hf-token")
    spec = build_spec(
        "hippius://bucket/model.safetensors",
        ["hf://org/repo/model.safetensors"],
        {"commit_message": "Add weights", "allow_source_rehash": True},
    )
    assert spec.source_model.base_url == "https://api.hippius.com"
    destination = spec.destination_models[0]
    assert destination.repo_id == "org/repo"
    assert destination.commit_message == "Add weights"
    assert spec.credential_variables() == ["HF_TOKEN", "HIPPIUS_API_TOKEN"]
