"""Read-only calls to the Beam HTTP API, authenticated with ``BEAM_API_KEY``."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .common import API_URL, CONSOLE_URL, BeamToolError


def api_key() -> str:
    value = (os.environ.get("BEAM_API_KEY") or "").strip()
    if not value:
        raise BeamToolError(
            "missing_env",
            f"BEAM_API_KEY is not set. Create a key at {CONSOLE_URL} and store it with "
            "`hermes config set BEAM_API_KEY <key>`.",
            variable="BEAM_API_KEY",
        )
    return value


def get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    headers = {"X-Api-Key": api_key(), "Accept": "application/json"}
    try:
        with httpx.Client(base_url=API_URL, timeout=20.0) as client:
            response = client.get(path, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise BeamToolError("network", f"Could not reach the Beam API: {type(exc).__name__}") from None
    if response.status_code == 200:
        return response.json()
    if response.status_code in (401, 403):
        raise BeamToolError(
            "auth",
            f"Beam refused the API key. Check it, or create one at {CONSOLE_URL}.",
            status_code=response.status_code,
        )
    if response.status_code == 404:
        raise BeamToolError("not_found", "Nothing with that ID in this organization.", status_code=404)
    raise BeamToolError("api_error", response.text[:300], status_code=response.status_code)


def whoami() -> dict[str, Any]:
    account = get_json("/auth/me")
    return {
        "status": account.get("status"),
        "user_type": account.get("user_type"),
        "key_role": account.get("current_key_role"),
        "key_prefix": account.get("current_key_prefix"),
    }


def transfer(transfer_id: str) -> dict[str, Any]:
    return get_json(f"/transfers/{transfer_id}")


def transfers(params: dict[str, Any]) -> Any:
    return get_json("/transfers", params=params)
