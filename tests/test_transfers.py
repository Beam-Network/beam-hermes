from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import beam_network_sdk
import pytest

from beam_hermes import api, state, transfers, worker
from beam_hermes.endpoints import estimated_credits


def call(handler, args: dict) -> dict:
    result = handler(args)
    assert isinstance(result, str)
    return json.loads(result)


@pytest.mark.parametrize(
    ("delivered", "credits"),
    [(0, 0.0), (1, 0.01), (10**9, 0.01), (10**9 + 1, 0.02), (3 * 10**12, 30.0), (2_500_000_000, 0.03)],
)
def test_estimated_credits_round_up_per_gb(delivered: int, credits: float) -> None:
    assert estimated_credits(delivered) == credits


def test_estimate_multiplies_by_destinations(monkeypatch: pytest.MonkeyPatch, r2_env: dict[str, str]) -> None:
    monkeypatch.setattr(
        beam_network_sdk,
        "prepare_provider_source_for_plan",
        lambda source: SimpleNamespace(size=2_500_000_000),
    )
    result = call(
        transfers.estimate,
        {"source": "r2://src/data.tar", "destinations": ["r2://a/data.tar", "r2://b/data.tar"]},
    )
    assert result["size_bytes"] == 2_500_000_000
    assert result["delivered_bytes"] == 5_000_000_000
    assert result["estimated_credits"] == 0.05
    assert result["estimated_usd"] == 0.025
    assert result["source"] == {
        "uri": "r2://src/data.tar",
        "env": ["R2_ACCESS_KEY_ID", "R2_ACCOUNT_ID", "R2_SECRET_ACCESS_KEY"],
    }
    assert "test-secret-value-not-real" not in json.dumps(result)


def test_estimate_reports_unreadable_source(monkeypatch: pytest.MonkeyPatch, r2_env: dict[str, str]) -> None:
    def refuse(source: object) -> None:
        raise PermissionError("403 Forbidden")

    monkeypatch.setattr(beam_network_sdk, "prepare_provider_source_for_plan", refuse)
    result = call(transfers.estimate, {"source": "r2://src/a.bin", "destinations": ["r2://dst/a.bin"]})
    assert result["kind"] == "source_unreadable"
    assert "403 Forbidden" in result["error"]


def test_estimate_names_missing_credentials() -> None:
    result = call(
        transfers.estimate, {"source": "r2://src/a.bin?env=fake", "destinations": ["r2://dst/a.bin?env=fake"]}
    )
    assert result == {
        "error": "FAKE_R2_ACCESS_KEY_ID is not set; it is required for r2:// endpoints.",
        "kind": "missing_env",
        "variable": "FAKE_R2_ACCESS_KEY_ID",
        "endpoint": "r2://src/a.bin",
    }


def test_start_requires_confirmation(data_dir: Path, r2_env: dict[str, str]) -> None:
    result = call(transfers.start, {"source": "r2://src/a.bin", "destinations": ["r2://dst/a.bin"]})
    assert result["kind"] == "confirmation_required"
    assert not (data_dir / "transfers").exists()


def test_start_requires_api_key(data_dir: Path, r2_env: dict[str, str]) -> None:
    result = call(
        transfers.start, {"source": "r2://src/a.bin", "destinations": ["r2://dst/a.bin"], "confirmed": True}
    )
    assert result["kind"] == "missing_env"
    assert result["variable"] == "BEAM_API_KEY"


FAKE_WORKER = (
    "import json, time\n"
    "print(json.dumps({'event': 'started'}), flush=True)\n"
    "print(json.dumps({'event': 'created', 'transfer_id': 'tr_test_123', 'estimated_credits': 0.01}), flush=True)\n"
    "time.sleep(30)\n"
)


def test_start_spawns_a_detached_worker_with_minimal_env(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, r2_env: dict[str, str]
) -> None:
    monkeypatch.setenv("BEAM_API_KEY", "b1m_test_not_a_real_key")
    monkeypatch.setenv("UNRELATED_SERVICE_TOKEN", "must-not-leak")
    captured: dict = {}
    original = state.spawn_detached

    def fake_spawn(argv, *, cwd, env, stdout_path, stderr_path=None):
        captured.update(argv=argv, env=env, cwd=cwd)
        return original([sys.executable, "-c", FAKE_WORKER], cwd=cwd, env=env, stdout_path=stdout_path)

    monkeypatch.setattr(state, "spawn_detached", fake_spawn)
    result = call(
        transfers.start,
        {
            "source": "r2://src/a.bin",
            "destinations": ["r2://dst/a.bin", "r2://dst2/"],
            "name": "-starts-with-dash",
            "confirmed": True,
            "wait_seconds": 20,
        },
    )
    try:
        assert result["transfer_id"] == "tr_test_123"
        assert result["phase"] == "running"
        assert result["worker_running"] is True
        job_id = result["job_id"]
        job_dir = data_dir / "transfers" / job_id
        assert captured["cwd"] == job_dir
        argv = captured["argv"]
        assert argv[1:4] == ["-m", "beam_hermes.worker", "run"]
        assert f"--job-dir={job_dir}" in argv
        assert "--source=r2://src/a.bin" in argv
        assert [a for a in argv if a.startswith("--dest=")] == ["--dest=r2://dst/a.bin", "--dest=r2://dst2/"]
        assert "--name=-starts-with-dash" in argv
        env = captured["env"]
        assert env["BEAM_API_KEY"] == "b1m_test_not_a_real_key"
        assert set(r2_env) <= set(env)
        assert "UNRELATED_SERVICE_TOKEN" not in env
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(state.PACKAGE_ROOT)
        job = json.loads((job_dir / "job.json").read_text())
        assert job["pid"] > 0
        assert oct((job_dir / "events.jsonl").stat().st_mode & 0o777) == "0o600"

        monkeypatch.setattr(api, "transfer", lambda transfer_id: {"id": transfer_id, "status": "pending"})
        status = call(transfers.status, {"job_id": job_id, "transfer_id": None})
        assert status["job"]["phase"] == "running"
        assert status["transfer"] == {"id": "tr_test_123", "status": "pending"}
        listed = transfers.local_jobs(5)
        assert listed[0]["job_id"] == job_id
    finally:
        job = json.loads((data_dir / "transfers" / result["job_id"] / "job.json").read_text())
        state.terminate(job["pid"], [])


def test_job_summary_phases(data_dir: Path) -> None:
    job_dir = data_dir / "transfers" / "bt_20260101T000000Z_abcdef"
    job_dir.mkdir(parents=True)
    state.write_json(job_dir / "job.json", {"pid": 999_999_999, "source": "r2://a/b"})
    events = job_dir / "events.jsonl"
    events.write_text('{"event": "started"}\nTraceback (most recent call last):\n')
    summary = transfers.job_summary(job_dir)
    assert summary["phase"] == "worker_exited"
    assert summary["worker_log"] == [{"event": "log", "line": "Traceback (most recent call last):"}]

    with events.open("a") as handle:
        handle.write('{"event": "created", "transfer_id": "tr_1"}\n')
        handle.write('{"event": "progress", "transfer_id": "tr_1", "percent": 50.0}\n')
        handle.write('{"event": "completed", "transfer_id": "tr_1"}\n')
    summary = transfers.job_summary(job_dir)
    assert summary["phase"] == "completed"
    assert summary["transfer_id"] == "tr_1"
    assert summary["last_event"]["event"] == "completed"


def test_status_reads_beam_for_a_job(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    job_dir = data_dir / "transfers" / "bt_20260101T000000Z_000001"
    job_dir.mkdir(parents=True)
    state.write_json(job_dir / "job.json", {"pid": 999_999_999})
    (job_dir / "events.jsonl").write_text('{"event": "created", "transfer_id": "tr_9"}\n')
    monkeypatch.setattr(api, "transfer", lambda transfer_id: {"id": transfer_id, "status": "in_progress"})
    result = call(transfers.status, {"job_id": "bt_20260101T000000Z_000001"})
    assert result["transfer"] == {"id": "tr_9", "status": "in_progress"}
    assert result["job"]["phase"] == "worker_exited"


@pytest.mark.parametrize(
    "args",
    [{}, {"job_id": "../etc"}, {"job_id": "bl_20260101T000000Z_000001"}, {"transfer_id": "-rf"}],
)
def test_status_validates_ids(data_dir: Path, args: dict) -> None:
    assert call(transfers.status, args)["kind"] == "invalid_argument"


def test_list_validates_filters(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    seen: dict = {}
    monkeypatch.setattr(api, "transfers", lambda params: seen.update(params) or {"transfers": [], "total": 0})
    assert call(transfers.list_transfers, {"status": "done"})["kind"] == "invalid_argument"
    result = call(transfers.list_transfers, {"status": "failed", "limit": 5, "order_by": "completed_at"})
    assert seen == {"limit": 5, "offset": 0, "status": "failed", "order_by": "completed_at"}
    assert result["local_jobs"] == []


def test_cancel_needs_an_id(data_dir: Path) -> None:
    assert call(transfers.cancel, {})["kind"] == "invalid_argument"


def test_account_without_key_is_a_clean_error() -> None:
    result = call(transfers.account, {})
    assert result["kind"] == "missing_env"
    assert result["variable"] == "BEAM_API_KEY"


def run_worker(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, list[dict]]:
    code = worker.main(argv)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return code, lines


def test_worker_reports_bad_uri_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    code, events = run_worker(["run", "--job-dir", "x", "--source", "gs://a/b", "--dest", "s3://c/d"], capsys)
    assert code == worker.EXIT_USAGE
    assert events[-1]["event"] == "error"
    assert events[-1]["kind"] == "unsupported_scheme"


def test_worker_requires_api_key(capsys: pytest.CaptureFixture[str], r2_env: dict[str, str]) -> None:
    code, events = run_worker(["run", "--job-dir", "x", "--source=r2://a/b", "--dest=r2://c/d"], capsys)
    assert code == worker.EXIT_USAGE
    assert events[-1] == {
        "event": "error",
        "kind": "missing_env",
        "message": "BEAM_API_KEY is not set.",
        "variable": "BEAM_API_KEY",
    }


def test_worker_usage_errors_are_json(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        worker.main(["run", "--source", "r2://a/b"])
    assert caught.value.code == worker.EXIT_USAGE
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["kind"] == "usage"


def test_worker_starts_as_a_module_with_the_worker_env(tmp_path: Path) -> None:
    """The detached worker runs from its job directory with only the env the plugin builds."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "beam_hermes.worker",
            "run",
            "--job-dir=x",
            "--source=gs://a/b",
            "--dest=s3://c/d",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=transfers.worker_env([]),
        timeout=60,
        check=False,
    )
    assert completed.returncode == worker.EXIT_USAGE, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1])["kind"] == "unsupported_scheme"


API_TRANSFER = {
    "id": "tr_1",
    "transfer_key": "tk_1",
    "status": "in_progress",
    "total_bytes": 4_000_000_000,
    "bytes_transferred": 1_000_000_000,
    "error_message": None,
    "metadata": {"performance": {"x": "y" * 1000}, "logical_chunk_count": 40, "delivery_task_count": 80},
    "created_at": "2026-10-09T00:00:00Z",
    "started_at": "2026-10-09T00:00:01Z",
    "completed_at": None,
}


def test_transfer_view_drops_internal_metadata() -> None:
    assert transfers.transfer_view(API_TRANSFER) == {
        "id": "tr_1",
        "status": "in_progress",
        "total_bytes": 4_000_000_000,
        "bytes_transferred": 1_000_000_000,
        "error_message": None,
        "created_at": "2026-10-09T00:00:00Z",
        "started_at": "2026-10-09T00:00:01Z",
        "completed_at": None,
        "percent": 25.0,
        "logical_chunk_count": 40,
        "delivery_task_count": 80,
    }
    assert (
        transfers.transfer_view(API_TRANSFER, include_metadata=True)["metadata"] == API_TRANSFER["metadata"]
    )


def test_list_and_status_return_compact_transfers(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setattr(
        api, "transfers", lambda params: {"transfers": [API_TRANSFER], "limit": 1, "offset": 0}
    )
    monkeypatch.setattr(api, "transfer", lambda transfer_id: API_TRANSFER)
    listed = call(transfers.list_transfers, {"limit": 1})
    assert "metadata" not in listed["transfers"][0]
    assert listed["limit"] == 1
    status = call(transfers.status, {"transfer_id": "tr_1"})
    assert "metadata" not in status["transfer"]
    assert "metadata" in call(transfers.status, {"transfer_id": "tr_1", "include_metadata": True})["transfer"]
