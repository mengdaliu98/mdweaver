"""Tests for reloading a document from disk.

The point of the button is that this server is not the only writer: an editor,
a `git pull`, another machine. So the tests change files behind the server's
back and check it notices.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave.serve import Workspace, make_server

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"
TEMPLATES = Path(__file__).resolve().parents[1] / "mdweave" / "templates"


@pytest.fixture()
def server(tmp_path):
    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "alpha.md").write_text("# Alpha\n\nOriginal.\n", encoding="utf-8")
    (inputs / "bravo.md").write_text("# Bravo\n\nOther.\n", encoding="utf-8")

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


def call(base, path, payload):
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_refresh_picks_up_an_edit_made_behind_our_back(server):
    base, workspace = server
    (workspace.inputs / "alpha.md").write_text(
        "# Alpha\n\nChanged by someone else.\n", encoding="utf-8"
    )

    status, payload = call(base, "/api/refresh", {"document": "alpha"})

    assert status == 200
    assert "Changed by someone else." in payload["body"]
    assert "Original." not in payload["body"]


def test_refresh_rewrites_the_html_on_disk_too(server):
    """A plain reload would otherwise still serve the stale file."""
    base, workspace = server
    (workspace.inputs / "alpha.md").write_text("# Alpha\n\nFresh.\n", encoding="utf-8")

    call(base, "/api/refresh", {"document": "alpha"})

    assert "Fresh." in (workspace.outputs / "alpha.html").read_text(encoding="utf-8")


def test_refresh_returns_a_fragment_that_stays_editable(server):
    base, _ = server
    _, payload = call(base, "/api/refresh", {"document": "alpha"})
    assert "<html" not in payload["body"]
    assert "<article" not in payload["body"]
    assert "data-src-start" in payload["body"]


def test_refresh_reports_the_document_set(server):
    """The client needs this to notice the sidebar has gone stale."""
    base, _ = server
    _, payload = call(base, "/api/refresh", {"document": "alpha"})
    assert payload["documents"] == ["alpha", "bravo"]


def test_a_document_added_externally_shows_up_in_the_set(server):
    base, workspace = server
    (workspace.inputs / "charlie.md").write_text("# Charlie\n", encoding="utf-8")

    _, payload = call(base, "/api/refresh", {"document": "alpha"})

    assert payload["documents"] == ["alpha", "bravo", "charlie"]
    assert (workspace.outputs / "charlie.html").exists(), "the new page is built too"


def test_refresh_rebuilds_every_document_not_just_this_one(server):
    """A pull can touch several files; the sidebar is baked into each page."""
    base, workspace = server
    (workspace.inputs / "bravo.md").write_text("# Bravo\n\nAlso changed.\n", encoding="utf-8")

    call(base, "/api/refresh", {"document": "alpha"})

    assert "Also changed." in (workspace.outputs / "bravo.html").read_text(encoding="utf-8")


def test_refresh_of_a_deleted_document_is_a_404(server):
    base, workspace = server
    (workspace.inputs / "alpha.md").unlink()
    assert call(base, "/api/refresh", {"document": "alpha"})[0] == 404


def test_refresh_needs_a_document(server):
    base, _ = server
    assert call(base, "/api/refresh", {})[0] == 400


# --- the browser side ------------------------------------------------------

def test_the_button_is_an_icon_with_no_label():
    template = (TEMPLATES / "document.html.j2").read_text(encoding="utf-8")
    button = template.split('id="refresh"')[1].split("</button>")[0]
    assert "<svg" in button
    assert "<span>" not in button, "icon only, no wording"
    assert 'aria-label="Refresh"' in button, "so it is not silent to a screen reader"


def test_the_button_sits_to_the_left_of_checkpoint():
    template = (TEMPLATES / "document.html.j2").read_text(encoding="utf-8")
    assert template.index('id="refresh"') < template.index('id="checkpoint"')


def test_the_button_is_hidden_until_a_server_answers():
    template = (TEMPLATES / "document.html.j2").read_text(encoding="utf-8")
    source = (ASSETS / "refresh.js").read_text(encoding="utf-8")
    assert template.split('id="refresh"')[1].split(">")[0].strip().endswith("hidden")
    assert "button.hidden = false" in source
    assert "ui.api()" in source


def test_refreshing_asks_the_server_rather_than_reloading():
    """location.reload() alone would just re-serve the same stale HTML."""
    source = (ASSETS / "refresh.js").read_text(encoding="utf-8")
    assert '"/api/refresh"' in source
    before_reload = source.split("window.location.reload()")[0]
    assert "fetch(" in before_reload


def test_a_changed_document_set_falls_back_to_a_real_reload():
    """Swapping the article cannot update a sidebar baked into the page."""
    source = (ASSETS / "refresh.js").read_text(encoding="utf-8")
    assert "sidebarDocuments()" in source
    assert "window.location.reload()" in source


def test_refresh_waits_for_an_edit_in_flight():
    """Replacing the prose mid-save would throw the edit away."""
    refresh = (ASSETS / "refresh.js").read_text(encoding="utf-8")
    edit = (ASSETS / "edit.js").read_text(encoding="utf-8")
    assert "window.mdweaveEdit" in refresh and "isBusy()" in refresh
    assert "window.mdweaveEdit" in edit and "isBusy:" in edit


def test_the_swap_is_shared_not_duplicated():
    ui = (ASSETS / "ui.js").read_text(encoding="utf-8")
    refresh = (ASSETS / "refresh.js").read_text(encoding="utf-8")
    edit = (ASSETS / "edit.js").read_text(encoding="utf-8")

    assert "adopt: adopt" in ui
    assert "ui.adopt(" in refresh and "ui.adopt(" in edit
    # Neither module keeps its own copy of the innerHTML dance.
    for module in (refresh, edit):
        assert 'getElementById("notes-layer")' not in module
