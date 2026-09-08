"""Committing and pushing on its own, a little after the writing stops.

The Checkpoint button asks for a message and commits one article. That is the
right shape for "I have finished a thought", and the wrong shape for "do not
lose this": it is manual, and it cannot capture the tree arrangement, which
belongs to no article. Work made in the browser and never checkpointed lives
only on the machine that served it.

So every write also arms a timer. When the writing stops, whatever changed
gets committed and pushed.

Two properties matter and are easy to get wrong:

* **Nothing blocks a request.** `touch()` resets a timer and returns; the git
  work happens later, on the timer's own thread. An edit's latency is
  unchanged whether this is on or off.
* **A burst is one commit.** Editing nine paragraphs in a row is one piece of
  work, not nine, so each write pushes the deadline back rather than queueing
  another commit.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import checkpoint as git

DEFAULT_DELAY = 45.0  # seconds of quiet before a commit


@dataclass
class Outcome:
    """What the last attempt did, for /api/health to report."""

    at: str = ""
    committed: bool = False
    revision: str = ""
    error: str = ""


@dataclass
class AutoCommit:
    """Commits a knowledge base once the writing has stopped."""

    inputs: Path
    outputs: Path
    delay: float = DEFAULT_DELAY
    enabled: bool = False

    _timer: threading.Timer | None = field(default=None, init=False, repr=False)
    # One git process at a time. A manual checkpoint can land mid-timer, and
    # two `git commit`s in one repository at once is an index.lock error.
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _last: Outcome = field(default_factory=Outcome, init=False, repr=False)

    # --- the request side, which must stay cheap --------------------------

    def touch(self) -> None:
        """Note that something changed. Returns immediately."""
        if not self.enabled:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True  # never hold up a shutdown
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    @property
    def last(self) -> Outcome:
        return self._last

    def status(self) -> dict:
        """What /api/health says about this, so a failure is not silent."""
        if not self.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "delay": self.delay,
            "at": self._last.at,
            "committed": self._last.committed,
            "revision": self._last.revision,
            "error": self._last.error,
        }

    # --- the timer's side -------------------------------------------------

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        self.run_now()

    def run_now(self) -> Outcome:
        """Commit and push whatever has changed. Never raises."""
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._lock:
                repo = git.repo_root(self.inputs)
                paths = [self.inputs, self.outputs]
                result = git.checkpoint(repo, paths, self._message(repo))
            self._last = Outcome(
                at=stamp, committed=result.committed, revision=result.revision
            )
        except git.GitError as exc:
            # A failed push is not an emergency: the commit is already made, and
            # `checkpoint` pushes unconditionally, so the next run catches up.
            self._last = Outcome(at=stamp, error=str(exc))
            print(f"mdweave: auto-commit failed: {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001 -- see below
            # Broad on purpose. This runs on a timer thread with nobody to
            # catch it, so anything unhandled would vanish into a dead thread
            # and leave the reader believing their work was being saved.
            self._last = Outcome(at=stamp, error=f"{type(exc).__name__}: {exc}")
            print(f"mdweave: auto-commit crashed: {exc!r}", flush=True)
        return self._last

    def _message(self, repo: Path) -> str:
        """Name what changed, the way the older hook's messages read."""
        names = sorted(_changed_documents(repo, self.inputs))
        if not names:
            return "auto: save"
        if len(names) == 1:
            return f"auto: update {names[0]}"
        return f"auto: update {len(names)} documents ({', '.join(names[:3])}…)"


def _changed_documents(repo: Path, inputs: Path) -> set[str]:
    """The documents behind the pending changes, by filename.

    Read off `git status`, so it sees a new file and a deleted one as readily
    as a modified one -- and ignores html_outputs, which is a consequence of
    the edit rather than a description of it.
    """
    try:
        listing = git._git(repo, "status", "--porcelain", "--", str(inputs))
    except git.GitError:
        return set()

    found = set()
    for line in listing.splitlines():
        path = line[3:].strip().strip('"')
        # A rename reads "old -> new"; the new name is the interesting one.
        path = path.rsplit(" -> ", 1)[-1]
        name = Path(path).name
        for suffix in (".ann.json", ".md"):
            if name.endswith(suffix):
                found.add(name[: -len(suffix)])
                break
    return found
