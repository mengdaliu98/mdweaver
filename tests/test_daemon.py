"""Tests for `mdweave start` / `mdweave stop` and the root route.

The lifecycle bits that spawn a real process are exercised end to end -- the
whole point of this layer is that a server outlives the command that started
it, which a mock cannot tell you.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave import cli, daemon
from mdweave.serve import Workspace, make_server


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def contents(tmp_path):
    """A knowledge base with two documents, newest-last alphabetically."""
    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "alpha.md").write_text("# Alpha\n\nFirst.\n", encoding="utf-8")
    (inputs / "zulu.md").write_text("# Zulu\n\nLast.\n", encoding="utf-8")
    return inputs, outputs


# --- the root route --------------------------------------------------------

@pytest.fixture()
def server(contents):
    inputs, outputs = contents
    workspace = Workspace(inputs=inputs, outputs=outputs)
    workspace.rebuild_all()

    httpd = make_server(workspace, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", workspace
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_root_redirects_to_the_first_document(server):
    """`mdweave start` opens the bare URL, so it has to land somewhere real."""
    base, _ = server
    request = urllib.request.Request(base + "/", method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        assert response.url.endswith("/alpha.html")


def test_root_does_not_leak_a_directory_listing(server):
    base, _ = server
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        body = response.read().decode()
    assert "Directory listing" not in body
    assert 'data-document="alpha"' in body


def test_root_with_no_documents_says_so(tmp_path):
    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    inputs.mkdir()
    outputs.mkdir()

    httpd = make_server(Workspace(inputs=inputs, outputs=outputs), "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/", timeout=10)
        assert caught.value.code == 404
        assert "no documents" in json.loads(caught.value.read())["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_health_reports_the_pid(server):
    """`mdweave stop` asks the server who it is rather than guessing from ps."""
    import os

    base, _ = server
    with urllib.request.urlopen(base + "/api/health", timeout=10) as response:
        assert json.loads(response.read())["pid"] == os.getpid()


# --- start and stop --------------------------------------------------------

@pytest.fixture()
def port(tmp_path, monkeypatch):
    """A free port, with pid/log files kept out of the real /tmp."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    chosen = free_port()
    yield chosen
    daemon.stop(chosen)  # never leave a process behind, even on failure


def test_start_renders_serves_and_survives(contents, port, capsys):
    inputs, outputs = contents

    assert cli.main(["start", "-i", str(inputs), "-o", str(outputs),
                     "--port", str(port), "--no-open"]) == 0

    assert (outputs / "alpha.html").exists(), "start must render before serving"
    info = daemon.health("127.0.0.1", port)
    assert info and info["documents"] == ["alpha", "zulu"]
    assert f"http://127.0.0.1:{port}/" in capsys.readouterr().out


def test_start_is_idempotent(contents, port):
    inputs, outputs = contents
    argv = ["start", "-i", str(inputs), "-o", str(outputs),
            "--port", str(port), "--no-open"]

    assert cli.main(argv) == 0
    first = daemon.health("127.0.0.1", port)["pid"]
    assert cli.main(argv) == 0
    assert daemon.health("127.0.0.1", port)["pid"] == first, "must not restart a current server"


def test_start_picks_up_a_document_added_since(contents, port):
    """Rendering happens on every start, running server or not."""
    inputs, outputs = contents
    argv = ["start", "-i", str(inputs), "-o", str(outputs),
            "--port", str(port), "--no-open"]
    assert cli.main(argv) == 0

    (inputs / "bravo.md").write_text("# Bravo\n\nNew.\n", encoding="utf-8")
    assert cli.main(argv) == 0
    assert (outputs / "bravo.html").exists()


def test_start_restarts_a_stale_server(contents, port, monkeypatch):
    inputs, outputs = contents
    argv = ["start", "-i", str(inputs), "-o", str(outputs),
            "--port", str(port), "--no-open"]

    assert cli.main(argv) == 0
    before = daemon.health("127.0.0.1", port)["pid"]

    # Pretend the source moved on under the running process.
    monkeypatch.setattr(daemon, "is_stale", lambda info: True)
    assert cli.main(argv) == 0

    after = daemon.health("127.0.0.1", port)["pid"]
    assert after != before, "a server running old code must be replaced"


def test_start_reports_a_missing_knowledge_base(tmp_path, port, capsys):
    missing = tmp_path / "nowhere"
    assert cli.main(["start", "-i", str(missing), "-o", str(tmp_path / "out"),
                     "--port", str(port), "--no-open"]) == 1
    assert "no markdown directory" in capsys.readouterr().err


def test_stop_shuts_it_down(contents, port):
    inputs, outputs = contents
    assert cli.main(["start", "-i", str(inputs), "-o", str(outputs),
                     "--port", str(port), "--no-open"]) == 0
    assert daemon.health("127.0.0.1", port) is not None

    assert cli.main(["stop", "--port", str(port)]) == 0
    assert daemon.health("127.0.0.1", port) is None


def test_stop_is_idempotent(port, capsys):
    assert cli.main(["stop", "--port", str(port)]) == 0
    assert "nothing running" in capsys.readouterr().out


def test_stop_clears_the_pidfile(contents, port):
    inputs, outputs = contents
    cli.main(["start", "-i", str(inputs), "-o", str(outputs),
              "--port", str(port), "--no-open"])
    assert daemon.pidfile(port).exists()

    cli.main(["stop", "--port", str(port)])
    assert not daemon.pidfile(port).exists()


def test_stop_ignores_a_stale_pidfile(port):
    """A pid that has been recycled must not be signalled."""
    daemon.pidfile(port).write_text("999999999", encoding="utf-8")
    assert daemon.stop(port) is None
    assert not daemon.pidfile(port).exists()


def test_stop_falls_back_to_the_pidfile(contents, port, monkeypatch):
    """A process too wedged to answer health can still be stopped."""
    inputs, outputs = contents
    cli.main(["start", "-i", str(inputs), "-o", str(outputs),
              "--port", str(port), "--no-open"])
    recorded = int(daemon.pidfile(port).read_text())

    monkeypatch.setattr(daemon, "health", lambda *a, **k: None)
    assert daemon.stop(port) == recorded

    monkeypatch.undo()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and daemon.health("127.0.0.1", port):
        time.sleep(0.1)
    assert daemon.health("127.0.0.1", port) is None


# --- where the documents live ---------------------------------------------

def test_contents_root_prefers_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MDWEAVE_CONTENTS", str(tmp_path))
    assert cli.contents_root() == tmp_path
    assert cli.default_indir() == tmp_path / "markdown_inputs"


def test_contents_root_finds_the_sibling_checkout(monkeypatch):
    """`mdweave start` has to work from any directory, not just the parent."""
    monkeypatch.delenv("MDWEAVE_CONTENTS", raising=False)
    expected = Path(cli.__file__).resolve().parents[2] / "knowledge_base"
    if not expected.is_dir():
        pytest.skip("no sibling knowledge_base checkout on this machine")
    monkeypatch.chdir("/")
    assert cli.contents_root() == expected
