"""Beam (b1m.ai) plugin for Hermes Agent: transfers, fan-out and Rooms as native tools."""

from __future__ import annotations

from pathlib import Path

from .beam_hermes import rooms, schemas, transfers

SKILL_PATH = Path(__file__).resolve().parent / "skills" / "beam" / "SKILL.md"

TOOLS = (
    (schemas.ACCOUNT, transfers.account),
    (schemas.TRANSFER_ESTIMATE, transfers.estimate),
    (schemas.TRANSFER_START, transfers.start),
    (schemas.TRANSFER_STATUS, transfers.status),
    (schemas.TRANSFER_LIST, transfers.list_transfers),
    (schemas.TRANSFER_CANCEL, transfers.cancel),
    (schemas.ROOMS, rooms.rooms),
    (schemas.ROOM_LISTEN, rooms.listen),
    (schemas.ROOM_MESSAGES, rooms.messages),
)


def register(ctx) -> None:
    for schema, handler in TOOLS:
        ctx.register_tool(name=schema["name"], toolset="beam", schema=schema, handler=handler)
    ctx.register_skill("beam", SKILL_PATH)
