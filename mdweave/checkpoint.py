"""Committing and pushing a single document.

The knowledge base is a git repository, so "save this properly" means a commit
and a push. The scope is deliberately one document: its markdown, its
annotation sidecar, and its rendered page -- never the shared assets, and never
somebody else's article that happens to be dirty at the same moment.

Every command goes through `_git`, which takes an argv list. Nothing is ever
handed to a shell, so a commit message is data and cannot become syntax.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

TIMEOUT = 15  # seconds for a local command
PUSH_TIMEOUT = 90  # the network one


class GitError(RuntimeError):
    """A git command failed; the message is what git said."""


@dataclass
class Checkpoint:
    committed: bool
    pushed: bool
    revision: str
    detail: str


def _git(repo: Path, *args: str, timeout: int = TIMEOUT) -> str:
    """Run one git command in `repo` and return its stdout.

    argv, never a shell string: the commit message arrives from the browser.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise GitError("git is not installed on this machine")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {timeout}s")

    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        raise GitError(detail or f"git {args[0]} failed ({done.returncode})")
    return done.stdout


def repo_root(inside: Path) -> Path:
    """The repository containing `inside`."""
    try:
        out = _git(inside, "rev-parse", "--show-toplevel")
    except GitError as exc:
        raise GitError(f"{inside} is not inside a git repository: {exc}")
    return Path(out.strip())


def _tracked_or_present(repo: Path, paths: list[Path]) -> list[str]:
    """Paths worth naming to git: they exist, or git already knows them.

    A deleted file still needs staging so the deletion is recorded; one that
    never existed would make `git add` fail and take the whole commit with it.
    """
    known = set()
    for path in paths:
        relative = path.relative_to(repo).as_posix()
        if path.exists():
            known.add(relative)
            continue
        try:
            if _git(repo, "ls-files", "--error-unmatch", "--", relative):
                known.add(relative)
        except GitError:
            pass  # git has never heard of it and it is not there: nothing to do
    return sorted(known)


def checkpoint(repo: Path, paths: list[Path], message: str) -> Checkpoint:
    """Stage, commit and push exactly `paths`.

    The pathspec is repeated on `commit` as well as `add` so that anything else
    already sitting in the index -- another document mid-edit, a stray asset --
    stays out of this commit.
    """
    message = message.strip()
    if not message:
        raise GitError("a checkpoint needs a message")

    targets = _tracked_or_present(repo, paths)
    if not targets:
        raise GitError("nothing to check point: no such files in this repository")

    _git(repo, "add", "--", *targets)

    staged = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", *targets],
        capture_output=True,
        timeout=TIMEOUT,
    )
    committed = staged.returncode != 0  # non-zero means there is a difference

    if committed:
        _git(repo, "commit", "-m", message, "--", *targets)

    # Push regardless: a previous checkpoint may have committed and then failed
    # to reach the remote, and this is the natural moment to catch up.
    push = _git(repo, "push", timeout=PUSH_TIMEOUT)
    revision = _git(repo, "rev-parse", "--short", "HEAD").strip()

    return Checkpoint(
        committed=committed,
        pushed=True,
        revision=revision,
        detail=(push.strip() or "pushed"),
    )
