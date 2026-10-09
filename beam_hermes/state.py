"""Plugin data files and the detached processes the tools start.

Everything lives under the per-plugin data directory Hermes provides
(``<hermes home>/plugin-data/beam/``), never in the plugin's install tree.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .common import PLUGIN_NAME, BeamToolError

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Non-secret variables a child process needs to run, resolve hosts and reach the network.
BASE_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
)

_ID_PATTERN = re.compile(r"^(?P<prefix>[a-z]{2})_\d{8}T\d{6}Z_[0-9a-f]{6}$")

_children: dict[int, subprocess.Popen[bytes]] = {}
_children_lock = threading.Lock()


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def private_dir(*parts: str) -> Path:
    """A directory under the plugin data directory; each level it creates is owner-only."""
    path = data_dir()
    for part in parts:
        path = path / part
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def new_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{secrets.token_hex(3)}"


def check_id(value: Any, prefix: str, argument: str) -> str:
    match = _ID_PATTERN.match(value) if isinstance(value, str) else None
    if not match or match.group("prefix") != prefix:
        raise BeamToolError(
            "invalid_argument",
            f"`{argument}` must be an ID returned by this plugin, such as {prefix}_20260101T000000Z_a1b2c3.",
            argument=argument,
        )
    return value


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, readable only by this user."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_json(path: Path, data: dict[str, Any]) -> None:
    write_private(path, json.dumps(data, indent=2, default=str) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_records(
    path: Path, offset: int = 0, *, limit: int | None = None, max_bytes: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Read complete JSONL lines from byte ``offset``; return them and the next offset.

    A line that is not a JSON object becomes ``{"event": "log", "line": ...}``. A trailing
    line without a newline is still being written and is left for the next read.
    """
    records: list[dict[str, Any]] = []
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return records, offset
    with handle:
        handle.seek(offset)
        consumed = 0
        while limit is None or len(records) < limit:
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                break
            if max_bytes is not None and records and consumed + len(line) > max_bytes:
                break
            consumed += len(line)
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                record = None
            if not isinstance(record, dict):
                record = {"event": "log", "line": text[:2000]}
            records.append(record)
    return records, offset + consumed


def tail_text(path: Path, max_bytes: int = 2000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def child_env(names: list[str] | tuple[str, ...] = (), **extra: str) -> dict[str, str]:
    """Build a child environment from an allowlist: the base variables plus ``names``."""
    env: dict[str, str] = {}
    for name in (*BASE_ENV, *names):
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update(extra)
    return env


def spawn_detached(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path | None = None,
) -> subprocess.Popen[bytes]:
    """Start ``argv`` in its own session, appending its output to owner-only files."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    stdout_fd = os.open(stdout_path, flags, 0o600)
    stderr_fd = os.open(stderr_path, flags, 0o600) if stderr_path else None
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_fd,
            stderr=stderr_fd if stderr_fd is not None else subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(stdout_fd)
        if stderr_fd is not None:
            os.close(stderr_fd)
    with _children_lock:
        _children[process.pid] = process
    return process


def _command_line(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = result.stdout.strip()
    return line if result.returncode == 0 and line else None


def is_running(pid: Any, markers: list[str]) -> bool:
    """Whether ``pid`` is still the process this plugin started (its command line has ``markers``)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    with _children_lock:
        child = _children.get(pid)
    if child is not None:
        if child.poll() is None:
            return True
        with _children_lock:
            _children.pop(pid, None)
        return False
    command = _command_line(pid)
    return command is not None and all(marker in command for marker in markers)


def terminate(pid: Any, markers: list[str], grace_seconds: float = 5.0) -> bool:
    """Stop the process group of a process this plugin started. Returns False if it was gone."""
    if not is_running(pid, markers):
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not is_running(pid, markers):
                return True
            time.sleep(0.2)
    return True
