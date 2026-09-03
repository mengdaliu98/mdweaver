"""Tests for the password gate.

The server was written for loopback and says so. These cover what has to be
true before it is reachable from anywhere else: nothing readable, nothing
writable, and no git push, without the password.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request

import pytest

from mdweave import serve as serve_module
from mdweave.serve import Workspace, make_server

PASSWORD = "correct horse battery staple"


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("MDWEAVE_PASSWORD", PASSWORD)
    monkeypatch.delenv("MDWEAVE_USER", raising=False)

    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "secret.md").write_text("# Secret\n\nPrivate notes.\n", encoding="utf-8")

    workspace = Workspace(inputs=inputs, outputs=outputs)
    workspace.rebuild_all()

    httpd = make_server(workspace, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def request(base, path, method="GET", token=None, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    if token:
        headers["Authorization"] = "Basic " + token
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def token_for(user=None, password=PASSWORD):
    raw = f"{'mdweave' if user is None else user}:{password}".encode()
    return base64.b64encode(raw).decode()


# --- nothing without the password ------------------------------------------

@pytest.mark.parametrize(
    "method, path, payload",
    [
        ("GET", "/secret.html", None),
        ("GET", "/", None),
        ("GET", "/api/health", None),
        ("GET", "/api/annotations?document=secret", None),
        ("GET", "/api/block?document=secret&start=0&end=1", None),
        ("GET", "/assets/mdweave.css", None),
        ("POST", "/api/documents", {"name": "x.md", "content": "hi"}),
        ("POST", "/api/block", {"document": "secret", "start": 0, "end": 1, "text": "x"}),
        ("POST", "/api/cut", {"document": "secret", "cuts": []}),
        ("POST", "/api/checkpoint", {"document": "secret", "message": "x"}),
        ("POST", "/api/annotations", {"document": "secret", "quote": "q", "body": "b"}),
        ("DELETE", "/api/annotations/x", None),
        ("PATCH", "/api/annotations/x", None),
    ],
)
def test_every_route_needs_the_password(server, method, path, payload):
    status, _ = request(server, path, method=method, payload=payload)
    assert status == 401


def test_the_prose_does_not_leak_in_the_401_body(server):
    _, body = request(server, "/secret.html")
    assert b"Private notes" not in body


def test_the_challenge_makes_the_browser_prompt(server):
    """Without WWW-Authenticate the browser shows an error, not a login box."""
    try:
        urllib.request.urlopen(server + "/secret.html", timeout=10)
        raise AssertionError("should have been refused")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
        assert exc.headers.get("WWW-Authenticate", "").startswith("Basic realm=")


# --- and everything with it ------------------------------------------------

def test_the_right_password_gets_in(server):
    status, body = request(server, "/secret.html", token=token_for())
    assert status == 200
    assert b"Private notes" in body


def test_the_api_works_once_authenticated(server):
    status, body = request(server, "/api/health", token=token_for())
    assert status == 200
    assert json.loads(body)["documents"] == ["secret"]


@pytest.mark.parametrize(
    "token",
    [
        token_for(password="wrong"),
        token_for(user="someone"),
        token_for(user="", password=""),
        "not base64 at all",
        base64.b64encode(b"no colon here").decode(),
    ],
)
def test_a_wrong_credential_is_refused(server, token):
    assert request(server, "/secret.html", token=token)[0] == 401


def test_a_custom_username_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("MDWEAVE_PASSWORD", "pw")
    monkeypatch.setenv("MDWEAVE_USER", "mengda")
    assert serve_module.credentials() == ("mengda", "pw")


# --- the health check exemption --------------------------------------------

def test_ping_answers_without_a_password(server):
    """A platform health check cannot present one."""
    status, body = request(server, "/api/ping")
    assert status == 200
    assert json.loads(body) == {"ok": True}


def test_ping_gives_nothing_away(server):
    """/api/health would have listed every document name."""
    _, body = request(server, "/api/ping")
    payload = json.loads(body)
    assert "documents" not in payload
    assert "fingerprint" not in payload


# --- no password configured ------------------------------------------------

def test_without_a_password_nothing_is_gated(tmp_path, monkeypatch):
    """The loopback server on your own machine stays as it was."""
    monkeypatch.delenv("MDWEAVE_PASSWORD", raising=False)

    inputs = tmp_path / "markdown_inputs"
    inputs.mkdir()
    (inputs / "open.md").write_text("# Open\n\nText.\n", encoding="utf-8")
    workspace = Workspace(inputs=inputs, outputs=tmp_path / "html_outputs")
    workspace.rebuild_all()

    httpd = make_server(workspace, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        assert request(base, "/open.html")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# --- refusing to bind wide open --------------------------------------------

def test_loopback_without_a_password_is_fine(monkeypatch):
    monkeypatch.delenv("MDWEAVE_PASSWORD", raising=False)
    monkeypatch.delenv("MDWEAVE_ALLOW_INSECURE", raising=False)
    for host in ("127.0.0.1", "::1", "localhost"):
        assert serve_module.refuse_insecure_bind(host) is None


def test_a_public_bind_without_a_password_is_refused(monkeypatch):
    """The accident this exists to prevent: deployed, open, and able to push."""
    monkeypatch.delenv("MDWEAVE_PASSWORD", raising=False)
    monkeypatch.delenv("MDWEAVE_ALLOW_INSECURE", raising=False)

    refusal = serve_module.refuse_insecure_bind("0.0.0.0")
    assert refusal and "MDWEAVE_PASSWORD" in refusal


def test_a_public_bind_with_a_password_is_allowed(monkeypatch):
    monkeypatch.setenv("MDWEAVE_PASSWORD", "pw")
    monkeypatch.delenv("MDWEAVE_ALLOW_INSECURE", raising=False)
    assert serve_module.refuse_insecure_bind("0.0.0.0") is None


def test_the_refusal_can_be_overridden_deliberately(monkeypatch):
    monkeypatch.delenv("MDWEAVE_PASSWORD", raising=False)
    monkeypatch.setenv("MDWEAVE_ALLOW_INSECURE", "1")
    assert serve_module.refuse_insecure_bind("0.0.0.0") is None


def test_serve_exits_rather_than_binding_wide_open(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MDWEAVE_PASSWORD", raising=False)
    monkeypatch.delenv("MDWEAVE_ALLOW_INSECURE", raising=False)

    inputs = tmp_path / "markdown_inputs"
    inputs.mkdir()
    assert serve_module.serve(inputs, tmp_path / "out", host="0.0.0.0", port=0) == 1
    assert "refusing to serve" in capsys.readouterr().err


# --- the CLI refuses before it does any work -------------------------------

def test_serve_refuses_before_rendering_anything(tmp_path, monkeypatch, capsys):
    """The refusal must not come after a full rebuild of the document set."""
    from mdweave.cli import main

    monkeypatch.delenv("MDWEAVE_PASSWORD", raising=False)
    monkeypatch.delenv("MDWEAVE_ALLOW_INSECURE", raising=False)

    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "doc.md").write_text("# Doc\n\nText.\n", encoding="utf-8")

    code = main(["serve", "-i", str(inputs), "-o", str(outputs), "--host", "0.0.0.0"])

    assert code == 1
    assert "refusing to serve" in capsys.readouterr().err
    assert not outputs.exists(), "nothing should have been rendered"


def test_start_refuses_a_public_bind_too(tmp_path, monkeypatch, capsys):
    from mdweave.cli import main

    monkeypatch.delenv("MDWEAVE_PASSWORD", raising=False)
    monkeypatch.delenv("MDWEAVE_ALLOW_INSECURE", raising=False)

    inputs = tmp_path / "markdown_inputs"
    inputs.mkdir()
    code = main(["start", "-i", str(inputs), "-o", str(tmp_path / "out"),
                 "--host", "0.0.0.0", "--port", "0", "--no-open"])

    assert code == 1
    assert "refusing to serve" in capsys.readouterr().err
