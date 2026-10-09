"""Transfer tools: account check, estimate, start, status, list and cancel."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import api, state
from .common import (
    CONSOLE_URL,
    USD_PER_CREDIT,
    BeamToolError,
    bool_arg,
    int_arg,
    list_arg,
    text_arg,
    tool,
)

TRANSFERS_DIR = "transfers"
JOB_PREFIX = "bt"
TRANSFER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
LIST_STATUSES = ("pending", "in_progress", "completed", "failed", "cancelled")
ORDER_BY = ("created_at", "completed_at")
FINAL_EVENTS = frozenset({"completed", "failed", "cancelled", "timeout", "error", "interrupted"})
TRANSFER_FIELDS = (
    "id",
    "status",
    "total_bytes",
    "bytes_transferred",
    "error_message",
    "created_at",
    "started_at",
    "completed_at",
)
METADATA_FIELDS = ("logical_chunk_count", "delivery_task_count")
ESTIMATE_NOTE = (
    "Estimate only: 0.01 credit per GB delivered to each destination (1 credit = $0.50). "
    "Existing objects at the destination keys are overwritten. The Console shows the actual charge."
)


def worker_python_path() -> str:
    """This package plus the site-packages directories Hermes imports from.

    Hermes may run a bare interpreter that reaches its dependencies (and this plugin's) only
    through ``sys.path``; the worker needs the same third-party packages.
    """
    site_dirs = [
        entry
        for entry in sys.path
        if entry and Path(entry).name in ("site-packages", "dist-packages") and Path(entry).is_dir()
    ]
    return os.pathsep.join(dict.fromkeys([str(state.PACKAGE_ROOT), *site_dirs]))


def worker_env(names: list[str]) -> dict[str, str]:
    return state.child_env(
        names,
        PYTHONPATH=worker_python_path(),
        PYTHONNOUSERSITE="1",
        PYTHONUNBUFFERED="1",
        PYTHONDONTWRITEBYTECODE="1",
    )


def transfer_id_arg(args: dict[str, Any], *, required: bool = False) -> str | None:
    value = text_arg(args, "transfer_id", required=required)
    if value is not None and not TRANSFER_ID.match(value):
        raise BeamToolError(
            "invalid_argument", "`transfer_id` is not a Beam transfer ID.", argument="transfer_id"
        )
    return value


def hf_options(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "commit_message": text_arg(args, "hf_commit_message"),
        "create_pr": bool_arg(args, "hf_create_pr"),
        "allow_source_rehash": bool_arg(args, "hf_allow_source_rehash"),
    }


def transfer_view(transfer: Any, *, include_metadata: bool = False) -> Any:
    """The fields of a Beam transfer object an agent needs; its internal metadata is large."""
    if not isinstance(transfer, dict):
        return transfer
    view = {key: transfer[key] for key in TRANSFER_FIELDS if key in transfer}
    total, done = transfer.get("total_bytes"), transfer.get("bytes_transferred")
    if isinstance(total, int) and total > 0 and isinstance(done, int):
        view["percent"] = round(100 * done / total, 1)
    metadata = transfer.get("metadata")
    if isinstance(metadata, dict):
        if include_metadata:
            view["metadata"] = metadata
        else:
            view.update({key: metadata[key] for key in METADATA_FIELDS if key in metadata})
    return view


def job_dir(job_id: str) -> Path:
    state.check_id(job_id, JOB_PREFIX, "job_id")
    path = state.data_dir() / TRANSFERS_DIR / job_id
    if not path.is_dir():
        raise BeamToolError("not_found", f"No transfer job {job_id} on this machine.", job_id=job_id)
    return path


def job_summary(path: Path) -> dict[str, Any]:
    job = state.read_json(path / "job.json")
    events, _ = state.read_records(path / "events.jsonl")
    created = next((e for e in events if e.get("event") == "created"), None)
    final = next((e for e in reversed(events) if e.get("event") in FINAL_EVENTS), None)
    last = next((e for e in reversed(events) if e.get("event") != "log"), None)
    running = state.is_running(job.get("pid"), [str(path)])
    if final is not None:
        phase = final["event"]
    elif running:
        phase = "running" if created else "starting"
    else:
        phase = "worker_exited"
    summary: dict[str, Any] = {
        "job_id": path.name,
        "transfer_id": (created or final or {}).get("transfer_id") or None,
        "phase": phase,
        "worker_running": running,
        "name": job.get("name"),
        "source": job.get("source"),
        "destinations": job.get("destinations"),
        "started_at": job.get("started_at"),
        "last_event": last,
    }
    if phase == "worker_exited":
        summary["worker_log"] = [e for e in events if e.get("event") == "log"][-5:]
        summary["hint"] = (
            "The worker stopped without a final event. Check the transfer with its transfer_id; "
            "it was not cancelled."
        )
    return summary


def wait_for_created(path: Path, wait_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    while True:
        summary = job_summary(path)
        if summary["phase"] != "starting" or time.monotonic() >= deadline:
            return summary
        time.sleep(0.5)


def local_jobs(limit: int) -> list[dict[str, Any]]:
    root = state.data_dir() / TRANSFERS_DIR
    if not root.is_dir():
        return []
    jobs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    keys = ("job_id", "transfer_id", "phase", "worker_running", "name", "started_at")
    return [{k: summary[k] for k in keys} for summary in map(job_summary, jobs[:limit])]


@tool
def account(args: dict[str, Any]) -> dict[str, Any]:
    return {**api.whoami(), "console": CONSOLE_URL}


@tool
def estimate(args: dict[str, Any]) -> dict[str, Any]:
    from . import endpoints

    spec = endpoints.build_spec(
        text_arg(args, "source", required=True),
        list_arg(args, "destinations", required=True),
        hf_options(args),
    )
    from beam_network_sdk import prepare_provider_source_for_plan

    try:
        planned = prepare_provider_source_for_plan(spec.source_model)
    except Exception as exc:  # noqa: BLE001 - any storage failure is reported, not raised
        raise BeamToolError(
            "source_unreadable",
            f"Could not read the source object's size: {type(exc).__name__}: {exc}",
            source=spec.source.uri,
            hint="Check the URI, the credentials named in `env`, and the endpoint.",
        ) from None
    delivered = planned.size * len(spec.destinations)
    credits = endpoints.estimated_credits(delivered)
    return {
        **spec.describe(),
        "size_bytes": planned.size,
        "delivered_bytes": delivered,
        "estimated_credits": credits,
        "estimated_usd": round(credits * USD_PER_CREDIT, 3),
        "note": ESTIMATE_NOTE,
    }


@tool
def start(args: dict[str, Any]) -> dict[str, Any]:
    if not bool_arg(args, "confirmed"):
        raise BeamToolError(
            "confirmation_required",
            "Run beam_transfer_estimate first and show the user the source, every destination "
            "(existing objects there are overwritten) and the estimated credits. Call again "
            "with confirmed=true only after the user approves.",
        )
    from . import endpoints

    source = text_arg(args, "source", required=True)
    destinations = list_arg(args, "destinations", required=True)
    name = text_arg(args, "name")
    timeout = int_arg(args, "timeout_seconds", 21600, minimum=60, maximum=7 * 86400)
    wait_seconds = int_arg(args, "wait_seconds", 60, minimum=0, maximum=300)
    options = hf_options(args)
    spec = endpoints.build_spec(source, destinations, options)
    api.api_key()

    job_id = state.new_id(JOB_PREFIX)
    path = state.private_dir(TRANSFERS_DIR, job_id)
    argv = [
        sys.executable,
        "-m",
        "beam_hermes.worker",
        "run",
        f"--job-dir={path}",
        f"--source={source}",
        *(f"--dest={destination}" for destination in destinations),
        f"--timeout={timeout}",
    ]
    if name:
        argv.append(f"--name={name}")
    if options["commit_message"]:
        argv.append(f"--hf-commit-message={options['commit_message']}")
    if options["create_pr"]:
        argv.append("--hf-create-pr")
    if options["allow_source_rehash"]:
        argv.append("--hf-allow-source-rehash")

    events_path = path / "events.jsonl"
    process = state.spawn_detached(
        argv,
        cwd=path,
        env=worker_env(["BEAM_API_KEY", *spec.credential_variables()]),
        stdout_path=events_path,
    )
    state.write_json(
        path / "job.json",
        {
            "job_id": job_id,
            "pid": process.pid,
            "started_at": state.now(),
            "name": name,
            "source": spec.source.location,
            "destinations": [d.location for d in spec.destinations],
            "timeout_seconds": timeout,
        },
    )
    summary = wait_for_created(path, wait_seconds)
    summary["events_file"] = str(events_path)
    if summary["phase"] in FINAL_EVENTS - {"completed"} or summary["phase"] == "worker_exited":
        last = summary.get("last_event") or {}
        summary["error"] = last.get("message") or last.get("error_message") or "The transfer did not start."
        summary["kind"] = last.get("kind", summary["phase"])
    elif summary["phase"] == "starting":
        summary["hint"] = "Still creating the transfer. Poll beam_transfer_status with this job_id."
    else:
        summary["hint"] = (
            "Report the transfer_id to the user. The worker keeps running in the background "
            "until the transfer ends; poll beam_transfer_status with the job_id."
        )
    return summary


@tool
def status(args: dict[str, Any]) -> dict[str, Any]:
    job_id = text_arg(args, "job_id")
    transfer_id = transfer_id_arg(args)
    if not job_id and not transfer_id:
        raise BeamToolError("invalid_argument", "Give a job_id or a transfer_id.")
    result: dict[str, Any] = {}
    if job_id:
        result["job"] = job_summary(job_dir(job_id))
        transfer_id = transfer_id or result["job"]["transfer_id"]
    if transfer_id:
        try:
            result["transfer"] = transfer_view(
                api.transfer(transfer_id), include_metadata=bool_arg(args, "include_metadata")
            )
        except BeamToolError as exc:
            if not job_id:
                raise
            result["transfer_error"] = exc.payload()
    return result


@tool
def list_transfers(args: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "limit": int_arg(args, "limit", 20, minimum=1, maximum=100),
        "offset": int_arg(args, "offset", 0, minimum=0, maximum=1_000_000),
    }
    for name, allowed in (("status", LIST_STATUSES), ("order_by", ORDER_BY)):
        value = text_arg(args, name)
        if value is not None:
            if value not in allowed:
                raise BeamToolError(
                    "invalid_argument", f"`{name}` must be one of {', '.join(allowed)}.", argument=name
                )
            params[name] = value
    completed_after = text_arg(args, "completed_after")
    if completed_after:
        params["completed_after"] = completed_after
    data = api.transfers(params)
    result = data if isinstance(data, dict) else {"transfers": data}
    if isinstance(result.get("transfers"), list):
        result["transfers"] = [transfer_view(transfer) for transfer in result["transfers"]]
    if bool_arg(args, "include_local_jobs", True):
        result["local_jobs"] = local_jobs(10)
    return result


@tool
def cancel(args: dict[str, Any]) -> dict[str, Any]:
    job_id = text_arg(args, "job_id")
    transfer_id = transfer_id_arg(args)
    if job_id and not transfer_id:
        transfer_id = job_summary(job_dir(job_id))["transfer_id"]
        if not transfer_id:
            raise BeamToolError(
                "not_started", "That job has no transfer_id yet; check beam_transfer_status.", job_id=job_id
            )
    if not transfer_id:
        raise BeamToolError("invalid_argument", "Give a transfer_id or a job_id.")
    api.api_key()
    argv = [sys.executable, "-m", "beam_hermes.worker", "cancel", transfer_id]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
            env=worker_env(["BEAM_API_KEY"]),
            cwd=state.private_dir(TRANSFERS_DIR),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise BeamToolError(
            "timeout", "Beam did not answer the cancel request in 120 seconds; check beam_transfer_status."
        ) from None
    events = []
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    final = next((e for e in reversed(events) if e.get("event") in ("cancel", "error")), None)
    if final is None:
        raise BeamToolError(
            "unexpected",
            "The cancel helper returned no result.",
            exit_code=completed.returncode,
            stderr_tail=completed.stderr[-500:],
        )
    if final["event"] == "error":
        details = {k: v for k, v in final.items() if k not in ("event", "kind", "message")}
        raise BeamToolError(final.get("kind", "api_error"), final.get("message", ""), **details)
    return {
        "transfer_id": transfer_id,
        "job_id": job_id,
        "success": final.get("success"),
        "message": final.get("message"),
    }
