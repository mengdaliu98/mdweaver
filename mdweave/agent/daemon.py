"""The devserver's half: ask for work, do it, push the result.

A loop around one request. `POST /api/agent/claim` is held open by the
container until there is a job or twenty-five seconds have passed, so a button
press reaches this machine as fast as a stream would deliver it -- and every
cycle is still a complete HTTP transaction that either finished or timed out.
There is no "is the connection still alive?" to get wrong, and a wedged socket
heals itself on the next pass instead of hanging until some proxy's cap.

What happens to a job, in order:

  1. run one Claude turn in the knowledge base checkout, forwarding progress
  2. re-render, commit, push
  3. ask the container to pull and rebuild, so the page shows it

Every job is a fresh Claude session. Nothing is carried between presses: the
turn reads the document off disk, which is the only state that was ever
authoritative anyway, and the prompt says everything it needs to.

Step 3 is the one that is easy to leave out. Git carries the prose between the
two machines perfectly well, but the container only pulls at boot -- so without
it the edit is safely committed and invisible.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import actions as registry
from . import runner
from . import protocol
from .protocol import CLAIM_SECONDS

# Slack on top of the server's own claim window, so a claim that is answered
# right at the deadline is not read here as a timeout.
CLAIM_MARGIN = 20.0

# Backoff between failed attempts to reach the container, in seconds.
BACKOFF = [1, 2, 5, 10, 20, 30, 60]


@dataclass
class Config:
    remote: str
    inputs: Path
    outputs: Path
    runner_name: str = ""
    model: str | None = None
    permission_mode: str = "bypassPermissions"
    add_dirs: tuple[str, ...] = ()
    once: bool = False
    verbose: bool = False


class Transport:
    """The few calls this makes, over stdlib http.

    No new dependency for six endpoints, and no credential: the endpoints this
    talks to take none. TLS still matters and is not optional -- it is what
    stops anything but the real site from answering, and `https` gets it.
    """

    def __init__(self, remote: str) -> None:
        self.remote = remote.rstrip("/")

    def post(self, path: str, payload: dict | None = None, timeout: float = 30.0):
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.remote}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 204:
                return None
            raw = response.read()
            return json.loads(raw) if raw else None


def _log(message: str) -> None:
    print(f"mdweave-agent: {message}", flush=True)


class Runner:
    """One job at a time, which is the only safe concurrency here.

    Two turns against one document would be two Claude sessions writing the
    same file, and two `git commit`s in one repository at once is an
    index.lock error. Neither is worth the parallelism.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.http = Transport(config.remote)

    # --- one job -----------------------------------------------------------

    def handle(self, job: dict) -> None:
        from ..serve import Workspace

        job_id = job["id"]
        document = job.get("document", "")
        action_name = job.get("action", "")
        instruction = job.get("instruction", "")
        raw_comments = job.get("comments") or []
        comments = [c for c in raw_comments if isinstance(c, dict)]

        def note(kind: str, text: str) -> None:
            try:
                self.http.post(
                    f"/api/agent/jobs/{job_id}/events", {"kind": kind, "text": text}
                )
            except (urllib.error.URLError, OSError, ValueError) as exc:
                # Losing a progress line costs the reader a spinner, not the
                # work: the turn keeps going and the result still gets posted.
                if self.config.verbose:
                    _log(f"could not post an event: {exc}")

        def finish(ok: bool, detail: str, revision: str = "", session: str = "") -> None:
            # `session` is reported for the record, not to be resumed from:
            # it is the id of the one fresh session that ran this job, which
            # is what you need to find its transcript on this machine
            # afterwards. Nothing reads it back to continue a conversation.
            try:
                self.http.post(
                    f"/api/agent/jobs/{job_id}/done",
                    {
                        "ok": ok,
                        "detail": detail,
                        "revision": revision,
                        "session": session,
                    },
                )
            except (urllib.error.URLError, OSError, ValueError) as exc:
                _log(f"could not report the result of {job_id}: {exc}")

        workspace = Workspace(inputs=self.config.inputs, outputs=self.config.outputs)

        try:
            # A job naming a document that is not here should fail now and say
            # so, rather than as a Claude turn that spends a minute looking for
            # a file nobody has.
            path = workspace.markdown_for(document)
        except Exception as exc:  # ApiError, mostly
            finish(False, f"no document called {document!r} here ({exc})")
            return

        action = registry.find(self.config.inputs, action_name)
        if action is None:
            finish(False, f"no action called {action_name!r} in this knowledge base")
            return

        if action.needs_instruction and not instruction.strip():
            finish(False, f"{action.label} needs something to be asked")
            return

        _log(f"{job_id}: {action.name} on {document}")
        note("status", f"{action.label} — {document}")

        location, extra_dirs = self.locate(path, document)
        if action.name == registry.REVIEW:
            prompt = registry.review_prompt(action, location, instruction, comments)
            if not comments:
                note(
                    "status",
                    "no comments were selected; asking for a read-through instead",
                )
        else:
            prompt = action.render(document, instruction)

        outcome = runner.run(
            prompt,
            root=self.config.inputs.parent,
            on_event=note,
            model=self.config.model,
            permission_mode=self.config.permission_mode,
            add_dirs=[*self.config.add_dirs, *extra_dirs],
        )

        if not outcome.ok:
            _log(f"{job_id}: failed — {outcome.error}")
            finish(False, outcome.error or "the turn failed", session=outcome.session_id or "")
            return

        revision = ""
        try:
            note("status", "re-rendering and pushing")
            revision = self.publish(workspace, document, action, instruction)
        except Exception as exc:  # noqa: BLE001 -- reported, never raised
            note("error", f"the edit is saved here but not pushed: {exc}")
            finish(
                False,
                f"{outcome.summary} — but publishing failed: {exc}",
                session=outcome.session_id or "",
            )
            return

        try:
            self.http.post("/api/agent/reconcile", {"job": job_id}, timeout=180.0)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            note("error", f"pushed, but the site could not pull it in: {exc}")

        _log(f"{job_id}: done — {outcome.summary}")
        finish(True, outcome.summary, revision, outcome.session_id or "")

    def locate(self, path: Path, document: str) -> tuple[str, list[str]]:
        """Where to send Claude for this document, and what else to open up.

        For an ordinary file that is `markdown_inputs/<id>.md`, relative to
        the checkout the turn runs in.

        A symlink is the interesting case, and following it is not a tidiness
        measure. A linked document almost always lives beside the thing it
        describes -- a design note in a codebase, a plan in a project
        directory -- and that neighbourhood is most of why the document is
        worth editing at all: the model needs to read the code to answer a
        comment about it. Handing over the link inside `markdown_inputs/`
        instead gives a model that can open one file and see nothing around
        it, in a directory full of unrelated notes.

        So the real path goes in the prompt, and the original's parent goes to
        `--add-dir`. Without the second half the first is a cruelty: the path
        resolves, the edit is refused, and the turn reports a permission error
        about a file it was told to work on.
        """
        try:
            if not path.is_symlink():
                return f"markdown_inputs/{document}.md", []
            real = path.resolve()
        except OSError:
            # A link we cannot resolve is one we should not be inventing a
            # path for. The checkout-relative name still opens it, because
            # everything that reads it follows the link anyway.
            return f"markdown_inputs/{document}.md", []

        _log(f"{document} is a link to {real}; working there instead")
        return str(real), [str(real.parent)]

    def publish(self, workspace, document: str, action, instruction: str) -> str:
        """Re-render, commit and push whatever the turn changed.

        The whole of `markdown_inputs` and `html_outputs`, not just this
        document: a turn is free to write a second file or delete one, and a
        commit scoped to the document that was asked about would leave that
        stranded on this machine.
        """
        from .. import checkpoint as git

        workspace.rebuild_all()

        repo = git.repo_root(self.config.inputs)
        summary = instruction.strip().splitlines()[0] if instruction.strip() else action.label
        message = f"agent: {action.name} on {document}\n\n{summary}".strip()

        result = git.checkpoint(
            repo,
            [self.config.inputs, self.config.outputs],
            message,
            generated=self.config.outputs,
            on_merge=workspace.rebuild_all,
        )
        return result.revision

    # --- the loop ----------------------------------------------------------

    def claim(self):
        return self.http.post(
            "/api/agent/claim",
            {"runner": self.config.runner_name},
            timeout=CLAIM_SECONDS + CLAIM_MARGIN,
        )

    def stale(self) -> bool:
        """Has the source on disk moved on from what this process imported?

        The same disease `mdweave start` already guards the server against: a
        long-lived process keeps whatever it imported at startup, so an edit is
        invisible until it restarts. It bites harder here, because the runner
        imports `serve` lazily when a job arrives -- so a *fresh* serve.py gets
        loaded against a *stale* cached `tree`, and the failure is an
        ImportError for a name that is plainly right there in the file. That
        is a genuinely baffling half hour, and it cost one real job.

        Checked between jobs rather than during one, so a turn in flight is
        never interrupted by a deploy on another terminal.
        """
        from ..serve import RUNNING_FINGERPRINT, source_fingerprint

        try:
            return source_fingerprint() != RUNNING_FINGERPRINT
        except OSError:
            return False  # cannot read the tree; not a reason to fall over

    def loop(self) -> int:
        _log(f"talking to {self.config.remote}")
        _log(f"knowledge base at {self.config.inputs}")
        _log(f"permission mode: {self.config.permission_mode}")

        failures = 0
        while True:
            # Exit rather than reload: a Python process cannot honestly swap
            # its own imported modules. Under the systemd unit `Restart=always`
            # turns "the code changed" into a ten second gap; run from a
            # terminal, or in-process behind `serve --monolithic`, it is a stop
            # and the next start picks the new code up. The wording stays true
            # of both rather than promising a supervisor that may not be there.
            if self.stale():
                _log("the source on disk has changed; stopping so a restart picks it up")
                return 0

            try:
                job = self.claim()
                if failures:
                    _log("reconnected")
                failures = 0
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                # A 401 here used to mean a bad token and was worth exiting
                # over. There is no token any more, so it can only mean the
                # container is running code older than this -- which is
                # precisely what a deploy looks like from underneath, and it
                # resolves itself in a minute. Exiting on it meant a runner
                # started during a redeploy died instead of waiting, which is
                # how this was found.
                if exc.code in (401, 403):
                    if failures == 0:
                        _log(
                            f"the container refused this runner ({exc.code}) -- "
                            "it is probably mid-deploy and older than this; waiting"
                        )
                else:
                    _log(f"claim failed: {exc.code} {detail}")
                job = None
                failures += 1
            except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
                if self.config.verbose or failures == 0:
                    _log(f"cannot reach the container: {exc}")
                job = None
                failures += 1

            if job:
                try:
                    self.handle(job)
                except Exception as exc:  # noqa: BLE001
                    # A crash here must not take the loop down: the lease will
                    # stall the job, and the next press should still work.
                    _log(f"crashed handling {job.get('id')}: {exc!r}")
                if self.config.once:
                    return 0
                continue

            if self.config.once and not failures:
                _log("nothing queued")
                return 0

            if failures:
                delay = BACKOFF[min(failures - 1, len(BACKOFF) - 1)]
                time.sleep(delay)


def from_env(args) -> Config | None:
    """Assemble the configuration, complaining about what is missing."""
    remote = args.remote or protocol.setting(protocol.RUNNER_REMOTE)

    problems = []
    if not remote:
        problems.append(
            f"  --remote https://<app>.up.railway.app  (or {protocol.RUNNER_REMOTE})"
        )
    if not args.indir.is_dir():
        problems.append(f"  -i {args.indir} is not a directory")

    if problems:
        print("error: mdweave agent needs:", file=sys.stderr)
        for line in problems:
            print(line, file=sys.stderr)
        return None

    return Config(
        remote=remote,
        inputs=args.indir.resolve(),
        outputs=args.outdir.resolve(),
        runner_name=args.name or socket.gethostname(),
        model=args.model,
        permission_mode=args.permission_mode,
        add_dirs=tuple(args.add_dir or ()),
        once=args.once,
        verbose=args.verbose,
    )
