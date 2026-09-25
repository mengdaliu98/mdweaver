"""Taking in what another machine wrote, on a timer.

The container pushes what it writes and, until this existed, only *pulled* at
boot. So writing on a laptop and reading on the deployed instance was one
directional: the documents were on the remote and the container had no reason
to look. The only ways round it were to restart the service or to edit
something there, which would get its push rejected and merge as a side effect.

A poll rather than a webhook because there is nothing to receive one. It is
the same `reconcile` the boot script and a rejected push already use, so there
is one merge policy in the codebase and not three: generated pages are
resolved by rebuilding, a conflict in the prose is left for a person.

Shares the auto-commit's lock. Two `git` processes in one repository at the
same moment is an index.lock error, and a pull landing between a commit and
its push is worse than that.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import checkpoint as git

DEFAULT_INTERVAL = 180.0  # seconds


@dataclass
class Outcome:
    at: str = ""
    merged: bool = False
    error: str = ""


@dataclass
class Pull:
    """Fetch and merge the remote every `interval` seconds."""

    inputs: Path
    outputs: Path
    rebuild: Callable[[], None]
    interval: float = DEFAULT_INTERVAL
    enabled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    _timer: threading.Timer | None = field(default=None, init=False, repr=False)
    _last: Outcome = field(default_factory=Outcome, init=False, repr=False)

    def start(self) -> None:
        if not self.enabled:
            return
        self._arm()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm(self) -> None:
        self._timer = threading.Timer(self.interval, self._fire)
        self._timer.daemon = True  # never hold up a shutdown
        self._timer.start()

    def _fire(self) -> None:
        try:
            self.run_now()
        finally:
            # Re-armed whatever happened. A poller that stops on its first
            # network blip is a poller that stops.
            if self.enabled:
                self._arm()

    def run_now(self) -> Outcome:
        """Fetch, merge, and rebuild if anything arrived. Never raises."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self.lock:
                repo = git.repo_root(self.inputs)
                merged = git.reconcile(repo, self.outputs)
            # Outside the lock: rendering is slow and touches no git state,
            # and holding the lock through it would stall a commit.
            if merged:
                self.rebuild()
            self._last = Outcome(at=stamp, merged=merged)
        except git.GitError as exc:
            # Expected and survivable: offline, or a prose conflict that
            # `reconcile` refused to guess at. Reported, not raised.
            self._last = Outcome(at=stamp, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            # Broad on purpose, like the auto-commit's: this runs on a timer
            # thread with nobody to catch it, and an unhandled error would
            # take the thread out silently and stop the polling for good.
            self._last = Outcome(at=stamp, error=f"{type(exc).__name__}: {exc}")
        return self._last

    def status(self) -> dict:
        if not self.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "interval": self.interval,
            "at": self._last.at,
            "merged": self._last.merged,
            "error": self._last.error,
        }
