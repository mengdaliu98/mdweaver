"""Tests for committing and pushing a single document.

A real repository with a real (bare, local) remote, so `git push` is genuinely
exercised rather than mocked out. What matters most here is the negative:
a checkpoint of one article must not sweep up anything else.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave import checkpoint as git_checkpoint
from mdweave.serve import Workspace, make_server

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"
TEMPLATES = Path(__file__).resolve().parents[1] / "mdweave" / "templates"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture()
def repo(tmp_path):
    """A knowledge base that is a git repo, wired to a bare remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   capture_output=True, check=True)

    root = tmp_path / "knowledge_base"
    (root / "markdown_inputs").mkdir(parents=True)
    (root / "html_outputs" / "assets").mkdir(parents=True)

    subprocess.run(["git", "init", "-b", "main", str(root)], capture_output=True, check=True)
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")

    (root / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nOne.\n", encoding="utf-8")
    (root / "markdown_inputs" / "bravo.md").write_text("# Bravo\n\nTwo.\n", encoding="utf-8")
    (root / "html_outputs" / "alpha.html").write_text("<i>alpha</i>", encoding="utf-8")
    (root / "html_outputs" / "bravo.html").write_text("<i>bravo</i>", encoding="utf-8")
    (root / "html_outputs" / "assets" / "mdweave.css").write_text("/* v1 */", encoding="utf-8")

    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    return root


# --- the git layer ---------------------------------------------------------

def test_repo_root_is_found_from_a_subdirectory(repo):
    assert git_checkpoint.repo_root(repo / "markdown_inputs") == repo.resolve()


def test_repo_root_outside_a_repository_says_so(tmp_path):
    lonely = tmp_path / "not_a_repo"
    lonely.mkdir()
    with pytest.raises(git_checkpoint.GitError):
        git_checkpoint.repo_root(lonely)


def test_a_checkpoint_commits_and_pushes(repo):
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")

    result = git_checkpoint.checkpoint(
        repo, [repo / "markdown_inputs" / "alpha.md"], "tidy alpha"
    )

    assert result.committed and result.pushed
    assert "tidy alpha" in git(repo, "log", "-1", "--pretty=%s")
    # The remote really has it.
    assert git(repo, "rev-parse", "HEAD").strip() == git(repo, "rev-parse", "origin/main").strip()


def test_a_checkpoint_leaves_other_documents_alone(repo):
    """The whole point of naming paths: bravo is dirty and must stay dirty."""
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")
    (repo / "markdown_inputs" / "bravo.md").write_text("# Bravo\n\nAlso edited.\n", encoding="utf-8")

    git_checkpoint.checkpoint(repo, [repo / "markdown_inputs" / "alpha.md"], "alpha only")

    touched = git(repo, "show", "--name-only", "--pretty=", "HEAD").split()
    assert touched == ["markdown_inputs/alpha.md"]
    assert "bravo.md" in git(repo, "status", "--short")


def test_a_checkpoint_ignores_what_is_already_staged(repo):
    """Someone else's `git add` must not ride along."""
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")
    (repo / "html_outputs" / "assets" / "mdweave.css").write_text("/* v2 */", encoding="utf-8")
    git(repo, "add", "html_outputs/assets/mdweave.css")

    git_checkpoint.checkpoint(repo, [repo / "markdown_inputs" / "alpha.md"], "alpha only")

    touched = git(repo, "show", "--name-only", "--pretty=", "HEAD").split()
    assert "html_outputs/assets/mdweave.css" not in touched


def test_a_checkpoint_with_no_changes_still_reports_cleanly(repo):
    result = git_checkpoint.checkpoint(
        repo, [repo / "markdown_inputs" / "alpha.md"], "nothing doing"
    )
    assert result.committed is False
    assert result.pushed is True


def test_an_unpushed_commit_is_caught_up(repo):
    """Commit succeeded, push failed, try again -- the second try should push."""
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")
    git(repo, "commit", "-am", "committed but not pushed")
    assert git(repo, "rev-parse", "HEAD").strip() != git(repo, "rev-parse", "origin/main").strip()

    git_checkpoint.checkpoint(repo, [repo / "markdown_inputs" / "alpha.md"], "catch up")
    assert git(repo, "rev-parse", "HEAD").strip() == git(repo, "rev-parse", "origin/main").strip()


def test_a_deleted_file_is_still_checkpointed(repo):
    (repo / "markdown_inputs" / "alpha.md").unlink()
    result = git_checkpoint.checkpoint(
        repo, [repo / "markdown_inputs" / "alpha.md"], "remove alpha"
    )
    assert result.committed
    assert "markdown_inputs/alpha.md" not in git(repo, "ls-files")


def test_a_sidecar_that_never_existed_is_not_an_error(repo):
    """Most documents have no annotations; naming a missing file must not fail."""
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")
    result = git_checkpoint.checkpoint(
        repo,
        [
            repo / "markdown_inputs" / "alpha.md",
            repo / "markdown_inputs" / "alpha.ann.json",  # not there
        ],
        "no sidecar",
    )
    assert result.committed


def test_an_empty_message_is_refused(repo):
    with pytest.raises(git_checkpoint.GitError, match="message"):
        git_checkpoint.checkpoint(repo, [repo / "markdown_inputs" / "alpha.md"], "   ")


def test_a_push_that_fails_surfaces_what_git_said(repo):
    git(repo, "remote", "set-url", "origin", str(repo / "nowhere.git"))
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")

    with pytest.raises(git_checkpoint.GitError) as caught:
        git_checkpoint.checkpoint(repo, [repo / "markdown_inputs" / "alpha.md"], "will not push")
    assert "nowhere.git" in str(caught.value) or "repository" in str(caught.value)


def test_a_message_cannot_become_a_git_argument(repo):
    """The message is data. It reaches git through argv, never a shell."""
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")
    nasty = "; rm -rf / && git push --delete origin main #"

    git_checkpoint.checkpoint(repo, [repo / "markdown_inputs" / "alpha.md"], nasty)

    assert git(repo, "log", "-1", "--pretty=%s").strip() == nasty
    assert (repo / "markdown_inputs" / "bravo.md").exists()


# --- the endpoint ----------------------------------------------------------

@pytest.fixture()
def server(repo):
    workspace = Workspace(
        inputs=repo / "markdown_inputs", outputs=repo / "html_outputs"
    )
    workspace.rebuild_all()

    httpd = make_server(workspace, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", repo
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def call(base, path, payload):
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_endpoint_commits_the_document(server):
    base, repo = server
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nVia http.\n", encoding="utf-8")

    status, payload = call(base, "/api/checkpoint", {
        "document": "alpha", "message": "through the API",
    })

    assert status == 200 and payload["committed"] is True
    assert git(repo, "log", "-1", "--pretty=%s").strip() == "through the API"
    assert len(payload["revision"]) >= 7


def test_the_endpoint_commits_the_sidecar_and_the_page_too(server):
    base, repo = server
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")
    (repo / "markdown_inputs" / "alpha.ann.json").write_text(
        '{"version": 1, "annotations": []}', encoding="utf-8"
    )

    call(base, "/api/checkpoint", {"document": "alpha", "message": "with sidecar"})

    touched = git(repo, "show", "--name-only", "--pretty=", "HEAD").split()
    assert "markdown_inputs/alpha.md" in touched
    assert "markdown_inputs/alpha.ann.json" in touched
    assert "html_outputs/alpha.html" in touched
    assert not any(name.startswith("html_outputs/assets/") for name in touched)


def test_the_endpoint_rejects_an_unknown_document(server):
    base, _ = server
    assert call(base, "/api/checkpoint", {"document": "ghost", "message": "x"})[0] == 404


def test_the_endpoint_rejects_an_empty_message(server):
    base, _ = server
    assert call(base, "/api/checkpoint", {"document": "alpha", "message": "  "})[0] == 400


def test_a_git_failure_reaches_the_reader(server):
    base, repo = server
    git(repo, "remote", "set-url", "origin", str(repo / "gone.git"))
    (repo / "markdown_inputs" / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")

    status, payload = call(base, "/api/checkpoint", {
        "document": "alpha", "message": "doomed",
    })
    assert status == 502
    assert payload["error"], "git's own words should come back, not a generic failure"


# --- the browser side ------------------------------------------------------

def test_the_button_is_hidden_until_a_server_answers():
    template = (TEMPLATES / "document.html.j2").read_text(encoding="utf-8")
    source = (ASSETS / "checkpoint.js").read_text(encoding="utf-8")
    assert 'id="checkpoint"' in template and "hidden>" in template
    assert "button.hidden = false" in source
    assert "ui.api()" in source


def test_the_dialog_is_modal_and_centred():
    template = (TEMPLATES / "document.html.j2").read_text(encoding="utf-8")
    source = (ASSETS / "checkpoint.js").read_text(encoding="utf-8")
    assert "<dialog" in template
    assert "dialog.showModal()" in source


def test_submitting_does_not_let_the_dialog_close_itself():
    """method="dialog" would close it before the request was even sent."""
    source = (ASSETS / "checkpoint.js").read_text(encoding="utf-8")
    assert "event.preventDefault()" in source
    assert "setBusy(true)" in source


def test_a_failure_stays_in_the_dialog():
    """Closing on error would lose the message the reader just typed."""
    source = (ASSETS / "checkpoint.js").read_text(encoding="utf-8")
    body = source.split(".catch(")[1]
    assert "showError" in body
    assert "close()" not in body.split("});")[0]
