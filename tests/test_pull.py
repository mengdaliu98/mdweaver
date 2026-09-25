"""Taking in what another machine wrote.

The container pushes what it writes and, until this existed, only fetched at
boot -- so writing on a laptop and reading on the deployed instance was one
directional. Two ways in now: a timer, and a button.

Everything here runs against two real checkouts of one bare remote, because
the interesting part is not the timer. It is the merge policy underneath it,
which has to bring in another machine's prose, rebuild the pages over it, and
refuse rather than guess when two people wrote the same paragraph.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave.serve import Workspace, make_server, puller_for
from mdweave.sync import Pull


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture()
def pair(tmp_path):
    """Two checkouts of one remote: `here` and `elsewhere`."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   capture_output=True, check=True)

    here = tmp_path / "here"
    (here / "markdown_inputs").mkdir(parents=True)
    (here / "html_outputs").mkdir()
    (here / "markdown_inputs" / "seed.md").write_text("# Seed\n\nhi\n", encoding="utf-8")

    subprocess.run(["git", "init", "-b", "main", str(here)], capture_output=True, check=True)
    git(here, "config", "user.email", "here@example.com")
    git(here, "config", "user.name", "Here")
    git(here, "add", "-A")
    git(here, "commit", "-m", "initial")
    git(here, "remote", "add", "origin", str(remote))
    git(here, "push", "-u", "origin", "main")

    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "clone", str(remote), str(elsewhere)],
                   capture_output=True, check=True)
    git(elsewhere, "config", "user.email", "there@example.com")
    git(elsewhere, "config", "user.name", "Elsewhere")

    workspace = Workspace(inputs=here / "markdown_inputs", outputs=here / "html_outputs")
    workspace.rebuild_all()
    return workspace, here, elsewhere


def write_elsewhere(elsewhere: Path, name: str, body: str) -> None:
    (elsewhere / "markdown_inputs" / f"{name}.md").write_text(body, encoding="utf-8")
    git(elsewhere, "add", "-A")
    git(elsewhere, "commit", "-m", f"elsewhere: {name}")
    git(elsewhere, "push", "origin", "main")


def a_pull(workspace: Workspace, **kw) -> Pull:
    return Pull(
        inputs=workspace.inputs,
        outputs=workspace.outputs,
        rebuild=workspace.rebuild_all,
        enabled=True,
        **kw,
    )


# --- the merge --------------------------------------------------------------

def test_a_document_written_elsewhere_arrives_and_is_rendered(pair):
    workspace, _, elsewhere = pair
    write_elsewhere(elsewhere, "from_the_laptop", "# From the laptop\n\nWritten there.\n")

    outcome = a_pull(workspace).run_now()

    assert outcome.merged and not outcome.error
    assert "from_the_laptop" in workspace.documents()
    # Rendered, not merely fetched -- a document nobody can open has not
    # really arrived.
    assert (workspace.outputs / "from_the_laptop.html").exists()


def test_every_sidebar_is_rebuilt_over_what_arrived(pair):
    """Each page bakes its own copy of the tree, so without the rebuild the
    documents already here would link to nothing new."""
    workspace, _, elsewhere = pair
    write_elsewhere(elsewhere, "from_the_laptop", "# From the laptop\n\nThere.\n")

    a_pull(workspace).run_now()

    assert "from_the_laptop" in (workspace.outputs / "seed.html").read_text(encoding="utf-8")


def test_nothing_to_take_is_not_an_error(pair):
    workspace, _, _ = pair
    outcome = a_pull(workspace).run_now()
    assert not outcome.merged and not outcome.error


def test_work_written_here_is_not_disturbed(pair):
    """A pull is not a reset. The local commit has to survive it."""
    workspace, here, elsewhere = pair
    (workspace.inputs / "mine.md").write_text("# Mine\n\nWritten here.\n", encoding="utf-8")
    git(here, "add", "-A")
    git(here, "commit", "-m", "mine")
    write_elsewhere(elsewhere, "theirs", "# Theirs\n\nWritten there.\n")

    assert a_pull(workspace).run_now().merged

    assert {"seed", "mine", "theirs"} <= set(workspace.documents())


def test_a_conflict_in_the_prose_is_reported_rather_than_guessed(pair):
    """The one thing a background job must not do is pick a side in somebody's
    writing. `reconcile` aborts, and the checkout is left as it was found."""
    workspace, here, elsewhere = pair
    write_elsewhere(elsewhere, "seed", "# Seed\n\nTheir sentence.\n")
    (workspace.inputs / "seed.md").write_text("# Seed\n\nOur sentence.\n", encoding="utf-8")
    git(here, "add", "-A")
    git(here, "commit", "-m", "ours")

    outcome = a_pull(workspace).run_now()

    assert outcome.error and "by hand" in outcome.error
    assert "Our sentence." in workspace.source_of("seed")
    assert not (here / ".git" / "MERGE_HEAD").exists(), "left mid-merge"


def test_an_error_does_not_stop_the_polling(pair, monkeypatch):
    """A poller that gives up on its first network blip is a poller that
    stops, and nobody notices until they wonder why nothing is arriving."""
    from mdweave import checkpoint as git_checkpoint

    workspace, _, _ = pair
    puller = a_pull(workspace)

    def explode(*_args, **_kw):
        raise git_checkpoint.GitError("the network is off")

    monkeypatch.setattr(git_checkpoint, "reconcile", explode)
    assert puller.run_now().error == "the network is off"

    monkeypatch.undo()
    assert not puller.run_now().error, "the next tick has to work"


def test_anything_unexpected_is_caught_too(pair, monkeypatch):
    """It runs on a timer thread with nobody to catch it; an unhandled error
    would take the thread out silently and end the polling for good."""
    from mdweave import checkpoint as git_checkpoint

    workspace, _, _ = pair
    monkeypatch.setattr(
        git_checkpoint, "reconcile",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("something odd")),
    )
    assert "ValueError" in a_pull(workspace).run_now().error


def test_the_lock_is_shared_with_the_auto_commit(pair):
    """Two git processes in one repository is an index.lock error, and a fetch
    landing between a commit and its push strands the commit."""
    from mdweave.serve import autocommit_for

    workspace, _, _ = pair
    autosave = autocommit_for(workspace)
    puller = puller_for(workspace, autosave.lock)
    assert puller.lock is autosave.lock


# --- the setting ------------------------------------------------------------

def test_polling_is_off_unless_asked_for(pair, monkeypatch):
    """Merging somebody else's work into a checkout unasked is not a default."""
    workspace, _, _ = pair
    monkeypatch.delenv("MDWEAVE_PULL", raising=False)
    assert puller_for(workspace, threading.Lock()).enabled is False

    monkeypatch.setenv("MDWEAVE_PULL", "180")
    on = puller_for(workspace, threading.Lock())
    assert on.enabled and on.interval == 180


@pytest.mark.parametrize("raw", ["", "nonsense", "0", "-5"])
def test_a_setting_that_is_not_a_number_of_seconds_leaves_it_off(pair, monkeypatch, raw):
    workspace, _, _ = pair
    monkeypatch.setenv("MDWEAVE_PULL", raw)
    assert puller_for(workspace, threading.Lock()).enabled is False


def test_the_container_turns_it_on():
    """The machine that had no other way to hear about a change."""
    start = (Path(__file__).resolve().parents[1] / "deploy" / "start.sh").read_text()
    assert 'MDWEAVE_PULL="${MDWEAVE_PULL:-180}"' in start


# --- the button -------------------------------------------------------------

@pytest.fixture()
def served(pair):
    workspace, here, elsewhere = pair
    httpd = make_server(workspace, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", workspace, elsewhere
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def call(base, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_button_takes_the_latest_and_hands_back_a_panel(served):
    base, workspace, elsewhere = served
    write_elsewhere(elsewhere, "from_the_laptop", "# From the laptop\n\nThere.\n")

    status, payload = call(base, "POST", "/api/pull", {"page": "seed"})

    assert status == 200 and payload["merged"] is True
    assert "from_the_laptop" in workspace.documents()
    # The panel comes back so the tree updates without a reload, like every
    # other tree change.
    assert "from_the_laptop" in payload["sidebar"]


def test_pressing_it_with_nothing_to_take_says_so(served):
    base, _, _ = served
    status, payload = call(base, "POST", "/api/pull", {"page": "seed"})
    assert status == 200 and payload["merged"] is False


def test_a_conflict_reaches_the_reader_through_the_button(served):
    base, workspace, elsewhere = served
    write_elsewhere(elsewhere, "seed", "# Seed\n\nTheirs.\n")
    (workspace.inputs / "seed.md").write_text("# Seed\n\nOurs.\n", encoding="utf-8")
    git(workspace.inputs.parent, "add", "-A")
    git(workspace.inputs.parent, "commit", "-m", "ours")

    status, payload = call(base, "POST", "/api/pull", {"page": "seed"})

    assert status == 502 and "by hand" in payload["error"]


def test_health_says_whether_there_is_a_remote_at_all(served, tmp_path):
    base, _, _ = served
    with urllib.request.urlopen(base + "/api/health", timeout=10) as response:
        health = json.load(response)
    assert health["remote"] is True
    assert health["pull"]["enabled"] is False  # not set in this fixture
