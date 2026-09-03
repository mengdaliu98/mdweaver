"""Starting and stopping the background server.

`mdweave start` has to outlive the shell that ran it, notice when a server is
already up, and notice when the one that is up is older than the code on disk.
That bookkeeping lives here so cli.py stays an argument parser.

This replaces the old mdweave_render.sh, which did the same things with curl,
nohup and pkill.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

WAIT_FOR_START = 20.0  # seconds; a cold start renders every document first
WAIT_FOR_STOP = 5.0


def _runtime_dir() -> Path:
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def pidfile(port: int) -> Path:
    return _runtime_dir() / f"mdweave-{port}.pid"


def logfile(port: int) -> Path:
    return _runtime_dir() / f"mdweave-{port}.log"


# --- what is running -------------------------------------------------------

def health(host: str, port: int, timeout: float = 1.0) -> dict | None:
    """What the server on this port reports, or None if nothing answers."""
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/health", timeout=timeout
        ) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def is_stale(info: dict) -> bool:
    """True when the running server imported code older than what is on disk.

    A long-lived process keeps serving whatever it imported at startup, so an
    edit to the source stays invisible until it restarts. A server too old to
    report a fingerprint at all counts as stale.
    """
    from .serve import source_fingerprint

    running = info.get("fingerprint")
    return not running or running != source_fingerprint()


# --- starting --------------------------------------------------------------

def spawn(inputs: Path, outputs: Path, host: str, port: int) -> subprocess.Popen:
    """Launch a detached server.

    `start_new_session` puts it in its own process group, so closing the
    terminal or Ctrl-C in the shell that ran `mdweave start` leaves it running.
    The build has already happened in the foreground, hence --no-build.
    """
    command = [
        sys.executable, "-m", "mdweave", "serve",
        "-i", str(inputs),
        "-o", str(outputs),
        "--host", host,
        "--port", str(port),
        "--no-build",
    ]
    with open(logfile(port), "wb") as sink:
        process = subprocess.Popen(
            command,
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    pidfile(port).write_text(str(process.pid), encoding="utf-8")
    return process


def wait_until_up(
    process: subprocess.Popen, host: str, port: int, timeout: float = WAIT_FOR_START
) -> dict | None:
    """Poll health until the server answers, or it dies, or we run out of time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = health(host, port)
        if info:
            return info
        if process.poll() is not None:  # died on the way up; no point waiting
            return None
        time.sleep(0.15)
    return None


# --- stopping --------------------------------------------------------------

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _recorded_pid(port: int) -> int | None:
    try:
        pid = int(pidfile(port).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def _reported_pid(host: str, port: int) -> int | None:
    """Ask the server on this port what its pid is.

    This is the authoritative answer and the one to prefer: anything that
    answers /api/health as mdweave *is* the server, so there is no guessing.
    It also covers a server whose pidfile was cleared out of /tmp, and one
    started by hand with `mdweave serve`.

    Matching against `ps` output was the obvious alternative and is a trap --
    the command line of the very shell running `mdweave stop` mentions
    "mdweave", "serve" and the port, so a substring match happily kills the
    caller.
    """
    info = health(host, port)
    if not info:
        return None
    pid = info.get("pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def stop(port: int, host: str = "127.0.0.1", timeout: float = WAIT_FOR_STOP) -> int | None:
    """Terminate the server on this port. Returns the pid, or None if idle."""
    # The pidfile is the fallback, for a process wedged badly enough that it no
    # longer answers health but is still holding the port.
    pid = _reported_pid(host, port) or _recorded_pid(port)
    if pid is None:
        pidfile(port).unlink(missing_ok=True)
        return None

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pidfile(port).unlink(missing_ok=True)
        return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            break
        time.sleep(0.1)
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    pidfile(port).unlink(missing_ok=True)
    return pid


# --- the browser -----------------------------------------------------------

def open_page(url: str) -> bool:
    """Hand the URL to the desktop. False when there is no desktop to hand to.

    macOS has `open`, Linux has `xdg-open`, and a headless devserver has
    neither anything useful to run nor a display to run it on -- there the
    printed URL is the whole answer.
    """
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False

    for opener in ("open", "xdg-open"):
        path = shutil.which(opener)
        if not path:
            continue
        try:
            subprocess.Popen(
                [path, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return True
        except OSError:
            continue
    return False
