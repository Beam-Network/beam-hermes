from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from beam_hermes import rooms
from beam_hermes.common import BeamToolError

BASE = ["--json", "--no-interactive"]
ROOM = "btr_room_test"
CHANNEL = "btr_channel_test"


def call(handler, args: dict) -> dict:
    result = handler(args)
    assert isinstance(result, str)
    return json.loads(result)


def argv(action: str, **args) -> list[str]:
    return rooms.build_command(action, args).argv


@pytest.mark.parametrize(
    ("action", "args", "expected"),
    [
        ("agent_status", {}, ["agent", "status"]),
        ("agent_connect", {}, ["agent", "connect"]),
        ("agent_connect", {"label": "lab box"}, ["agent", "connect", "--label=lab box"]),
        ("list", {}, ["room", "list"]),
        ("list", {"state": "all"}, ["room", "list", "--state=all"]),
        ("show", {"room": ROOM}, ["room", ROOM, "show"]),
        ("member_list", {"room": ROOM}, ["room", ROOM, "member", "list"]),
        ("channel_list", {"room": ROOM}, ["room", ROOM, "channel", "list"]),
        (
            "grant_list",
            {"room": ROOM, "channel": CHANNEL},
            ["room", ROOM, "channel", CHANNEL, "grant", "list"],
        ),
        (
            "channel_create",
            {"room": ROOM, "name": "events"},
            ["room", ROOM, "channel", "create", "--name=events", "--kind=message"],
        ),
        (
            "channel_create",
            {
                "room": ROOM,
                "name": "inbox",
                "kind": "message",
                "visibility": "room",
                "persistence_modes": ["none", "receiver_local"],
                "persistence_max_bytes": 1048576,
                "persistence_max_age_seconds": 3600,
            },
            [
                "room",
                ROOM,
                "channel",
                "create",
                "--name=inbox",
                "--kind=message",
                "--visibility=room",
                "--persistence-modes=none,receiver_local",
                "--persistence-max-bytes=1048576",
                "--persistence-max-age=3600s",
            ],
        ),
        (
            "grant_put",
            {
                "room": ROOM,
                "channel": CHANNEL,
                "subject": "btr_member_x",
                "actions": ["discover", "subscribe,discover"],
            },
            [
                "room",
                ROOM,
                "channel",
                CHANNEL,
                "grant",
                "put",
                "--subject-type=member",
                "--subject=btr_member_x",
                "--actions=discover,subscribe",
            ],
        ),
        (
            "media_publish",
            {"room": ROOM, "channel": CHANNEL, "publisher": "studio-cam"},
            ["room", ROOM, "channel", CHANNEL, "media", "publish", "--as=studio-cam"],
        ),
    ],
)
def test_build_command(action: str, args: dict, expected: list[str]) -> None:
    assert rooms.build_command(action, args).argv == [*BASE, *expected]


def test_create_requires_confirmation_and_sets_an_idempotency_key() -> None:
    with pytest.raises(BeamToolError) as caught:
        rooms.build_command("create", {})
    assert caught.value.kind == "confirmation_required"
    command = rooms.build_command("create", {"confirmed": True})
    key = command.extra["idempotency_key"]
    assert command.argv == [*BASE, "room", "create", f"--idempotency-key={key}"]
    assert rooms.build_command("create", {"confirmed": True, "idempotency_key": "k1"}).extra == {
        "idempotency_key": "k1"
    }


def test_publish_sends_the_payload_on_stdin() -> None:
    command = rooms.build_command(
        "publish",
        {"room": ROOM, "channel": CHANNEL, "message": "--looks-like-a-flag", "retention": "sender_local"},
    )
    assert command.argv == [
        *BASE,
        "room",
        ROOM,
        "channel",
        CHANNEL,
        "publish",
        "--stdin",
        "--retention=sender_local",
    ]
    assert command.stdin == "--looks-like-a-flag"
    with pytest.raises(BeamToolError):
        rooms.build_command("publish", {"room": ROOM, "channel": CHANNEL, "message": "x" * ((1 << 20) + 1)})
    with pytest.raises(BeamToolError):
        rooms.build_command("publish", {"room": ROOM, "channel": CHANNEL})


def test_invite_writes_to_a_file_and_validates_access(tmp_path: Path) -> None:
    out = tmp_path / "inv.token"
    command = rooms.build_command(
        "invite",
        {
            "room": ROOM,
            "channel_access": ["JOBS=discover,subscribe", "RESULTS=discover,publish"],
            "agent_id": "agt_1",
            "ttl_seconds": 1800,
        },
        invitation_path=out,
    )
    assert command.argv == [
        *BASE,
        "room",
        ROOM,
        "invite",
        "--channel-access=JOBS=discover,subscribe",
        "--channel-access=RESULTS=discover,publish",
        "--max-uses=1",
        "--ttl=1800",
        "--agent=agt_1",
        "--out",
        str(out),
    ]
    for bad in (
        {"room": ROOM, "channel_access": ["JOBS"]},
        {"room": ROOM, "channel_access": ["JOBS=Discover Everything"]},
        {"room": ROOM, "agent_id": "a", "principal_id": "p"},
        {"room": ROOM, "max_uses": 0},
    ):
        with pytest.raises(BeamToolError):
            rooms.build_command("invite", bad, invitation_path=out)


def test_join_resolves_an_existing_invitation_file(tmp_path: Path) -> None:
    token = tmp_path / "invitation.token"
    token.write_text("x\n")
    command = rooms.build_command("join", {"room": ROOM, "invitation_file": str(token)})
    assert command.argv == [*BASE, "room", "join", ROOM, "--invitation-file", str(token.resolve())]
    with pytest.raises(BeamToolError):
        rooms.build_command("join", {"room": ROOM, "invitation_file": str(tmp_path / "missing")})


@pytest.mark.parametrize("room", ["--json", "-x", "", "a\nb"])
def test_ids_never_become_options(room: str) -> None:
    with pytest.raises(BeamToolError):
        rooms.build_command("show", {"room": room})


def test_unknown_action_and_missing_arguments() -> None:
    with pytest.raises(BeamToolError):
        rooms.build_command("close", {"room": ROOM})
    with pytest.raises(BeamToolError):
        rooms.build_command("grant_put", {"room": ROOM, "channel": CHANNEL, "subject": "m"})


def test_redact_and_compact() -> None:
    raw = {
        "invitation": {"invitation_id": "inv_1", "expires_at": "2026-10-09T00:00:00Z"},
        "invitation_token": "SECRET-TOKEN",
        "nested": [{"token": "MEDIA-TOKEN", "whip_url": "https://media.example/whip"}],
        "credential_id": "agc_1",
        "limits": {"max_rate_bps": 1},
        "closed_at": rooms.ZERO_TIME,
        "name": "lab",
    }
    redacted = rooms.redact(raw)
    text = json.dumps(redacted)
    assert "SECRET-TOKEN" not in text and "MEDIA-TOKEN" not in text
    assert redacted["credential_id"] == "agc_1"
    assert rooms.compact(redacted) == {
        "invitation": {"invitation_id": "inv_1", "expires_at": "2026-10-09T00:00:00Z"},
        "invitation_token": "[redacted]",
        "nested": [{"token": "[redacted]", "whip_url": "https://media.example/whip"}],
        "credential_id": "agc_1",
        "name": "lab",
    }


def test_cli_error_reads_the_json_error() -> None:
    stderr = 'warning: something\n{"error":{"code":8,"message":"Room was not found.","hint":"Check the ID.","kind":"room_not_found"}}\n'
    error = rooms.cli_error(8, "", stderr)
    assert (error.kind, str(error), error.details) == (
        "room_not_found",
        "Room was not found.",
        {"exit_code": 8, "hint": "Check the ID."},
    )
    plain = rooms.cli_error(6, "", "agent socket unavailable")
    assert (plain.kind, str(plain)) == ("agent_unavailable", "agent socket unavailable")


def test_rooms_handler_never_returns_tokens(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    def fake_run(command: rooms.Command) -> dict:
        out = command.argv[command.argv.index("--out") + 1]
        Path(out).write_text("SECRET-TOKEN\n")
        return {"invitation": {"invitation_id": "inv_1"}, "invitation_file": out}

    monkeypatch.setattr(rooms, "run_beam", fake_run)
    result = call(rooms.rooms, {"action": "invite", "room": ROOM, "channel_access": ["C=discover,subscribe"]})
    path = Path(result["invitation_file"])
    assert path.parent == data_dir / "rooms" / "invitations"
    assert path.read_text() == "SECRET-TOKEN\n"
    assert "SECRET-TOKEN" not in json.dumps(result)


def test_media_publish_is_saved_to_an_owner_only_file(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    output = {
        "room_id": ROOM,
        "channel_id": CHANNEL,
        "name": "main",
        "workload_id": "wl_1",
        "whip_url": "https://media.b1m.example/whip/abc",
        "token": "MEDIA-BEARER",
        "expires_at": "2026-10-10T00:00:00Z",
    }
    monkeypatch.setattr(rooms, "run_beam", lambda command: output)
    result = call(rooms.rooms, {"action": "media_publish", "room": ROOM, "channel": CHANNEL})
    assert "MEDIA-BEARER" not in json.dumps(result)
    assert result["whip_host"] == "media.b1m.example"
    path = Path(result["file"])
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["token"] == "MEDIA-BEARER"


def test_handler_errors_are_json(data_dir: Path) -> None:
    assert call(rooms.rooms, {})["kind"] == "invalid_argument"
    assert call(rooms.rooms, {"action": "show"})["kind"] == "invalid_argument"
    assert call(rooms.listen, {"action": "stop", "listener_id": "../x"})["kind"] == "invalid_argument"
    assert call(rooms.messages, {"listener_id": "bl_20260101T000000Z_000000"})["kind"] == "not_found"


def test_listener_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data_dir: Path) -> None:
    fake_beam = tmp_path / "beam"
    fake_beam.write_text('#!/bin/sh\necho \'{"payload":"one"}\'\necho \'{"payload":"two"}\'\nexec sleep 30\n')
    fake_beam.chmod(0o700)
    monkeypatch.setattr(rooms, "beam_binary", lambda: str(fake_beam))
    started = call(rooms.listen, {"action": "start", "room": ROOM, "channel": CHANNEL})
    listener_id = started["listener_id"]
    try:
        assert started["running"] is True
        deadline = time.monotonic() + 5
        read: dict = {}
        while time.monotonic() < deadline:
            read = call(rooms.messages, {"listener_id": listener_id})
            if read["count"] == 2:
                break
            time.sleep(0.1)
        assert [m["payload"] for m in read["messages"]] == ["one", "two"]
        again = call(rooms.messages, {"listener_id": listener_id, "cursor": read["next_cursor"]})
        assert again["count"] == 0
        listed = call(rooms.listen, {"action": "list"})["listeners"]
        assert listed[0]["listener_id"] == listener_id
    finally:
        stopped = call(rooms.listen, {"action": "stop", "listener_id": listener_id})
    assert stopped["was_running"] is True
    assert call(rooms.messages, {"listener_id": listener_id})["running"] is False


def test_listener_that_exits_reports_the_cli_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data_dir: Path
) -> None:
    fake_beam = tmp_path / "beam"
    fake_beam.write_text(
        "#!/bin/sh\n"
        'echo \'{"error":{"code":4,"message":"Missing subscribe.","kind":"channel_permission_denied"}}\' >&2\n'
        "exit 4\n"
    )
    fake_beam.chmod(0o700)
    monkeypatch.setattr(rooms, "beam_binary", lambda: str(fake_beam))
    result = call(rooms.listen, {"action": "start", "room": ROOM, "channel": CHANNEL})
    assert result["running"] is False
    assert result["kind"] == "channel_permission_denied"


def test_missing_cli_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(BeamToolError) as caught:
        rooms.beam_binary()
    assert caught.value.kind == "beam_cli_missing"
