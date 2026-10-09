"""Constants, the tool error type and the JSON wrapper every handler uses."""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from typing import Any

PLUGIN_NAME = "beam"
API_URL = "https://beamcore.b1m.ai"
CONSOLE_URL = "https://console.b1m.ai"
BYTES_PER_GB = 1_000_000_000
USD_PER_CREDIT = 0.5

logger = logging.getLogger("beam_hermes")


class BeamToolError(Exception):
    """A failure the model can act on: a stable ``kind`` plus context fields."""

    def __init__(self, kind: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"error": str(self), "kind": self.kind, **self.details}


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def tool(func: Callable[[dict[str, Any]], Any]) -> Callable[..., str]:
    """Wrap a handler so it always returns a JSON string and never raises."""

    @functools.wraps(func)
    def handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        try:
            return dumps(func(args if isinstance(args, dict) else {}))
        except BeamToolError as exc:
            return dumps(exc.payload())
        except Exception as exc:  # noqa: BLE001 - handlers must never raise into Hermes
            logger.exception("beam tool %s failed", func.__name__)
            return dumps({"error": f"{type(exc).__name__}: {exc}", "kind": "unexpected"})

    return handler


def text_arg(args: dict[str, Any], name: str, *, required: bool = False) -> str | None:
    value = args.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BeamToolError("invalid_argument", f"`{name}` is required.", argument=name)
        return None
    if not isinstance(value, str):
        raise BeamToolError("invalid_argument", f"`{name}` must be a string.", argument=name)
    return value.strip()


def int_arg(args: dict[str, Any], name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = args.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise BeamToolError("invalid_argument", f"`{name}` must be an integer.", argument=name)
    value = int(value)
    if not minimum <= value <= maximum:
        raise BeamToolError(
            "invalid_argument",
            f"`{name}` must be between {minimum} and {maximum}.",
            argument=name,
        )
    return value


def bool_arg(args: dict[str, Any], name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise BeamToolError("invalid_argument", f"`{name}` must be true or false.", argument=name)
    return value


def list_arg(args: dict[str, Any], name: str, *, required: bool = False) -> list[str]:
    value = args.get(name)
    if value is None or value == []:
        if required:
            raise BeamToolError("invalid_argument", f"`{name}` is required.", argument=name)
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise BeamToolError(
            "invalid_argument", f"`{name}` must be a list of non-empty strings.", argument=name
        )
    return [item.strip() for item in value]
