"""Transfer worker: ``python -m beam_hermes.worker run|cancel``.

``beam_transfer_start`` runs ``run`` detached, in its own session, so the transfer's client
process outlives the Hermes session that started it. That process signs routes and answers
integrity checks until the transfer ends, so it must live until its final event.

Every stdout line is one JSON object with an ``event`` field; log records are written the
same way. Storage credentials are read from the environment and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from typing import Any, NoReturn

import httpx

from .common import API_URL, BeamToolError
from .endpoints import build_spec, estimated_credits

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_AUTH = 4
EXIT_INTERRUPTED = 130


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


def fail(kind: str, message: str, code: int, **details: Any) -> int:
    emit("error", kind=kind, message=message, **details)
    return code


class JsonLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = json.dumps(
                {
                    "event": "log",
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage()[:2000],
                }
            )
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 - logging must never break the transfer
            pass


def require_api_key() -> str:
    api_key = (os.environ.get("BEAM_API_KEY") or "").strip()
    if not api_key:
        raise BeamToolError("missing_env", "BEAM_API_KEY is not set.", variable="BEAM_API_KEY")
    return api_key


def beam_client(api_key: str) -> Any:
    from beam_network_sdk import BEAM_PROD_URL, BeamSDK

    return BeamSDK(api_key=api_key, nats_url=BEAM_PROD_URL, environment="prod")


async def report_progress(api_key: str, transfer_id: str, interval: float) -> None:
    last: tuple[Any, Any] | None = None
    headers = {"X-Api-Key": api_key}
    async with httpx.AsyncClient(base_url=API_URL, timeout=20.0) as client:
        while True:
            await asyncio.sleep(interval)
            try:
                response = await client.get(f"/transfers/{transfer_id}", headers=headers)
            except httpx.HTTPError as exc:
                logging.getLogger("beam_hermes.worker").warning(
                    "progress check failed: %s", type(exc).__name__
                )
                continue
            if response.status_code != 200:
                continue
            transfer = response.json()
            current = (transfer.get("status"), transfer.get("bytes_transferred"))
            if current == last:
                continue
            last = current
            total = transfer.get("total_bytes") or 0
            done = transfer.get("bytes_transferred") or 0
            emit(
                "progress",
                transfer_id=transfer_id,
                status=transfer.get("status"),
                bytes_transferred=done,
                total_bytes=total,
                percent=round(100 * done / total, 1) if total else None,
            )


async def run(args: argparse.Namespace) -> int:
    from beam_network_sdk import (
        BeamAPIError,
        BeamProviderTransferError,
        BeamRouteRecoveryPendingError,
        BeamStorageAccessError,
        BeamTimeoutError,
        BeamTransferFailedError,
    )

    spec = build_spec(
        args.source,
        args.dest,
        {
            "commit_message": args.hf_commit_message,
            "create_pr": args.hf_create_pr,
            "allow_source_rehash": args.hf_allow_source_rehash,
        },
    )
    api_key = require_api_key()
    emit("started", pid=os.getpid(), name=args.name, timeout_seconds=args.timeout, **spec.describe())
    transfer_id = ""
    async with beam_client(api_key) as beam:
        try:
            try:
                prepared = await beam.transfers.create_transfer(
                    sources=[spec.source_model],
                    destinations=spec.destination_models,
                    name=args.name,
                )
            except BeamRouteRecoveryPendingError as exc:
                transfer_id = exc.transfer_id
                emit("created", transfer_id=transfer_id, recovering=True, **spec.describe())
            else:
                if not prepared.success:
                    return fail(
                        "create_rejected",
                        prepared.error or prepared.message or "Beam rejected the transfer.",
                        EXIT_FAILED,
                    )
                transfer_id = prepared.transfer_id
                delivered = prepared.total_size * max(prepared.total_destinations, 1)
                emit(
                    "created",
                    transfer_id=transfer_id,
                    **spec.describe(),
                    size_bytes=prepared.total_size,
                    chunks=prepared.logical_chunks,
                    delivery_tasks=prepared.total_chunks,
                    estimated_credits=estimated_credits(delivered),
                )

            progress = asyncio.create_task(report_progress(api_key, transfer_id, args.progress_interval))
            try:
                status = await beam.transfers.wait_complete(transfer_id, timeout=args.timeout)
            finally:
                progress.cancel()
            emit(
                "completed",
                transfer_id=transfer_id,
                size_bytes=status.source_bytes_total,
                delivered_bytes=status.delivery_bytes_completed,
                destinations_completed=status.destinations_completed,
                started_at=status.started_at,
                completed_at=status.completed_at,
                integrity_check_warning=status.integrity_check_warning,
            )
            return EXIT_OK
        except BeamStorageAccessError as exc:
            emit(
                "failed",
                transfer_id=transfer_id,
                code=exc.code,
                error_message=exc.error_message,
                hint="Grant the key read (source) or write and multipart (destination) access, "
                "with no IP or network restriction.",
            )
            return EXIT_FAILED
        except BeamTransferFailedError as exc:
            emit("failed", transfer_id=transfer_id, error_message=exc.error_message)
            return EXIT_FAILED
        except BeamProviderTransferError as exc:
            emit(
                "failed",
                transfer_id=exc.transfer_id,
                error_message=str(exc),
                transfer_cancelled=exc.transfer_cancelled,
            )
            return EXIT_FAILED
        except BeamTimeoutError as exc:
            emit(
                "timeout",
                transfer_id=transfer_id,
                message=str(exc),
                hint="The transfer may still finish; check it with beam_transfer_status. This "
                "worker stopped re-signing its routes, so restart long transfers with a larger "
                "timeout_seconds.",
            )
            return EXIT_TIMEOUT
        except BeamAPIError as exc:
            if exc.status_code == 409 and transfer_id:
                emit("cancelled", transfer_id=transfer_id)
                return EXIT_FAILED
            if exc.status_code in (401, 403):
                return fail("auth", exc.detail, EXIT_AUTH, status_code=exc.status_code)
            kind = "insufficient_credits" if exc.status_code == 402 else "api_error"
            return fail(kind, exc.detail, EXIT_FAILED, status_code=exc.status_code)
        except asyncio.CancelledError:
            emit(
                "interrupted",
                transfer_id=transfer_id or None,
                hint="The worker was stopped but the transfer was not cancelled. Check it with "
                "beam_transfer_status, or stop it with beam_transfer_cancel.",
            )
            return EXIT_INTERRUPTED


async def cancel(args: argparse.Namespace) -> int:
    async with beam_client(require_api_key()) as beam:
        result = await beam.transfers.cancel(args.transfer_id)
    emit("cancel", transfer_id=args.transfer_id, success=result.success, message=result.message)
    return EXIT_OK if result.success else EXIT_FAILED


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return seconds


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        emit("error", kind="usage", message=message)
        raise SystemExit(EXIT_USAGE)


def parser() -> argparse.ArgumentParser:
    root = JsonArgumentParser(prog="python -m beam_hermes.worker")
    commands = root.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    run_parser = commands.add_parser("run")
    run_parser.add_argument(
        "--job-dir", required=True, help="Job directory; identifies this worker's process"
    )
    run_parser.add_argument("--source", required=True)
    run_parser.add_argument("--dest", action="append", required=True)
    run_parser.add_argument("--name")
    run_parser.add_argument("--timeout", type=positive_seconds, default=21600.0)
    run_parser.add_argument("--progress-interval", type=positive_seconds, default=30.0)
    run_parser.add_argument("--hf-commit-message")
    run_parser.add_argument("--hf-create-pr", action="store_true")
    run_parser.add_argument("--hf-allow-source-rehash", action="store_true")

    cancel_parser = commands.add_parser("cancel")
    cancel_parser.add_argument("transfer_id")
    return root


async def dispatch(args: argparse.Namespace) -> int:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signum, task.cancel)
    if args.command == "run":
        return await run(args)
    return await cancel(args)


def is_auth_error(exc: Exception) -> bool:
    try:
        from beam_network_sdk import BeamAuthError
    except ImportError:
        return False
    return isinstance(exc, BeamAuthError)


def main(argv: list[str] | None = None) -> int:
    handler = JsonLogHandler()
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    args = parser().parse_args(argv)
    try:
        return asyncio.run(dispatch(args))
    except BeamToolError as exc:
        return fail(exc.kind, str(exc), EXIT_USAGE, **exc.details)
    except (KeyboardInterrupt, asyncio.CancelledError):
        emit("interrupted", hint="Check the transfer with beam_transfer_status.")
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - every failure must end as a JSON event
        if is_auth_error(exc):
            return fail("auth", str(exc), EXIT_AUTH)
        return fail("unexpected", f"{type(exc).__name__}: {exc}", EXIT_FAILED)


if __name__ == "__main__":
    sys.exit(main())
