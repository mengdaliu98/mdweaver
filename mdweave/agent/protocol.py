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

# --- what the settings are called ------------------------------------------
#
# This feature adds no credential of its own. It did add two, and both were
# deleted: a token for the runner, and an operator key for the browser.
#
# The operator key existed to stop a hostile page queueing a job with
# credentials your browser attaches by itself. Requiring
# `Content-Type: application/json` on every body ended that whole class more
# cheaply -- a cross-site form cannot send it, and a script that tries forces
# a preflight this server does not answer -- which left the key guarding only
# "a leaked reading password is not also an execution one". A real property,
# but not one worth a second secret on a personal knowledge base, and not one
# that survives a page password written down somewhere anyway.
#
# The old names still work. A deployment is a thing someone has already
# configured, and renaming a variable should not be a way to take their site
# down while they are not looking.

RUNNER_REMOTE = "MDWEAVE_RUNNER_REMOTE"
JOBS_STORE = "MDWEAVE_JOBS_STORE"

SUPERSEDED = {
    RUNNER_REMOTE: "MDWEAVE_AGENT_REMOTE",
    JOBS_STORE: "MDWEAVE_AGENT_STORE",
}

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

    `comments` is the review flow's payload: one entry per comment the reader
    ticked, already resolved into the quote, the body, the semantic type and
    that type's instruction. Resolved *here*, when the button is pressed,
    rather than looked up by the runner from its own checkout -- the runner
    does not pull before a turn, so its `.ann.json` can be minutes behind the
    one the reader is looking at, and a review of comments that have since
    been edited is worse than no review at all.

    `before` and `after` are the document's markdown either side of the turn.
    They exist so the browser can draw a diff once the job is done, which it
    cannot reconstruct afterwards: the file on disk has already moved, and git
    is on the other machine until the push lands.
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
    comments: list[dict] = field(default_factory=list)
    before: str = ""
    after: str = ""

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL

    def summary(self) -> dict:
        """What the browser is told about a job, without the event log.

        `comments` is a count here for the same reason `events` is, and
        `before`/`after` are left out entirely: a summary is polled, and two
        whole documents in it would be two whole documents on every poll.
        """
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
            "comments": len(self.comments),
        }

    def to_dict(self) -> dict:
        payload = self.summary()
        payload.update(
            {
                "session": self.session,
                "claimed_at": self.claimed_at,
                "lease_until": self.lease_until,
                "events": [e.to_dict() for e in self.events],
                "comments": list(self.comments),
                "before": self.before,
                "after": self.after,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        raw = data.get("events") or []
        events = [Event.from_dict(e) for e in raw] if isinstance(raw, list) else []
        # A job written by an older process has no `comments` at all, and one
        # whose count survived a `summary()` round trip has an integer there.
        # Neither is a list of comments, and both must read back as "none"
        # rather than taking the whole store down on load.
        stored = data.get("comments")
        comments = [c for c in stored if isinstance(c, dict)] if isinstance(stored, list) else []
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
            comments=comments,
            before=str(data.get("before", "")),
            after=str(data.get("after", "")),
        )


def dispatch(job: Job) -> dict:
    """What a runner is handed when it claims a job.

    The comments go over the wire rather than being re-read on the devserver,
    because they are the instruction: a review job with an empty list is a
    turn that will read the document and guess. `before`/`after` do not --
    they are for the browser's diff, and the runner has the file itself.
    """
    return {
        "id": job.id,
        "document": job.document,
        "action": job.action,
        "instruction": job.instruction,
        "comments": job.comments,
        "lease": LEASE_SECONDS,
    }
