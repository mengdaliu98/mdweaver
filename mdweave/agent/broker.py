"""The container's half: a queue with leases.

Buttons put jobs in, the devserver's runner takes them out, and progress comes
back the same way it went in -- over ordinary requests. There is no connection
held open to the runner and nothing to keep alive, which is the point: Railway
closes an HTTP request after five minutes of silence and caps every one of them
at fifteen, so a "persistent" stream to the devserver would be a fiction
reconnecting four times an hour. A claim that waits twenty-five seconds and
then answers "nothing" is the same latency with none of that.

Everything here is called from `ThreadingHTTPServer` worker threads, so every
public method takes the lock, and the two that wait -- `claim` and
`wait_for_events` -- wait on the condition rather than polling.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .protocol import (
    CLAIM_SECONDS,
    CLAIMED,
    DONE,
    FAILED,
    LEASE_SECONDS,
    QUEUED,
    STALLED,
    Event,
    Job,
    new_id,
)

# How many finished jobs to keep around for the page to ask about.
HISTORY = 200

# A runner that claimed within this long ago is considered present. One claim
# timeout plus enough slack for the round trip to Amsterdam and back.
PRESENT_AFTER = CLAIM_SECONDS + 20.0

# Per-job event cap. A chatty Claude turn should not grow the store without
# bound; the tail is the interesting part, so the head is what goes.
MAX_EVENTS = 500


class Broker:
    """Jobs, their events, and who is holding what."""

    def __init__(
        self,
        store: Path | None = None,
        lease: float = LEASE_SECONDS,
        history: int = HISTORY,
    ) -> None:
        self.store = store
        self.lease = lease
        self.history = history
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._condition = threading.Condition()
        self._last_claim = 0.0
        self._runner_seen = ""
        self._load()

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        """Read back what a previous process left, if there is a volume.

        Anything that was `claimed` when this process started is stalled by
        definition: the runner was holding a request against a server that no
        longer exists, and it will never report back on that job. Marking it
        so is more honest than leaving it to time out against a lease clock
        that restarted with us.
        """
        if not self.store or not self.store.exists():
            return
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return
        for raw in data.get("jobs", []):
            try:
                job = Job.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if job.state == CLAIMED:
                job.state = STALLED
                job.detail = "the server restarted while this job was running"
            self._jobs[job.id] = job

    def _save(self) -> None:
        """Mirror the queue to the volume. Called with the lock held."""
        if not self.store:
            return
        payload = {"jobs": [j.to_dict() for j in self._jobs.values()]}
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.store.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(temporary, self.store)
        except OSError:
            # The queue lives in memory and the work is already happening. A
            # volume that will not take a write should not fail the request.
            pass

    # --- housekeeping ------------------------------------------------------

    def _expire(self) -> bool:
        """Stall anything whose runner stopped talking. Lock held."""
        changed = False
        cutoff = time.monotonic()
        for job in self._jobs.values():
            if job.state == CLAIMED and job.lease_until and job.lease_until < cutoff:
                job.state = STALLED
                job.detail = (
                    "the runner stopped reporting; nothing was retried, "
                    "because a Claude turn is not safe to repeat"
                )
                job.events.append(
                    Event(seq=len(job.events) + 1, kind="error", text=job.detail)
                )
                changed = True
        return changed

    def _trim(self) -> None:
        """Forget the oldest finished jobs. Lock held."""
        while len(self._jobs) > self.history:
            for job_id, job in self._jobs.items():
                if job.finished:
                    del self._jobs[job_id]
                    break
            else:
                return  # nothing finished to drop; let it grow rather than lose work

    # --- the browser's side ------------------------------------------------

    def submit(self, document: str, action: str, instruction: str = "") -> Job:
        job = Job(
            id=new_id(), document=document, action=action, instruction=instruction
        )
        with self._condition:
            self._expire()
            self._jobs[job.id] = job
            self._trim()
            self._save()
            self._condition.notify_all()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._condition:
            self._expire()
            return self._jobs.get(job_id)

    def recent(self, document: str | None = None, limit: int = 20) -> list[Job]:
        with self._condition:
            self._expire()
            jobs = list(self._jobs.values())
        if document:
            jobs = [j for j in jobs if j.document == document]
        return jobs[-limit:][::-1]

    def events_since(self, job_id: str, after: int) -> list[Event]:
        with self._condition:
            job = self._jobs.get(job_id)
            return [e for e in job.events if e.seq > after] if job else []

    def wait_for_events(
        self, job_id: str, after: int, timeout: float
    ) -> tuple[list[Event], str]:
        """Block until this job says something new, or the timeout runs out.

        Returns the events and the job's state, so a stream can close itself
        once there is nothing more coming.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                self._expire()
                job = self._jobs.get(job_id)
                if job is None:
                    return [], ""
                fresh = [e for e in job.events if e.seq > after]
                if fresh or job.finished:
                    return fresh, job.state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], job.state
                self._condition.wait(remaining)

    def status(self) -> dict:
        with self._condition:
            self._expire()
            waiting = sum(1 for j in self._jobs.values() if j.state == QUEUED)
            running = sum(1 for j in self._jobs.values() if j.state == CLAIMED)
            since = time.monotonic() - self._last_claim if self._last_claim else None
            connected = since is not None and since < PRESENT_AFTER
            self._save()
        return {
            "connected": connected,
            "runner": self._runner_seen if connected else "",
            "last_seen": round(since, 1) if since is not None else None,
            "queued": waiting,
            "running": running,
        }

    # --- the runner's side -------------------------------------------------

    def claim(self, timeout: float = CLAIM_SECONDS, runner: str = "") -> Job | None:
        """Hand out one queued job, waiting up to `timeout` for one to appear.

        Holding the request open is what makes a button press feel immediate
        without a runner that polls in a tight loop. Answering None is a
        perfectly good outcome -- the runner simply asks again.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            self._last_claim = time.monotonic()
            if runner:
                self._runner_seen = runner
            while True:
                self._expire()
                for job in self._jobs.values():
                    if job.state == QUEUED:
                        job.state = CLAIMED
                        job.claimed_at = time.monotonic()
                        job.lease_until = job.claimed_at + self.lease
                        self._save()
                        return job

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._save()
                    return None
                self._condition.wait(remaining)
                # Refresh the heartbeat: the runner is demonstrably still here
                # for the whole time it is blocked in this call.
                self._last_claim = time.monotonic()

    def note(self, job_id: str, kind: str, text: str) -> Event | None:
        """Record progress, and push the lease back.

        Renewing on every event is what lets a long turn run without a timeout
        while still catching a runner that has died: silence expires, work does
        not.
        """
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None or job.finished:
                return None
            event = Event(seq=len(job.events) + 1, kind=kind, text=text)
            job.events.append(event)
            if len(job.events) > MAX_EVENTS:
                del job.events[: len(job.events) - MAX_EVENTS]
            job.lease_until = time.monotonic() + self.lease
            self._last_claim = time.monotonic()
            self._save()
            self._condition.notify_all()
            return event

    def finish(
        self,
        job_id: str,
        ok: bool,
        detail: str = "",
        revision: str = "",
        session: str | None = None,
    ) -> Job | None:
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.state = DONE if ok else FAILED
            job.detail = detail
            job.revision = revision
            if session:
                job.session = session
            job.events.append(
                Event(
                    seq=len(job.events) + 1,
                    kind="done" if ok else "error",
                    text=detail or ("finished" if ok else "failed"),
                )
            )
            job.lease_until = 0.0
            self._last_claim = time.monotonic()
            self._save()
            self._condition.notify_all()
            return job
