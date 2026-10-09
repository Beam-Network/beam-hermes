from __future__ import annotations

import sys
from pathlib import Path

import pytest

from beam_hermes import state
from beam_hermes.common import BeamToolError


def test_read_records_parses_complete_lines_from_an_offset(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        b'{"event": "started"}\nnot json\n[1, 2]\n\n{"event": "created", "transfer_id": "t"}\n{"event": "prog'
    )
    records, offset = state.read_records(path)
    assert records == [
        {"event": "started"},
        {"event": "log", "line": "not json"},
        {"event": "log", "line": "[1, 2]"},
        {"event": "created", "transfer_id": "t"},
    ]
    assert path.read_bytes()[offset:] == b'{"event": "prog'
    with path.open("ab") as handle:
        handle.write(b'ress"}\n')
    more, next_offset = state.read_records(path, offset)
    assert more == [{"event": "progress"}]
    assert next_offset == path.stat().st_size


def test_read_records_respects_limit_and_size(tmp_path: Path) -> None:
    path = tmp_path / "messages.jsonl"
    path.write_text("".join(f'{{"n": {n}, "pad": "{"x" * 100}"}}\n' for n in range(10)))
    records, offset = state.read_records(path, limit=3)
    assert [r["n"] for r in records] == [0, 1, 2]
    records, _ = state.read_records(path, offset, max_bytes=250)
    assert [r["n"] for r in records] == [3, 4]
    assert state.read_records(tmp_path / "missing.jsonl", 7) == ([], 7)


def test_write_private_is_owner_only_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "secret.json"
    state.write_json(path, {"a": 1})
    assert state.read_json(path) == {"a": 1}
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["secret.json"]


def test_ids_round_trip_and_reject_paths() -> None:
    job_id = state.new_id("bt")
    assert state.check_id(job_id, "bt", "job_id") == job_id
    for bad in ("../bt_20260101T000000Z_abcdef", "bt_x", job_id.replace("bt", "bl"), None, 5):
        with pytest.raises(BeamToolError):
            state.check_id(bad, "bt", "job_id")


def test_child_env_is_an_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_OTHER_API_KEY", "nope")
    monkeypatch.setenv("BEAM_API_KEY", "b1m_x")
    env = state.child_env(["BEAM_API_KEY", "NOT_SET_ANYWHERE"], EXTRA="1")
    assert env["BEAM_API_KEY"] == "b1m_x"
    assert env["EXTRA"] == "1"
    assert "SOME_OTHER_API_KEY" not in env
    assert "NOT_SET_ANYWHERE" not in env
    assert set(env) <= {*state.BASE_ENV, "BEAM_API_KEY", "EXTRA"}


def test_detached_process_lifecycle(tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    process = state.spawn_detached(
        [
            sys.executable,
            "-c",
            "import time; print('{\"ok\": true}', flush=True); time.sleep(30)",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=state.child_env(),
        stdout_path=out,
    )
    assert state.is_running(process.pid, [str(tmp_path)])
    assert state.terminate(process.pid, [str(tmp_path)]) is True
    assert not state.is_running(process.pid, [str(tmp_path)])
    assert state.terminate(process.pid, [str(tmp_path)]) is False
    assert out.stat().st_mode & 0o777 == 0o600


def test_foreign_process_is_never_signalled(tmp_path: Path) -> None:
    import subprocess

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert state.is_running(other.pid, ["/definitely/not/in/its/command/line"]) is False
        assert state.terminate(other.pid, ["/definitely/not/in/its/command/line"]) is False
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_private_dirs_are_owner_only(data_dir: Path) -> None:
    leaf = state.private_dir("rooms", "listeners", "bl_x")
    assert leaf == data_dir / "rooms" / "listeners" / "bl_x"
    for path in (data_dir / "rooms", data_dir / "rooms" / "listeners", leaf):
        assert path.stat().st_mode & 0o777 == 0o700
