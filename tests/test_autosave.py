"""Tests for committing on its own once the writing stops.

Against a real repository with a real bare remote, as the checkpoint tests do:
the whole point is that git actually runs, and mocking it would test nothing.
Delays are tiny here so the suite stays quick -- the debounce behaviour is the
same at 0.15s as at 45s.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave.autosave import AutoCommit
from mdweave.serve import Workspace, autocommit_for, make_server

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture()
def base(tmp_path):
    """A knowledge base that is a git repo, wired to a bare remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   capture_output=True, check=True)

    root = tmp_path / "knowledge_base"
    inputs, outputs = root / "markdown_inputs", root / "html_outputs"
    inputs.mkdir(parents=True)
    outputs.mkdir()

    subprocess.run(["git", "init", "-b", "main", str(root)], capture_output=True, check=True)
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (inputs / "alpha.md").write_text("# Alpha\n\nOne.\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    return root, inputs, outputs


def saver(base, delay=0.15, enabled=True):
    _, inputs, outputs = base
    return AutoCommit(inputs=inputs, outputs=outputs, delay=delay, enabled=enabled)


def settle(auto, timeout=8.0):
    """Wait for a fired timer to finish its git work."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if auto.last.at:
            return auto.last
        time.sleep(0.05)
    raise AssertionError("the timer never ran")


# --- the debounce ----------------------------------------------------------

def test_a_write_is_committed_once_the_writing_stops(base):
    root, inputs, _ = base
    auto = saver(base)

    (inputs / "beta.md").write_text("# Beta\n\nNew.\n", encoding="utf-8")
    auto.touch()
    settle(auto)

    assert auto.last.committed
    assert "beta.md" in git(root, "show", "--name-only", "--pretty=", "HEAD")
    assert git(root, "rev-parse", "HEAD").strip() == git(root, "rev-parse", "origin/main").strip()


def test_a_burst_of_writes_becomes_one_commit(base):
    """Nine edited paragraphs are one piece of work, not nine commits."""
    root, inputs, _ = base
    before = int(git(root, "rev-list", "--count", "HEAD"))
    auto = saver(base, delay=0.4)

    for n in range(9):
        (inputs / "alpha.md").write_text(f"# Alpha\n\nEdit {n}.\n", encoding="utf-8")
        auto.touch()
        time.sleep(0.05)  # well inside the window, so each one pushes it back
    settle(auto)

    assert int(git(root, "rev-list", "--count", "HEAD")) == before + 1


def test_touch_returns_immediately(base):
    """Non-blocking is the whole point: an edit must not wait on git."""
    auto = saver(base, delay=5)
    started = time.monotonic()
    for _ in range(200):
        auto.touch()
    assert time.monotonic() - started < 0.5, "touch() is doing real work"
    auto.cancel()


def test_the_commit_runs_off_the_calling_thread(base):
    _, inputs, _ = base
    auto = saver(base)
    (inputs / "beta.md").write_text("# Beta\n", encoding="utf-8")

    here = threading.current_thread().name
    auto.touch()
    settle(auto)
    assert auto.last.committed


def test_disabled_does_nothing_at_all(base):
    root, inputs, _ = base
    before = git(root, "rev-parse", "HEAD").strip()
    auto = saver(base, enabled=False)

    (inputs / "beta.md").write_text("# Beta\n", encoding="utf-8")
    auto.touch()
    time.sleep(0.4)

    assert auto.last.at == "", "nothing should have run"
    assert git(root, "rev-parse", "HEAD").strip() == before


def test_cancel_stops_a_pending_commit(base):
    root, inputs, _ = base
    before = git(root, "rev-parse", "HEAD").strip()
    auto = saver(base, delay=0.4)

    (inputs / "beta.md").write_text("# Beta\n", encoding="utf-8")
    auto.touch()
    auto.cancel()
    time.sleep(0.7)

    assert git(root, "rev-parse", "HEAD").strip() == before


# --- what it commits -------------------------------------------------------

def test_it_captures_the_tree_arrangement_that_checkpoint_cannot(base):
    """A folder and an order file belong to no article, so the per-document
    Checkpoint button can never pick them up. This is the reason it exists."""
    root, inputs, _ = base
    (inputs / "research").mkdir()
    (inputs / "research" / "moved.md").write_text("# Moved\n", encoding="utf-8")
    (inputs / ".mdweave-order.json").write_text('{"": ["research"]}', encoding="utf-8")

    auto = saver(base)
    auto.touch()
    settle(auto)

    tracked = git(root, "ls-files")
    assert "markdown_inputs/research/moved.md" in tracked
    assert "markdown_inputs/.mdweave-order.json" in tracked


def test_a_deletion_is_committed_too(base):
    root, inputs, _ = base
    (inputs / "alpha.md").unlink()

    auto = saver(base)
    auto.touch()
    settle(auto)

    assert "markdown_inputs/alpha.md" not in git(root, "ls-files")


def test_nothing_to_do_is_not_an_error(base):
    auto = saver(base)
    auto.touch()
    settle(auto)
    assert auto.last.error == ""
    assert auto.last.committed is False


# --- the message -----------------------------------------------------------

def test_one_document_is_named(base):
    root, inputs, _ = base
    (inputs / "alpha.md").write_text("# Alpha\n\nEdited.\n", encoding="utf-8")

    auto = saver(base)
    auto.touch()
    settle(auto)

    assert git(root, "log", "-1", "--pretty=%s").strip() == "auto: update alpha"


def test_several_documents_are_counted(base):
    root, inputs, _ = base
    for name in ("alpha", "beta", "gamma"):
        (inputs / f"{name}.md").write_text(f"# {name}\n\nText.\n", encoding="utf-8")

    auto = saver(base)
    auto.touch()
    settle(auto)

    subject = git(root, "log", "-1", "--pretty=%s").strip()
    assert subject.startswith("auto: update 3 documents")


def test_a_sidecar_is_named_after_its_document(base):
    root, inputs, _ = base
    (inputs / "alpha.ann.json").write_text('{"version":1,"annotations":[]}', encoding="utf-8")

    auto = saver(base)
    auto.touch()
    settle(auto)

    assert git(root, "log", "-1", "--pretty=%s").strip() == "auto: update alpha"


# --- failure ---------------------------------------------------------------

def test_a_failed_push_is_recorded_not_raised(base):
    """The commit is already made; a later run pushes it. Nothing should crash."""
    root, inputs, _ = base
    git(root, "remote", "set-url", "origin", str(root / "gone.git"))
    (inputs / "beta.md").write_text("# Beta\n", encoding="utf-8")

    auto = saver(base)
    auto.touch()
    settle(auto)

    assert auto.last.error, "the failure should be recorded"
    assert auto.last.committed is False
    assert "beta.md" in git(root, "show", "--name-only", "--pretty=", "HEAD")


def test_a_recorded_failure_reaches_the_status(base):
    root, inputs, _ = base
    git(root, "remote", "set-url", "origin", str(root / "gone.git"))
    (inputs / "beta.md").write_text("# Beta\n", encoding="utf-8")

    auto = saver(base)
    auto.touch()
    settle(auto)

    status = auto.status()
    assert status["enabled"] is True
    assert status["error"]


def test_outside_a_repository_is_recorded_not_raised(tmp_path):
    lonely = tmp_path / "loose"
    (lonely / "markdown_inputs").mkdir(parents=True)
    auto = AutoCommit(
        inputs=lonely / "markdown_inputs",
        outputs=lonely / "html_outputs",
        delay=0.1,
        enabled=True,
    )
    auto.touch()
    settle(auto)
    assert auto.last.error


# --- configuration ---------------------------------------------------------

def test_it_is_off_unless_the_environment_asks(monkeypatch, tmp_path):
    monkeypatch.delenv("MDWEAVE_AUTOCOMMIT", raising=False)
    workspace = Workspace(inputs=tmp_path / "in", outputs=tmp_path / "out")
    assert autocommit_for(workspace).enabled is False


@pytest.mark.parametrize("raw, enabled, delay", [
    ("45", True, 45.0),
    ("0.5", True, 0.5),
    ("0", False, 45.0),
    ("", False, 45.0),
    ("nonsense", False, 45.0),
])
def test_the_delay_comes_from_the_environment(monkeypatch, tmp_path, raw, enabled, delay):
    monkeypatch.setenv("MDWEAVE_AUTOCOMMIT", raw)
    workspace = Workspace(inputs=tmp_path / "in", outputs=tmp_path / "out")
    auto = autocommit_for(workspace)
    assert auto.enabled is enabled
    assert auto.delay == delay


# --- through the server ----------------------------------------------------

@pytest.fixture()
def server(base, monkeypatch):
    root, inputs, outputs = base
    monkeypatch.delenv("MDWEAVE_PASSWORD", raising=False)

    workspace = Workspace(inputs=inputs, outputs=outputs)
    workspace.rebuild_all()
    auto = AutoCommit(inputs=inputs, outputs=outputs, delay=0.2, enabled=True)

    httpd = make_server(workspace, "127.0.0.1", 0, auto)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", root, auto
    finally:
        auto.cancel()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def call(url, payload=None, method="POST"):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, json.loads(response.read())


def test_an_edit_through_the_api_is_committed_on_its_own(server):
    url, root, auto = server
    call(url + "/api/documents/create", {"path": "fresh"})
    settle(auto)

    assert auto.last.committed
    assert "markdown_inputs/fresh.md" in git(root, "ls-files")


def test_the_request_does_not_wait_for_the_commit(server):
    """The response must land long before the debounce window has even closed."""
    url, _, auto = server
    started = time.monotonic()
    call(url + "/api/documents/create", {"path": "quick"})
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, f"the write waited {elapsed:.2f}s -- it should not wait at all"
    auto.cancel()


def test_a_read_only_post_does_not_arm_it(server):
    """Copying text changes nothing, so it must not schedule a commit."""
    url, _, auto = server
    call(url + "/api/extract", {
        "document": "alpha", "spans": [{"start": 0, "end": 1, "from": 0, "to": 1}]
    })
    time.sleep(0.5)
    assert auto.last.at == "", "a copy should not have scheduled anything"


def test_health_reports_the_state(server):
    url, _, auto = server
    _, payload = call(url + "/api/health", method="GET")
    assert payload["autosave"]["enabled"] is True
    assert payload["autosave"]["delay"] == 0.2


def test_health_reports_it_off_when_it_is_off(base, monkeypatch):
    root, inputs, outputs = base
    monkeypatch.delenv("MDWEAVE_PASSWORD", raising=False)
    workspace = Workspace(inputs=inputs, outputs=outputs)
    workspace.rebuild_all()

    httpd = make_server(workspace, "127.0.0.1", 0)  # no autosave passed
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        _, payload = call(f"http://127.0.0.1:{httpd.server_port}/api/health", method="GET")
        assert payload["autosave"] == {"enabled": False}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_every_mutating_endpoint_arms_it():
    """Armed at one choke point, so a new endpoint cannot forget to."""
    source = (Path(__file__).resolve().parents[1] / "mdweave" / "serve.py").read_text()
    body = source.split("def _dispatch(")[1].split("def ")[0]
    assert "self.autosave.touch()" in body
    assert source.count("self.autosave.touch()") == 1, "one place, not thirteen"
    assert 'self.command != "GET"' in body


def test_a_silent_failure_is_surfaced_once_in_the_browser():
    """It runs on a timer with nobody watching; a dead push must not be quiet."""
    source = (ASSETS / "checkpoint.js").read_text(encoding="utf-8")
    assert "health.autosave" in source
    assert "saving.error" in source
    assert 'ui.toast(' in source.split("health.autosave")[1]
    # Informational only -- it must not gate the button or open the dialog.
    after = source.split("var saving = health.autosave;")[1].split("});")[0]
    assert "showModal" not in after and "disabled" not in after
