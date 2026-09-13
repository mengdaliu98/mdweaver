"""Running one Claude turn on the devserver.

`claude -p --resume <session>` in the knowledge base checkout, with its
`stream-json` output parsed back into the small event vocabulary the page
understands. One turn per job: the process starts, edits the markdown, and
exits, so nothing is resident between button presses and a devserver drain
costs at most the turn that was in flight.

Resuming reuses the *same* session id rather than minting a new one, which is
the property the whole design leans on -- it means the conversation a button is
driving is the same conversation you get by typing

    claude --resume <session-id>

in a terminal on this machine. The sidecar beside the document holds that id,
so joining the session Claude is having about a document is a copy and a paste.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# A turn that has produced nothing at all for this long is hung rather than
# thinking. Generous: a large edit over a long document is genuinely slow.
SILENCE_TIMEOUT = 300.0

# The most any single turn may take, however talkative it is.
WALL_TIMEOUT = 1800.0

# Tool inputs and assistant prose both go into an event log the browser
# renders, so neither should be able to push a megabyte into it.
SNIPPET = 240


@dataclass
class RunResult:
    ok: bool
    session_id: str | None = None
    summary: str = ""
    error: str = ""
    turns: int = 0
    cost: float = 0.0
    tools: list[str] = field(default_factory=list)


def _clip(text: str, limit: int = SNIPPET) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_tool(name: str, payload: dict, root: Path) -> str:
    """One line for the event log: what the model just reached for.

    Paths are shown relative to the knowledge base, because an absolute path
    into somebody's devserver is noise in a page about their notes.
    """
    def relative(raw) -> str:
        try:
            return str(Path(str(raw)).relative_to(root))
        except (ValueError, TypeError):
            return str(raw)

    if not isinstance(payload, dict):
        return name

    if name in ("Write", "Read", "NotebookEdit"):
        return f"{name} {relative(payload.get('file_path', ''))}"
    if name == "Edit":
        return f"Edit {relative(payload.get('file_path', ''))}"
    if name == "Bash":
        return f"Bash {_clip(payload.get('command', ''), 120)}"
    if name in ("Grep", "Glob"):
        return f"{name} {_clip(payload.get('pattern', ''), 80)}"
    if name == "Task":
        return f"Task {_clip(payload.get('description', ''), 80)}"
    return name


def build_argv(
    prompt: str,
    session: str | None,
    *,
    model: str | None = None,
    permission_mode: str = "bypassPermissions",
    add_dirs: list[str] | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """The command line for one turn.

    `--verbose` is not optional next to `--output-format stream-json`: without
    it the stream carries only the final result, and the page shows a spinner
    for two minutes and then everything at once.
    """
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        permission_mode,
        # Nobody is watching stdin to answer a prompt, so anything that would
        # ask must resolve on its own rather than deadlocking the job.
        "--permission-prompts",
        "none",
    ]
    if session:
        argv += ["--resume", session]
    if model:
        argv += ["--model", model]
    for directory in add_dirs or []:
        argv += ["--add-dir", directory]
    argv += extra or []
    return argv


def _pump(stream, sink: "queue.Queue", tag: str) -> None:
    try:
        for line in stream:
            sink.put((tag, line))
    finally:
        sink.put((tag, None))


def run(
    prompt: str,
    root: Path,
    session: str | None = None,
    *,
    on_event=None,
    model: str | None = None,
    permission_mode: str = "bypassPermissions",
    add_dirs: list[str] | None = None,
    extra: list[str] | None = None,
    silence_timeout: float = SILENCE_TIMEOUT,
    wall_timeout: float = WALL_TIMEOUT,
    cancelled=None,
) -> RunResult:
    """Run one turn and report what happened.

    `on_event(kind, text)` is called as the turn progresses; it is what ends up
    in the browser. `cancelled` is an optional callable polled between lines so
    a stop from the page can take effect without waiting for the model.
    """
    emit = on_event or (lambda kind, text: None)

    if shutil.which("claude") is None:
        return RunResult(ok=False, error="the `claude` CLI is not on this machine's PATH")

    argv = build_argv(
        prompt,
        session,
        model=model,
        permission_mode=permission_mode,
        add_dirs=add_dirs,
        extra=extra,
    )

    emit("status", f"resuming {session[:8]}" if session else "starting a new session")

    try:
        process = subprocess.Popen(
            argv,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return RunResult(ok=False, error=f"could not start claude: {exc}")

    lines: "queue.Queue" = queue.Queue()
    readers = [
        threading.Thread(target=_pump, args=(process.stdout, lines, "out"), daemon=True),
        threading.Thread(target=_pump, args=(process.stderr, lines, "err"), daemon=True),
    ]
    for reader in readers:
        reader.start()

    started = time.monotonic()
    # Reset by every line that arrives, on either stream. A turn is allowed to
    # be slow; what it is not allowed to be is *silent*, because a `claude`
    # that has wedged looks exactly like one that is thinking, and only the
    # clock can tell them apart.
    last_heard = started

    result = RunResult(ok=False, session_id=session)
    stderr_tail: list[str] = []
    open_streams = 2
    reason = ""

    while open_streams:
        if cancelled is not None and cancelled():
            reason = "cancelled"
            break
        now = time.monotonic()
        if now - started > wall_timeout:
            reason = f"the turn ran past {wall_timeout:g}s"
            break
        if now - last_heard > silence_timeout:
            reason = f"claude said nothing for {silence_timeout:g}s"
            break

        try:
            # Short, so the two clocks above are checked often rather than
            # only when something happens to arrive.
            tag, line = lines.get(timeout=1.0)
        except queue.Empty:
            continue

        last_heard = time.monotonic()

        if line is None:
            open_streams -= 1
            continue

        if tag == "err":
            text = line.strip()
            if text:
                stderr_tail.append(text)
                del stderr_tail[:-20]
            continue

        text = line.strip()
        if not text.startswith("{"):
            continue  # the launcher's banner, not the stream
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            continue

        _consume(message, result, emit, root)

    if reason:
        process.kill()
        process.wait(timeout=10)
        return RunResult(
            ok=False,
            session_id=result.session_id,
            error=reason,
            tools=result.tools,
        )

    code = process.wait(timeout=30)

    if result.summary and not result.error:
        result.ok = True
    elif not result.error:
        tail = " / ".join(stderr_tail[-3:])
        result.error = tail or f"claude exited {code} without a result"

    return result


def _consume(message: dict, result: RunResult, emit, root: Path) -> None:
    """Fold one stream-json message into the running result."""
    kind = message.get("type")

    if kind == "system":
        if message.get("subtype") == "init":
            got = message.get("session_id")
            if got:
                result.session_id = got
                emit("status", f"session {got[:8]}")
        return  # hook chatter and the rest are for the terminal, not the page

    if kind == "assistant":
        for block in (message.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                said = _clip(block.get("text", ""))
                if said:
                    emit("text", said)
            elif block.get("type") == "tool_use":
                described = describe_tool(
                    str(block.get("name", "")), block.get("input") or {}, root
                )
                result.tools.append(described)
                emit("tool", described)
            # thinking blocks are deliberately dropped: they are long, and the
            # page is a progress indicator rather than a transcript.
        return

    if kind == "result":
        result.turns = int(message.get("num_turns") or 0)
        try:
            result.cost = float(message.get("total_cost_usd") or 0.0)
        except (TypeError, ValueError):
            result.cost = 0.0
        got = message.get("session_id")
        if got:
            result.session_id = got
        if message.get("is_error") or message.get("subtype") != "success":
            result.error = _clip(
                message.get("result") or message.get("subtype") or "the turn failed"
            )
        else:
            result.summary = _clip(message.get("result") or "done", 400)
