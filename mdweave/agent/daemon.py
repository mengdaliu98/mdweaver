"""The devserver's half: ask for work, do it, push the result.

A loop around one request. `POST /api/agent/claim` is held open by the
container until there is a job or twenty-five seconds have passed, so a button
press reaches this machine as fast as a stream would deliver it -- and every
cycle is still a complete HTTP transaction that either finished or timed out.
There is no "is the connection still alive?" to get wrong, and a wedged socket
heals itself on the next pass instead of hanging until some proxy's cap.

What happens to a job, in order:

  1. read the session sidecar beside the document, for the conversation id
  2. run one Claude turn in the knowledge base checkout, forwarding progress
  3. write the session id back, so the next press resumes this conversation
  4. re-render, commit, push
  5. ask the container to pull and rebuild, so the page shows it

Step 5 is the one that is easy to leave out. Git carries the prose between the
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
    token: str
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

    No new dependency for six endpoints, and one place that knows the token is
    a bearer header rather than the page's basic auth -- the two credentials
    are deliberately different, so the password that reads your notes is not
    also the one that runs commands here.
    """

    def __init__(self, remote: str, token: str) -> None:
        self.remote = remote.rstrip("/")
        self.token = token

    def post(self, path: str, payload: dict | None = None, timeout: float = 30.0):
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.remote}{path}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
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
        self.http = Transport(config.remote, config.token)

    # --- one job -----------------------------------------------------------

    def handle(self, job: dict) -> None:
        from ..serve import Workspace
        from ..sources import session as session_store

        job_id = job["id"]
        document = job.get("document", "")
        action_name = job.get("action", "")
        instruction = job.get("instruction", "")

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
            markdown = workspace.markdown_for(document)
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

        sidecar = session_store.session_path(markdown)
        session = session_store.load(sidecar)

        if action.mode == "warm" and not session.warm:
            # Recorded rather than acted on. A resumed one-shot reuses the same
            # session id, so the conversation is already joinable from a
            # terminal -- which is what `warm` was really for.
            session.warm = True

        _log(f"{job_id}: {action.name} on {document}")
        note("status", f"{action.label} — {document}")

        prompt = action.render(document, instruction)
        outcome = runner.run(
            prompt,
            root=self.config.inputs.parent,
            session=session.session_id,
            on_event=note,
            model=self.config.model,
            permission_mode=self.config.permission_mode,
            add_dirs=list(self.config.add_dirs),
        )

        if outcome.session_id:
            session.session_id = outcome.session_id
            session.cwd = str(self.config.inputs.parent)
            session.turns += max(outcome.turns, 1)
            try:
                session_store.save(sidecar, session)
            except OSError as exc:
                note("error", f"could not record the session id: {exc}")

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

    def loop(self) -> int:
        _log(f"talking to {self.config.remote}")
        _log(f"knowledge base at {self.config.inputs}")
        _log(f"permission mode: {self.config.permission_mode}")

        failures = 0
        while True:
            try:
                job = self.claim()
                if failures:
                    _log("reconnected")
                failures = 0
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                if exc.code in (401, 403):
                    _log(f"the container refused this token ({exc.code}): {detail}")
                    return 1
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
    token = args.token or protocol.setting(protocol.RUNNER_TOKEN)

    problems = []
    if not remote:
        problems.append(
            f"  --remote https://<app>.up.railway.app  (or {protocol.RUNNER_REMOTE})"
        )
    if not token:
        problems.append(
            f"  --token <shared secret>                (or {protocol.RUNNER_TOKEN})"
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
        token=token,
        inputs=args.indir.resolve(),
        outputs=args.outdir.resolve(),
        runner_name=args.name or socket.gethostname(),
        model=args.model,
        permission_mode=args.permission_mode,
        add_dirs=tuple(args.add_dir or ()),
        once=args.once,
        verbose=args.verbose,
    )
