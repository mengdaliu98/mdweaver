"""Tests for importing a markdown file from the browser."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave.serve import Workspace, make_server
from mdweave.tree import safe_document_name

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"


# --- filename safety ------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("notes.md", "notes.md"),
        ("My Notes.md", "My_Notes.md"),
        ("  padded  .md", "padded.md"),
        ("weekly   update.md", "weekly_update.md"),
        ("Notes.MD", "Notes.md"),
        # Any path is discarded down to the bare filename.
        ("../../../etc/passwd.md", "passwd.md"),
        ("/absolute/path/doc.md", "doc.md"),
        ("..\\..\\windows\\doc.md", "doc.md"),
        # Characters that break filenames or URLs are dropped.
        ('a<>:"|?*b.md', "ab.md"),
        ("with\x00null.md", "withnull.md"),
        # No hidden files.
        (".hidden.md", "hidden.md"),
    ],
)
def test_safe_document_name(raw, expected):
    assert safe_document_name(raw) == expected


@pytest.mark.parametrize("raw", ["notes.txt", "notes", "notes.md.exe", "", "README"])
def test_non_markdown_is_refused(raw):
    with pytest.raises(ValueError, match="only .md"):
        safe_document_name(raw)


@pytest.mark.parametrize("raw", [".md", "...md", "   .md", "///.md", '<>.md'])
def test_a_name_with_nothing_usable_is_refused(raw):
    with pytest.raises(ValueError, match="usable"):
        safe_document_name(raw)


def test_a_sanitised_name_can_never_escape_the_root(tmp_path):
    """The property that matters: the write always lands inside `inputs`."""
    root = (tmp_path / "inputs").resolve()
    root.mkdir()
    for raw in ["../out.md", "../../../../tmp/evil.md", "/etc/x.md", "..\\..\\y.md"]:
        target = (root / safe_document_name(raw)).resolve()
        assert target.parent == root


# --- the endpoint ---------------------------------------------------------

@pytest.fixture()
def server(tmp_path):
    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "existing.md").write_text("# Existing\n\nAlready here.\n", encoding="utf-8")

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


def call(base, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def upload(base, name="imported_notes.md", content="# Imported\n\nHello.\n", **extra):
    payload = {"name": name, "content": content}
    payload.update(extra)
    return call(base, "POST", "/api/documents", payload)


def test_import_writes_the_markdown_file(server):
    base, workspace = server
    status, payload = upload(base)

    assert status == 201
    assert payload["document"] == {
        "id": "imported_notes",
        "href": "imported_notes.html",
        "label": "Imported notes",
    }
    assert (workspace.inputs / "imported_notes.md").read_text() == "# Imported\n\nHello.\n"


def test_import_renders_the_html(server):
    base, workspace = server
    upload(base)

    html = (workspace.outputs / "imported_notes.html").read_text()
    assert "<h1" in html and "Imported" in html
    assert 'data-document="imported_notes"' in html


def test_the_new_document_appears_in_every_sidebar(server):
    """The point of rebuilding all pages: no restart, no stale navigation."""
    base, workspace = server
    upload(base)

    existing = (workspace.outputs / "existing.html").read_text()
    assert 'href="imported_notes.html"' in existing
    assert "Imported notes" in existing


def test_the_new_document_is_served_immediately(server):
    base, _ = server
    upload(base)

    with urllib.request.urlopen(base + "/imported_notes.html", timeout=10) as response:
        assert response.status == 200
    _, health = call(base, "GET", "/api/health")
    assert "imported_notes" in health["documents"]


def test_an_imported_document_accepts_comments_straight_away(server):
    base, _ = server
    upload(base, content="# Imported\n\nSome anchorable text here.\n")

    status, payload = call(base, "POST", "/api/annotations", {
        "document": "imported_notes", "quote": "anchorable text",
        "prefix": "Some", "suffix": "here.", "occurrence": 0,
        "body": "Works right away.",
    })
    assert status == 201
    assert payload["annotation"]["target"]["quote"] == "anchorable text"


def test_a_clash_is_refused_rather_than_overwriting(server):
    base, workspace = server
    status, payload = upload(base, name="existing.md", content="# Replaced\n")

    assert status == 409
    assert "already exists" in payload["error"]
    assert "Already here." in (workspace.inputs / "existing.md").read_text()


def test_replace_is_honoured_when_asked_for_explicitly(server):
    base, workspace = server
    status, _ = upload(base, name="existing.md", content="# Replaced\n", replace=True)

    assert status == 201
    assert (workspace.inputs / "existing.md").read_text() == "# Replaced\n"


def test_a_spaced_filename_is_normalised(server):
    base, workspace = server
    _, payload = upload(base, name="My Weekly Notes.md")

    assert payload["document"]["id"] == "My_Weekly_Notes"
    # The id keeps the name as given; the label is sentence case, like every
    # other label in the tree.
    assert payload["document"]["label"] == "My weekly notes"
    assert (workspace.inputs / "My_Weekly_Notes.md").exists()


@pytest.mark.parametrize(
    "name", ["../escape.md", "/etc/escape.md", "..\\escape.md"]
)
def test_a_traversing_name_lands_inside_the_root(server, name):
    base, workspace = server
    status, payload = upload(base, name=name)

    assert status == 201
    written = list(workspace.inputs.rglob("*.md"))
    assert all(p.parent == workspace.inputs for p in written)
    assert not (workspace.inputs.parent / "escape.md").exists()


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"name": "notes.txt", "content": "x"}, 400),
        ({"name": ".md", "content": "x"}, 400),
        ({"name": "ok.md"}, 400),                    # no content
        ({"name": "ok.md", "content": 42}, 400),     # content not a string
        ({"content": "x"}, 400),                     # no name
        ({"name": "", "content": "x"}, 400),
    ],
)
def test_bad_imports_are_rejected(server, payload, expected):
    base, workspace = server
    before = set(workspace.inputs.iterdir())

    assert call(base, "POST", "/api/documents", payload)[0] == expected
    assert set(workspace.inputs.iterdir()) == before, "nothing should be written"


def test_an_empty_document_is_allowed(server):
    base, workspace = server
    status, _ = upload(base, name="blank.md", content="")
    assert status == 201
    assert (workspace.inputs / "blank.md").read_text() == ""


def test_a_large_document_is_accepted(server):
    base, workspace = server
    big = "# Big\n\n" + ("filler paragraph text. " * 20000)
    status, _ = upload(base, name="big.md", content=big)

    assert status == 201
    assert len((workspace.inputs / "big.md").read_text()) == len(big)


def test_an_oversized_document_is_refused(server):
    base, _ = server
    request = urllib.request.Request(
        base + "/api/documents",
        data=json.dumps({"name": "huge.md", "content": "x" * (9 * 1024 * 1024)}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=30)
        raise AssertionError("expected a rejection")
    except urllib.error.HTTPError as exc:
        assert exc.code == 413


def test_unicode_content_survives(server):
    base, workspace = server
    upload(base, name="unicode.md", content="# Ünïcode\n\nCRAM/BAM — 30 % smaller.\n")
    assert "—" in (workspace.inputs / "unicode.md").read_text(encoding="utf-8")


# --- the browser side -----------------------------------------------------

def test_only_markdown_is_offered_and_accepted_client_side():
    source = (ASSETS / "sidebar.js").read_text(encoding="utf-8")
    assert re.search(r"/\\\.md\$/i", source), "client must filter to .md"
    assert "isMarkdown" in source


def test_dropping_elsewhere_cannot_navigate_the_page_away():
    """Without this, a missed drop replaces the app with the raw file."""
    source = (ASSETS / "sidebar.js").read_text(encoding="utf-8")
    assert 'window.addEventListener("drop"' in source
    assert 'window.addEventListener("dragover"' in source


def test_importing_only_turns_on_once_a_server_answers():
    """The per-row buttons live in filetree.js now; the drop still lives here."""
    sidebar = (ASSETS / "sidebar.js").read_text(encoding="utf-8")
    filetree = (ASSETS / "filetree.js").read_text(encoding="utf-8")

    assert "ui.api()" in sidebar
    assert "enableDrop()" in sidebar.split("ui.api()")[1]
    assert "ui.api()" in filetree


def test_a_rejected_drop_is_reported_in_the_middle_of_the_page():
    """A corner toast is missed mid-drag; the eye is on the cursor."""
    sidebar = (ASSETS / "sidebar.js").read_text(encoding="utf-8")
    assert '"only markdown files are supported"' in sidebar
    assert "ui.notice(REJECTED)" in sidebar
    assert "skipped" not in sidebar, "the old corner-toast wording is gone"


def test_the_notice_is_centred_and_dismissable():
    ui = (ASSETS / "ui.js").read_text(encoding="utf-8")
    css = (Path(__file__).resolve().parents[1] / "mdweave" / "theme" / "editor.css").read_text(
        encoding="utf-8"
    )

    assert "function notice(" in ui
    assert "notice: notice" in ui, "notice must be published on window.mdweaveUI"
    assert 'el.setAttribute("role", "alert")' in ui, "screen readers must hear it"
    assert 'event.key === "Escape"' in ui

    block = css.split(".mdweave-notice {")[1].split("}")[0]
    assert "position: fixed" in block
    assert "place-items: center" in block, "the message must land in the middle"
    assert "pointer-events: none" in block, "the overlay must not trap clicks"
    assert "pointer-events: auto" in css.split(".mdweave-notice__card {")[1].split("}")[0]


def test_only_one_notice_shows_at_a_time():
    """Two bad drops in a row must not stack two cards on top of each other."""
    ui = (ASSETS / "ui.js").read_text(encoding="utf-8")
    body = ui.split("function notice(")[1]
    assert "dismissNotice();" in body.split("var el =")[0]


def test_shared_helpers_are_used_rather_than_duplicated():
    ui = (ASSETS / "ui.js").read_text(encoding="utf-8")
    annotate = (ASSETS / "annotate.js").read_text(encoding="utf-8")
    sidebar = (ASSETS / "sidebar.js").read_text(encoding="utf-8")

    assert "window.mdweaveUI" in ui
    for module in (annotate, sidebar):
        assert "window.mdweaveUI" in module or "mdweaveUI" in module
    # The old per-module copies are gone.
    assert "function toast(" not in annotate
    assert "function toast(" not in sidebar
