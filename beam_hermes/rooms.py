"""Beam Rooms tools: the ``beam`` CLI with ``--json``, plus detached channel listeners.

The CLI drives the Beam agent on this machine over its owner-only local socket. Invitation
tokens and media bearer tokens are written to owner-only files and never returned.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import state
from .common import BeamToolError, bool_arg, int_arg, list_arg, text_arg, tool

ROOMS_DIR = "rooms"
LISTENER_PREFIX = "bl"
ACTIONS = (
    "agent_status",
    "agent_connect",
    "list",
    "create",
    "show",
    "channel_list",
    "channel_create",
    "grant_list",
    "grant_put",
    "invite",
    "join",
    "publish",
    "member_list",
    "media_publish",
)
ROOM_STATES = ("active", "closed", "all")
CHANNEL_KINDS = ("message", "stream", "datagram", "request-reply", "object", "media")
VISIBILITIES = ("restricted", "room")
GRANT_ACTIONS = ("discover", "subscribe", "publish", "manage")
ACTION_NAME = re.compile(r"^[a-z][a-z_-]{0,31}$")
SUBJECT_TYPES = ("member", "role")
PUBLISH_RETENTION = ("none", "sender_local")
LISTEN_RETENTION = ("none", "receiver_local")
PERSISTENCE_MODES = ("none", "sender_local", "receiver_local")
MAX_MESSAGE_BYTES = 1 << 20
ROOM_ENV = ("BEAM_API_KEY", "BEAM_TUNNEL_ROOM_DATA_ADDR")
EXIT_KINDS = {
    2: "usage",
    3: "configuration",
    4: "auth",
    6: "agent_unavailable",
    7: "version_mismatch",
    8: "not_found",
    9: "conflict",
    10: "operation_failed",
}
SECRET_KEY = re.compile(r"token|secret|bearer|authorization|password|api_?key", re.IGNORECASE)
NOISE_KEYS = frozenset(
    {
        "assigned_by_member_id",
        "authorization_epoch",
        "boot_id",
        "channel_revision",
        "created_by_member_id",
        "delivery",
        "granted_by_member_id",
        "key_epoch",
        "last_attempt_at",
        "lease_expires_at",
        "lease_version",
        "limits",
        "notification_cursor",
        "plan_version",
        "presence_version",
        "public_key",
        "public_key_fingerprint",
        "qos",
        "renew_after",
        "resume",
        "updated_at",
        "version",
    }
)
ZERO_TIME = "0001-01-01T00:00:00Z"
UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class Command:
    argv: list[str]
    stdin: str | None = None
    timeout: float = 60.0
    extra: dict[str, Any] = field(default_factory=dict)


def ident(args: dict[str, Any], name: str, *, required: bool = True) -> str | None:
    """A Room, channel, member or role ID passed as one argument; never an option."""
    value = text_arg(args, name, required=required)
    if value is None:
        return None
    if value.startswith("-") or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise BeamToolError("invalid_argument", f"`{name}` is not a valid ID.", argument=name)
    return value


def choice(args: dict[str, Any], name: str, allowed: tuple[str, ...]) -> str | None:
    value = text_arg(args, name)
    if value is not None and value not in allowed:
        raise BeamToolError(
            "invalid_argument", f"`{name}` must be one of {', '.join(allowed)}.", argument=name
        )
    return value


def actions_list(values: list[str], argument: str) -> str:
    actions = [a.strip() for value in values for a in value.split(",") if a.strip()]
    if not actions or not all(ACTION_NAME.match(action) for action in actions):
        raise BeamToolError(
            "invalid_argument",
            f"`{argument}` takes channel actions such as {', '.join(GRANT_ACTIONS)}.",
            argument=argument,
        )
    return ",".join(dict.fromkeys(actions))


def channel_access(values: list[str]) -> list[str]:
    entries = []
    for value in values:
        channel, sep, actions = value.partition("=")
        channel = channel.strip()
        if not sep or not channel or channel.startswith("-"):
            raise BeamToolError(
                "invalid_argument",
                "`channel_access` entries look like CHANNEL_ID=discover,subscribe.",
                argument="channel_access",
            )
        entries.append(f"--channel-access={channel}={actions_list([actions], 'channel_access')}")
    return entries


def message_payload(args: dict[str, Any]) -> str:
    value = args.get("message")
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    if not isinstance(value, str) or not value:
        raise BeamToolError("invalid_argument", "`message` is required.", argument="message")
    if len(value.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise BeamToolError("invalid_argument", "`message` is larger than 1 MiB.", argument="message")
    return value


def build_command(action: str, args: dict[str, Any], *, invitation_path: Path | None = None) -> Command:
    """Translate one ``beam_rooms`` call into ``beam`` CLI arguments (without the binary)."""
    base = ["--json", "--no-interactive"]
    if action == "agent_status":
        return Command([*base, "agent", "status"])
    if action == "agent_connect":
        label = ident(args, "label", required=False)
        return Command([*base, "agent", "connect", *([f"--label={label}"] if label else [])], timeout=180)
    if action == "list":
        room_state = choice(args, "state", ROOM_STATES)
        return Command([*base, "room", "list", *([f"--state={room_state}"] if room_state else [])])
    if action == "create":
        if not bool_arg(args, "confirmed"):
            raise BeamToolError(
                "confirmation_required",
                "Creating a Room costs 1 credit. Ask the user, then call again with confirmed=true.",
            )
        key = ident(args, "idempotency_key", required=False) or f"hermes-{uuid.uuid4()}"
        return Command(
            [*base, "room", "create", f"--idempotency-key={key}"],
            timeout=120,
            extra={"idempotency_key": key},
        )
    if action == "join":
        room = ident(args, "room")
        path = Path(text_arg(args, "invitation_file", required=True) or "").expanduser().resolve()
        if not path.is_file():
            raise BeamToolError(
                "invalid_argument", "`invitation_file` is not a file.", argument="invitation_file"
            )
        return Command(
            [*base, "room", "join", room or "", "--invitation-file", str(path)],
            timeout=180,
            extra={"invitation_file": str(path)},
        )

    room = ident(args, "room") or ""
    if action == "show":
        return Command([*base, "room", room, "show"])
    if action == "member_list":
        return Command([*base, "room", room, "member", "list"])
    if action == "channel_list":
        return Command([*base, "room", room, "channel", "list"])
    if action == "channel_create":
        argv = [
            *base,
            "room",
            room,
            "channel",
            "create",
            f"--name={ident(args, 'name')}",
            f"--kind={choice(args, 'kind', CHANNEL_KINDS) or 'message'}",
        ]
        visibility = choice(args, "visibility", VISIBILITIES)
        if visibility:
            argv.append(f"--visibility={visibility}")
        modes = list_arg(args, "persistence_modes")
        if modes:
            unknown = sorted(set(modes) - set(PERSISTENCE_MODES))
            if unknown:
                raise BeamToolError(
                    "invalid_argument",
                    f"`persistence_modes` takes {', '.join(PERSISTENCE_MODES)}.",
                    argument="persistence_modes",
                )
            argv.append(f"--persistence-modes={','.join(modes)}")
        if args.get("persistence_max_bytes") is not None:
            max_bytes = int_arg(args, "persistence_max_bytes", 0, minimum=1, maximum=1 << 40)
            argv.append(f"--persistence-max-bytes={max_bytes}")
        if args.get("persistence_max_age_seconds") is not None:
            max_age = int_arg(args, "persistence_max_age_seconds", 0, minimum=1, maximum=365 * 86400)
            argv.append(f"--persistence-max-age={max_age}s")
        return Command(argv)
    if action == "invite":
        if invitation_path is None:
            raise BeamToolError("unexpected", "No invitation file path was prepared.")
        argv = [
            *base,
            "room",
            room,
            "invite",
            *channel_access(list_arg(args, "channel_access")),
            *(f"--role={ident({'role': role}, 'role')}" for role in list_arg(args, "roles")),
            f"--max-uses={int_arg(args, 'max_uses', 1, minimum=1, maximum=100)}",
            f"--ttl={int_arg(args, 'ttl_seconds', 900, minimum=60, maximum=7 * 86400)}",
        ]
        agent_id = ident(args, "agent_id", required=False)
        principal_id = ident(args, "principal_id", required=False)
        if agent_id and principal_id:
            raise BeamToolError("invalid_argument", "Give agent_id or principal_id, not both.")
        if agent_id:
            argv.append(f"--agent={agent_id}")
        if principal_id:
            argv.append(f"--principal={principal_id}")
        argv += ["--out", str(invitation_path)]
        return Command(argv)

    channel = ident(args, "channel") or ""
    if action == "grant_list":
        return Command([*base, "room", room, "channel", channel, "grant", "list"])
    if action == "grant_put":
        return Command(
            [
                *base,
                "room",
                room,
                "channel",
                channel,
                "grant",
                "put",
                f"--subject-type={choice(args, 'subject_type', SUBJECT_TYPES) or 'member'}",
                f"--subject={ident(args, 'subject')}",
                f"--actions={actions_list(list_arg(args, 'actions', required=True), 'actions')}",
            ]
        )
    if action == "publish":
        argv = [*base, "room", room, "channel", channel, "publish", "--stdin"]
        content_type = text_arg(args, "content_type")
        if content_type:
            argv.append(f"--content-type={content_type}")
        retention = choice(args, "retention", PUBLISH_RETENTION)
        if retention:
            argv.append(f"--retention={retention}")
        key = ident(args, "idempotency_key", required=False)
        if key:
            argv.append(f"--idempotency-key={key}")
        return Command(argv, stdin=message_payload(args))
    if action == "media_publish":
        publisher = ident(args, "publisher", required=False)
        return Command(
            [
                *base,
                "room",
                room,
                "channel",
                channel,
                "media",
                "publish",
                *([f"--as={publisher}"] if publisher else []),
            ]
        )
    raise BeamToolError(
        "invalid_argument", f"`action` must be one of {', '.join(ACTIONS)}.", argument="action"
    )


def listen_argv(room: str, channel: str, retention: str | None) -> list[str]:
    argv = ["--json", "--no-interactive", "room", room, "channel", channel, "listen"]
    if retention:
        argv.append(f"--retention={retention}")
    return argv


def beam_binary() -> str:
    found = shutil.which("beam")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "beam"
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    raise BeamToolError(
        "beam_cli_missing",
        "The beam CLI is not installed on this machine. Ask the user to install it with the "
        "official installer documented at https://docs.b1m.ai.",
    )


def parse_output(text: str) -> Any:
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"output": text[-2000:]}


def cli_error(returncode: int, stdout: str, stderr: str) -> BeamToolError:
    for line in reversed(stderr.strip().splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            details = {k: v for k, v in error.items() if k not in ("message", "kind", "code")}
            return BeamToolError(
                str(error.get("kind") or EXIT_KINDS.get(returncode, "beam_error")),
                str(error.get("message") or "beam failed."),
                exit_code=returncode,
                **redact(details),
            )
    tail = (stderr.strip() or stdout.strip())[-500:]
    return BeamToolError(
        EXIT_KINDS.get(returncode, "beam_error"), tail or "beam failed.", exit_code=returncode
    )


def run_beam(command: Command) -> Any:
    try:
        completed = subprocess.run(
            [beam_binary(), *command.argv],
            input=command.stdin,
            capture_output=True,
            text=True,
            timeout=command.timeout,
            env=state.child_env(ROOM_ENV),
            cwd=state.data_dir(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise BeamToolError("timeout", f"beam did not finish within {command.timeout:.0f} seconds.") from None
    if completed.returncode != 0:
        raise cli_error(completed.returncode, completed.stdout, completed.stderr)
    return parse_output(completed.stdout)


def redact(value: Any) -> Any:
    """Replace the value of every secret-looking key, at any depth."""
    if isinstance(value, dict):
        return {
            k: "[redacted]" if SECRET_KEY.search(str(k)) and v not in (None, "") else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def compact(value: Any) -> Any:
    """Drop transport tuning, versions and empty timestamps the model rarely needs."""
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items() if k not in NOISE_KEYS and v != ZERO_TIME}
    if isinstance(value, list):
        return [compact(item) for item in value]
    return value


def find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = find_key(item, key)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = find_key(item, key)
            if found is not None:
                return found
    return None


def safe_name(*parts: str) -> str:
    return "-".join(UNSAFE_NAME.sub("_", part)[:80] for part in parts if part)


def save_media(output: Any, room: str, channel: str, publisher: str | None) -> dict[str, Any]:
    path = state.private_dir(ROOMS_DIR, "media") / f"{safe_name(room, channel, publisher or 'main')}.json"
    state.write_private(path, json.dumps(output, indent=2) + "\n")
    whip_url = find_key(output, "whip_url")
    keys = ("room_id", "channel_id", "name", "workload_id", "session_id", "expires_at")
    return {
        "action": "media_publish",
        "file": str(path),
        "whip_host": urlsplit(whip_url).hostname if isinstance(whip_url, str) else None,
        **{k: find_key(output, k) for k in keys},
        "hint": "The file (owner-only) holds the WHIP URL and its bearer token for an encoder "
        "such as OBS or ffmpeg. Give the user the file path; never print the token.",
    }


@tool
def rooms(args: dict[str, Any]) -> dict[str, Any]:
    action = choice(args, "action", ACTIONS)
    if action is None:
        raise BeamToolError("invalid_argument", "`action` is required.", argument="action")
    invitation_path = None
    if action == "invite":
        room = ident(args, "room") or ""
        invitation_path = (
            state.private_dir(ROOMS_DIR, "invitations") / f"{safe_name(room, state.new_id('in'))}.token"
        )
    command = build_command(action, args, invitation_path=invitation_path)
    output = run_beam(command)
    if action == "media_publish":
        return save_media(
            output,
            ident(args, "room") or "",
            ident(args, "channel") or "",
            ident(args, "publisher", required=False),
        )
    if action == "join" and bool_arg(args, "remove_invitation_file"):
        Path(command.extra["invitation_file"]).unlink(missing_ok=True)
        command.extra["invitation_file_removed"] = True
    result = redact(output)
    if not bool_arg(args, "verbose"):
        result = compact(result)
    response: dict[str, Any] = {"action": action, "result": result, **command.extra}
    if action == "invite":
        response["invitation_file"] = str(invitation_path)
        response["hint"] = (
            "The owner-only file holds a one-use bearer token. Deliver the file privately, as "
            "the user approves; never print or paste its contents. The invitee runs beam_rooms "
            "action=join with it."
        )
    return response


def listener_dir(listener_id: str) -> Path:
    state.check_id(listener_id, LISTENER_PREFIX, "listener_id")
    path = state.data_dir() / ROOMS_DIR / "listeners" / listener_id
    if not path.is_dir():
        raise BeamToolError(
            "not_found", f"No listener {listener_id} on this machine.", listener_id=listener_id
        )
    return path


def listener_markers(meta: dict[str, Any]) -> list[str]:
    return [str(meta.get("room")), str(meta.get("channel")), "listen"]


def listener_error(path: Path) -> dict[str, Any] | None:
    stderr = state.tail_text(path / "stderr.log")
    if not stderr:
        return None
    error = cli_error(1, "", stderr)
    return {"error": str(error), "kind": error.kind}


@tool
def listen(args: dict[str, Any]) -> dict[str, Any]:
    action = choice(args, "action", ("start", "stop", "list"))
    if action == "list":
        root = state.private_dir(ROOMS_DIR, "listeners")
        listeners = []
        for path in sorted(root.iterdir(), key=lambda p: p.name, reverse=True)[:20]:
            meta = state.read_json(path / "listener.json")
            listeners.append(
                {
                    "listener_id": path.name,
                    "room": meta.get("room"),
                    "channel": meta.get("channel"),
                    "started_at": meta.get("started_at"),
                    "running": state.is_running(meta.get("pid"), listener_markers(meta)),
                }
            )
        return {"listeners": listeners}
    if action == "stop":
        path = listener_dir(text_arg(args, "listener_id", required=True) or "")
        meta = state.read_json(path / "listener.json")
        was_running = state.terminate(meta.get("pid"), listener_markers(meta))
        meta["stopped_at"] = state.now()
        state.write_json(path / "listener.json", meta)
        return {"listener_id": path.name, "stopped": True, "was_running": was_running}
    if action != "start":
        raise BeamToolError("invalid_argument", "`action` must be start, stop or list.", argument="action")

    room = ident(args, "room") or ""
    channel = ident(args, "channel") or ""
    retention = choice(args, "retention", LISTEN_RETENTION)
    binary = beam_binary()
    listener_id = state.new_id(LISTENER_PREFIX)
    path = state.private_dir(ROOMS_DIR, "listeners", listener_id)
    process = state.spawn_detached(
        [binary, *listen_argv(room, channel, retention)],
        cwd=path,
        env=state.child_env(ROOM_ENV),
        stdout_path=path / "messages.jsonl",
        stderr_path=path / "stderr.log",
    )
    meta = {
        "listener_id": listener_id,
        "pid": process.pid,
        "room": room,
        "channel": channel,
        "retention": retention or "none",
        "started_at": state.now(),
    }
    state.write_json(path / "listener.json", meta)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.2)
    if process.poll() is not None:
        failure = listener_error(path) or {"error": "beam listen exited.", "kind": "listen_exited"}
        return {"listener_id": listener_id, "running": False, **failure}
    return {
        "listener_id": listener_id,
        "room": room,
        "channel": channel,
        "running": True,
        "messages_file": str(path / "messages.jsonl"),
        "hint": "Listening. Publish only after this, and read messages with beam_room_messages "
        "using next_cursor. Stop it with action=stop when done.",
    }


@tool
def messages(args: dict[str, Any]) -> dict[str, Any]:
    path = listener_dir(text_arg(args, "listener_id", required=True) or "")
    cursor = int_arg(args, "cursor", 0, minimum=0, maximum=1 << 53)
    limit = int_arg(args, "limit", 50, minimum=1, maximum=500)
    max_bytes = int_arg(args, "max_bytes", 65536, minimum=1024, maximum=MAX_MESSAGE_BYTES)
    records, next_cursor = state.read_records(
        path / "messages.jsonl", cursor, limit=limit, max_bytes=max_bytes
    )
    shown = []
    for record in records:
        encoded = json.dumps(record)
        if len(encoded) > max_bytes:
            record = {"truncated": True, "bytes": len(encoded), "preview": encoded[:2000]}
        shown.append(record)
    meta = state.read_json(path / "listener.json")
    running = state.is_running(meta.get("pid"), listener_markers(meta))
    result: dict[str, Any] = {
        "listener_id": path.name,
        "messages": shown,
        "count": len(shown),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "running": running,
    }
    if not running:
        failure = listener_error(path)
        if failure:
            result["listener_error"] = failure
    return result
