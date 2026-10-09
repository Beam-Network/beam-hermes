from __future__ import annotations

from pathlib import Path

import pytest

from beam_hermes import state

CREDENTIAL_VARIABLES = (
    "BEAM_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_ACCOUNT_ID",
    "R2_ENDPOINT_URL",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_REGION",
    "S3_PROVIDER",
    "HIPPIUS_API_TOKEN",
    "HIPPIUS_BASE_URL",
    "HF_TOKEN",
    "HF_ENDPOINT",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        for prefix in ("SRC_", "DST_"):
            monkeypatch.delenv(prefix + name, raising=False)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "plugin-data" / "beam"
    root.mkdir(parents=True)
    monkeypatch.setattr(state, "data_dir", lambda: root)
    return root


@pytest.fixture
def r2_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "R2_ACCESS_KEY_ID": "test-access-key-id",
        "R2_SECRET_ACCESS_KEY": "test-secret-value-not-real",
        "R2_ACCOUNT_ID": "0123456789abcdef0123456789abcdef",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values
