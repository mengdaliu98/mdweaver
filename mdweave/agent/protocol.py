"""The shapes the two halves agree on.

Both ends are deployed from this repository, so there is exactly one definition
of a job and one of an event, and no version to negotiate.
"""

from __future__ import annotations

import os
import random
import string
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- what the two credentials are called -----------------------------------
#
# There are two secrets and they belong to two different holders, so they are
# named after the holders: the *operator* is a person at a browser, the
# *runner* is the process on the devserver. They were originally
# MDWEAVE_AGENT_PASSWORD and MDWEAVE_AGENT_TOKEN, which read as a matched pair
# and are not one -- and the first of those went by three names depending on
# where you met it (a prompt said "agent key", the setting said "password",
# the request header said neither). Two secrets are confusing enough.
#
# The old names still work. A deployment is a thing someone has already
# configured, and renaming a variable should not be a way to take their site
# down while they are not looking.

OPERATOR_KEY = "MDWEAVE_OPERATOR_KEY"
RUNNER_TOKEN = "MDWEAVE_RUNNER_TOKEN"
RUNNER_REMOTE = "MDWEAVE_RUNNER_REMOTE"
JOBS_STORE = "MDWEAVE_JOBS_STORE"

SUPERSEDED = {
    OPERATOR_KEY: "MDWEAVE_AGENT_PASSWORD",
    RUNNER_TOKEN: "MDWEAVE_AGENT_TOKEN",
    RUNNER_REMOTE: "MDWEAVE_AGENT_REMOTE",
    JOBS_STORE: "MDWEAVE_AGENT_STORE",
}

# The header the browser presents the operator key in. Both are accepted for
# the same reason the variables are, and because a page cached in somebody's
# tab is older than whatever the server is running.
OPERATOR_HEADER = "X-Mdweave-Operator-Key"
OPERATOR_HEADER_WAS = "X-Mdweave-Agent-Key"

_warned: set[str] = set()


def setting(name: str) -> str:
    """Read one of the settings above, accepting the name it used to have.

    Says so once per process when the old name is what answered -- once,
    because this is read on every request and a line per request would bury
    the log it is trying to be useful in.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value

    old = SUPERSEDED.get(name)
    if not old:
        return ""
    value = os.environ.get(old, "").strip()
    if value and old not in _warned:
        _warned.add(old)
        print(
            f"mdweave: {old} is the old name for {name}; still honoured, "
            "rename it when convenient",
            file=sys.stderr,
            flush=True,
        )
    return value

ID_ALPHABET = string.ascii_lowercase + string.digits
ID_LENGTH = 8

# How long the runner may hold a job before the broker decides it is gone. Each
# event it posts pushes this back, so a long Claude turn is not a timeout as
# long as it is saying something; silence is what expires.
LEASE_SECONDS = 120.0

# How long the broker holds a claim open before answering "nothing". Comfortably
# inside Railway's five-minute idle cut-off, which closes a request that has
# transferred no data.
CLAIM_SECONDS = 25.0

# How long a browser's event stream waits for something new before sending a
# heartbeat comment. Same reasoning, more headroom.
STREAM_HEARTBEAT = 20.0


# --- job states ------------------------------------------------------------
#
# queued -> claimed -> done
#              |  \--> failed
#              \-----> stalled     (the lease expired: the runner went away)
#
# `stalled` is deliberately terminal rather than a return to `queued`. The
# usual advice is at-least-once delivery, and it is wrong here: a job runs a
# language model that edits files, so a redelivery is not a retry, it is a
# second, different edit. Surfacing it and letting a person press the button
# again is the only honest recovery.

QUEUED = "queued"
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"
STALLED = "stalled"

TERMINAL = {DONE, FAILED, STALLED}


def new_id() -> str:
    return "".join(random.choices(ID_ALPHABET, k=ID_LENGTH))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Event:
    """One line of progress on a job.

    `seq` is per-job and monotonic. It is what a browser sends back as
    `Last-Event-ID` after a dropped stream, and what the broker replays from --
    the SSE standard carries the id around but stores nothing, so the replay
    buffer is ours to keep.
    """

    seq: int
    kind: str  # status | text | tool | error | done
    text: str
    at: str = field(default_factory=now)

    def to_dict(self) -> dict:
        return {"seq": self.seq, "kind": self.kind, "text": self.text, "at": self.at}

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            seq=int(data.get("seq", 0)),
            kind=str(data.get("kind", "status")),
            text=str(data.get("text", "")),
            at=str(data.get("at") or now()),
        )


@dataclass
class Job:
    """One press of one button.

    `session` is filled in by the runner, not the browser: which conversation
    owns a document is recorded beside the document on the devserver, and the
    container has no business knowing it.
    """

    id: str
    document: str
    action: str
    instruction: str = ""
    state: str = QUEUED
    created: str = field(default_factory=now)
    claimed_at: float = 0.0
    lease_until: float = 0.0
    session: str | None = None
    detail: str = ""
    revision: str = ""
    events: list[Event] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL

    def summary(self) -> dict:
        """What the browser is told about a job, without the event log."""
        return {
            "id": self.id,
            "document": self.document,
            "action": self.action,
            "instruction": self.instruction,
            "state": self.state,
            "created": self.created,
            "detail": self.detail,
            "revision": self.revision,
            "events": len(self.events),
        }

    def to_dict(self) -> dict:
        payload = self.summary()
        payload.update(
            {
                "session": self.session,
                "claimed_at": self.claimed_at,
                "lease_until": self.lease_until,
                "events": [e.to_dict() for e in self.events],
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        raw = data.get("events") or []
        events = [Event.from_dict(e) for e in raw] if isinstance(raw, list) else []
        return cls(
            id=str(data.get("id") or new_id()),
            document=str(data.get("document", "")),
            action=str(data.get("action", "")),
            instruction=str(data.get("instruction", "")),
            state=str(data.get("state", QUEUED)),
            created=str(data.get("created") or now()),
            claimed_at=float(data.get("claimed_at") or 0.0),
            lease_until=float(data.get("lease_until") or 0.0),
            session=data.get("session") or None,
            detail=str(data.get("detail", "")),
            revision=str(data.get("revision", "")),
            events=events,
        )


def dispatch(job: Job) -> dict:
    """What a runner is handed when it claims a job."""
    return {
        "id": job.id,
        "document": job.document,
        "action": job.action,
        "instruction": job.instruction,
        "lease": LEASE_SECONDS,
    }
