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
    sent: int = 0  # commits the push actually carried to the remote


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

        if path.is_dir():
            # A directory is only a usable pathspec once something under it
            # exists or is tracked. Naming an empty one makes `git commit`
            # fail with "did not match any file(s) known to git", and makes
            # the staged-check above it read that error as "there are
            # changes" -- so an empty html_outputs took the whole run down.
            has_file = any(child.is_file() for child in path.rglob("*"))
            if has_file or _tracked_under(repo, relative):
                known.add(relative)
            continue

        if path.exists():
            known.add(relative)
            continue
        try:
            if _git(repo, "ls-files", "--error-unmatch", "--", relative):
                known.add(relative)
        except GitError:
            pass  # git has never heard of it and it is not there: nothing to do
    return sorted(known)


def _tracked_under(repo: Path, relative: str) -> bool:
    try:
        return bool(_git(repo, "ls-files", "--", relative).strip())
    except GitError:
        return False


def _is_non_fast_forward(exc: GitError) -> bool:
    """Did the push lose a race, rather than fail for some other reason?"""
    said = str(exc)
    return "non-fast-forward" in said or "fetch first" in said


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def reconcile(repo: Path, generated: Path | None = None) -> bool:
    """Take what the remote has, keeping both sides. True if anything came in.

    Two writers, one branch. A laptop and a container both commit here, and
    whichever loses the race is told `non-fast-forward` -- and then told it
    again on every attempt after that, because nothing about the situation
    changes on its own. A whole session's work sits on one machine looking
    saved. That is the failure this exists to end.

    Merging is the only move that keeps both sides; a rebase would rewrite
    commits that are already on the remote, and a reset would throw one side
    away. Generated pages conflict every single time, because each writer
    rebuilds them from its own prose -- but they are not work, so they are
    resolved by taking ours and letting the next render settle the content.

    A conflict under the prose is different in kind. Two people wrote two
    things and only one of them knows what was meant, so the merge is aborted
    and the checkout left exactly as it was found. Silently picking a side
    there would lose writing, which is the one thing this must never do.
    """
    branch = _current_branch(repo)

    # A shallow clone -- what the container starts from -- may not reach back
    # to a commit both sides share, and a merge with no merge base fails.
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        try:
            _git(repo, "fetch", "--unshallow", "origin", branch, timeout=PUSH_TIMEOUT)
        except GitError:
            _git(repo, "fetch", "--depth=1000", "origin", branch, timeout=PUSH_TIMEOUT)
    else:
        _git(repo, "fetch", "origin", branch, timeout=PUSH_TIMEOUT)

    behind = _git(repo, "rev-list", "--count", f"HEAD..FETCH_HEAD").strip()
    if behind == "0":
        return False  # the rejection was not about being behind after all

    try:
        _git(repo, "merge", "--no-edit", "FETCH_HEAD", timeout=PUSH_TIMEOUT)
        return True
    except GitError as exc:
        stuck = [
            line.strip()
            for line in _git(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
            if line.strip()
        ]
        if not stuck:
            raise GitError(f"could not merge the remote: {exc}")

        prefix = f"{generated.relative_to(repo).as_posix()}/" if generated else None
        prose = [p for p in stuck if not (prefix and p.startswith(prefix))]
        if prose:
            _git(repo, "merge", "--abort")
            raise GitError(
                "the remote and this machine both changed "
                + ", ".join(prose[:3])
                + (f" and {len(prose) - 3} more" if len(prose) > 3 else "")
                + " -- merge it by hand; nothing here has been changed"
            )

        # Only generated pages. Take ours; the render after this rewrites them
        # from the prose that just arrived anyway.
        _git(repo, "checkout", "--ours", "--", prefix.rstrip("/"))
        _git(repo, "add", "--", prefix.rstrip("/"))
        _git(repo, "commit", "--no-edit")
        return True


def checkpoint(
    repo: Path,
    paths: list[Path],
    message: str,
    *,
    generated: Path | None = None,
    on_merge=None,
) -> Checkpoint:
    """Stage, commit and push exactly `paths`.

    The pathspec is repeated on `commit` as well as `add` so that anything else
    already sitting in the index -- another document mid-edit, a stray asset --
    stays out of this commit.

    `generated` names the directory whose contents are rebuilt rather than
    written, and `on_merge` is called after the remote's work has been taken
    in, to rebuild it. Both are only used when a push is rejected for being
    behind -- see `reconcile`.
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
    # to reach the remote, and this is the natural moment to catch up. Counted
    # first, because after the push there is nothing left to count -- and
    # "committed nothing" is not the same as "sent nothing", which is exactly
    # what a failed push followed by a retry looks like.
    sent = _unpushed(repo)
    try:
        push = _git(repo, "push", timeout=PUSH_TIMEOUT)
    except GitError as exc:
        if not _is_non_fast_forward(exc):
            raise
        # Someone else pushed between our last fetch and now. Take their work,
        # rebuild the pages over the prose that arrived, and try once more --
        # once, because a second rejection means a writer busy enough that
        # retrying in a loop would be a spin rather than a recovery.
        reconcile(repo, generated)
        if on_merge is not None:
            on_merge()
            targets = _tracked_or_present(repo, paths)
            _git(repo, "add", "--", *targets)
            if subprocess.run(
                ["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", *targets],
                capture_output=True,
                timeout=TIMEOUT,
            ).returncode != 0:
                _git(repo, "commit", "-m", f"{message} (after merging the remote)",
                     "--", *targets)
                committed = True
        sent = _unpushed(repo)
        push = _git(repo, "push", timeout=PUSH_TIMEOUT)
    revision = _git(repo, "rev-parse", "--short", "HEAD").strip()

    return Checkpoint(
        committed=committed,
        pushed=True,
        revision=revision,
        detail=(push.strip() or "pushed"),
        sent=sent,
    )


def _unpushed(repo: Path) -> int:
    """How many commits the next push will carry.

    Zero when there is no upstream to compare against -- an unknown count is
    better reported as nothing than guessed at.
    """
    try:
        return int(_git(repo, "rev-list", "--count", "@{u}..HEAD").strip() or 0)
    except (GitError, ValueError):
        return 0
