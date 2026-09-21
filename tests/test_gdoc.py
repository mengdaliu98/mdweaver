"""Importing a Google Doc.

A snapshot, not a link: the doc is read once, converted to markdown, and from
then on it is an ordinary document here. Two things are worth testing beyond
the happy path. The `meta` CLI is not on every machine -- a container has no
such thing -- so everything has to degrade to a clear refusal rather than a
traceback. And the sources dictionary only earns its keep if re-importing the
same doc updates the document already here instead of making a second copy.

The CLI is stubbed throughout: a test that needs Meta's corporate tooling and
a live Google Doc is a test that runs nowhere, including here on a bad day.
The one thing a stub cannot check is whether the real `meta google.docs get`
still speaks markdown, which is what `test_the_cli_contract_is_what_we_think`
is for -- and it skips when the CLI is absent.
"""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave import gdoc
from mdweave.serve import Workspace, make_server
from mdweave.tree import LABELS_FILE, SOURCES_FILE, load_labels, load_sources

DOC_ID = "1YSoXNsDpzcYAl7IN7udz2eOAog5H_SIiwCJkJflhhj8"
URL = f"https://docs.google.com/document/d/{DOC_ID}/edit?usp=drivesdk"

DESCRIBE = {
    "name": "Code Efficiency — AAI Proposal",
    "id": DOC_ID,
    "modifiedTime": "2026-09-21T14:38:46-07:00",
    "url": f"https://docs.google.com/document/d/{DOC_ID}/edit?usp=drivesdk",
}
BODY = "Code Efficiency — AAI Proposal\n\n## TL;DR\n\nSome prose.\n"


@pytest.fixture()
def cli(monkeypatch):
    """Stand in for `meta google.docs`, and record what it was asked."""
    calls: list[tuple[str, ...]] = []
    answers = {"describe": json.dumps(DESCRIBE), "get": BODY}

    def fake(*args: str) -> str:
        calls.append(args)
        return answers[args[0]]

    monkeypatch.setattr(gdoc, "available", lambda: True)
    monkeypatch.setattr(gdoc, "_run", fake)
    return calls, answers


# --- reading the URL --------------------------------------------------------

@pytest.mark.parametrize(
    "given",
    [
        URL,
        f"https://docs.google.com/document/d/{DOC_ID}/edit",
        f"https://docs.google.com/document/u/0/d/{DOC_ID}/edit#heading=x",
        DOC_ID,
        f"  {URL}  ",
    ],
)
def test_the_id_is_found_however_the_link_was_copied(given):
    assert gdoc.doc_id_from(given) == DOC_ID


@pytest.mark.parametrize("given", ["", "   ", "https://example.com/x", "notaurl"])
def test_something_that_is_not_a_doc_is_refused(given):
    with pytest.raises(gdoc.GDocError):
        gdoc.doc_id_from(given)


# --- the snapshot -----------------------------------------------------------

def test_the_title_becomes_the_heading(cli):
    """The export leads with the title as a bare line. Left alone it renders
    as an unstyled first paragraph and the page has no <h1> to take its own
    title from."""
    snapshot = gdoc.fetch(URL)
    assert snapshot.markdown.startswith("# Code Efficiency — AAI Proposal")
    assert snapshot.markdown.count("Code Efficiency — AAI Proposal") == 1


def test_a_document_that_already_has_a_heading_is_left_as_it_is(cli):
    calls, answers = cli
    answers["get"] = "# Already a heading\n\nProse.\n"
    assert gdoc.fetch(URL).markdown.startswith("# Already a heading")


def test_images_are_asked_to_be_stripped(cli):
    """They arrive as data URIs, and a megabyte of base64 in a file meant to
    be read and edited as text is not a document."""
    calls, _ = cli
    gdoc.fetch(URL)
    get = next(c for c in calls if c[0] == "get")
    assert "--images=strip" in get
    assert "--output=markdown" in get


def test_the_revision_is_the_modified_time(cli):
    """`google.docs revisions` is the obvious answer and returns nothing at
    all for many documents, including the first one this was tried against."""
    assert gdoc.fetch(URL).revision == "2026-09-21T14:38:46-07:00"


def test_an_empty_document_is_refused(cli):
    _, answers = cli
    answers["get"] = "   \n"
    with pytest.raises(gdoc.GDocError, match="empty"):
        gdoc.fetch(URL)


def test_an_error_from_the_cli_reaches_the_reader(cli):
    _, answers = cli
    answers["describe"] = json.dumps({"status": "error", "message": "no access"})
    with pytest.raises(gdoc.GDocError, match="no access"):
        gdoc.fetch(URL)


def test_without_the_cli_it_says_so_rather_than_failing(monkeypatch):
    monkeypatch.setattr(gdoc, "available", lambda: False)
    with pytest.raises(gdoc.GDocError, match="not on this machine"):
        gdoc.fetch(URL)


@pytest.mark.parametrize(
    "title, want",
    [
        ("Short one", "Short one"),
        ("Code Efficiency — AAI Proposal", "Code Efficiency AAI Proposal"),
        ("Q3: Plans / Risks", "Q3 Plans Risks"),
        ("x" * 200, "x" * 60),
    ],
)
def test_the_filename_is_short_and_plain(title, want):
    """A title is prose and a filename is a URL. The label carries the whole
    title, so the file only has to be short and easy to type."""
    assert gdoc.filename_for(title) == want


def test_a_long_title_is_cut_at_a_word(tmp_path):
    name = gdoc.filename_for("A really quite extraordinarily long document title that keeps going")
    assert len(name) <= gdoc.FILENAME_CHARS
    assert not name.endswith(" ") and " " in name


# --- through the server -----------------------------------------------------

@pytest.fixture()
def served(tmp_path):
    inputs, outputs = tmp_path / "markdown_inputs", tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "seed.md").write_text("# Seed\n\nhi\n", encoding="utf-8")
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
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_importing_writes_a_document_a_label_and_a_source(served, cli):
    base, workspace = served
    status, payload = call(base, "/api/documents/import-gdoc", {"url": URL})

    assert status == 201 and payload["updated"] is False
    doc_id = payload["document"]["id"]

    assert (workspace.inputs / f"{doc_id}.md").exists()
    assert (workspace.outputs / f"{doc_id}.html").exists()

    # The real title, verbatim -- `humanize` would eat the em dash and the
    # capitals, and a Google Doc's name is already prose.
    assert load_labels(workspace.inputs)[doc_id] == "Code Efficiency — AAI Proposal"

    source = load_sources(workspace.inputs)[doc_id]
    assert source["google_id"] == DOC_ID
    assert source["revision"] == "2026-09-21T14:38:46-07:00"
    assert source["kind"] == "gdoc"
    assert source["url"].endswith("/edit?usp=drivesdk")


def test_importing_the_same_doc_again_updates_it_in_place(served, cli):
    """What the dictionary is *for*. Without the lookup a second import makes
    `Code Efficiency AAI Proposal` and `Code Efficiency AAI Proposal-1`, and
    the reader has two copies of one document."""
    base, workspace = served
    _, first = call(base, "/api/documents/import-gdoc", {"url": URL})
    doc_id = first["document"]["id"]

    _, answers = cli
    answers["get"] = "Code Efficiency — AAI Proposal\n\n## TL;DR\n\nRewritten.\n"
    answers["describe"] = json.dumps({**DESCRIBE, "modifiedTime": "2026-09-22T09:00:00-07:00"})

    status, again = call(base, "/api/documents/import-gdoc", {"url": URL})

    assert status == 201 and again["updated"] is True
    assert again["document"]["id"] == doc_id
    assert sorted(workspace.documents()) == sorted([doc_id, "seed"])
    assert "Rewritten." in workspace.source_of(doc_id)
    assert load_sources(workspace.inputs)[doc_id]["revision"] == "2026-09-22T09:00:00-07:00"


def test_a_reimport_keeps_the_annotations_and_the_label(served, cli):
    """Only the prose comes from Google. Everything the reader added to the
    document here is theirs."""
    from mdweave.model import Annotation, TextTarget
    from mdweave.sources import sidecar

    base, workspace = served
    _, first = call(base, "/api/documents/import-gdoc", {"url": URL})
    doc_id = first["document"]["id"]

    sidecar.save(
        sidecar.sidecar_path(workspace.markdown_for(doc_id)),
        [Annotation(id="a1", target=TextTarget(quote="Some prose"), kind="highlight", color=3)],
    )
    call(base, "/api/tree/label", {"key": doc_id, "label": "My name for it"})

    call(base, "/api/documents/import-gdoc", {"url": URL})

    assert len(workspace.annotations_for(doc_id)) == 1
    assert load_labels(workspace.inputs)[doc_id] == "My name for it"


def test_importing_into_a_folder(served, cli):
    base, workspace = served
    (workspace.inputs / "notes").mkdir()

    _, payload = call(base, "/api/documents/import-gdoc", {"url": URL, "folder": "notes"})
    assert payload["document"]["id"].startswith("notes/")


def test_a_bad_url_is_reported_not_raised(served, cli):
    base, workspace = served
    status, payload = call(base, "/api/documents/import-gdoc", {"url": "https://example.com/x"})

    assert status == 502
    assert "does not look like" in payload["error"]
    assert not (workspace.inputs / SOURCES_FILE).exists()


def test_deleting_an_imported_document_forgets_where_it_came_from(served, cli):
    base, workspace = served
    _, payload = call(base, "/api/documents/import-gdoc", {"url": URL})
    doc_id = payload["document"]["id"]

    call(base, "/api/documents/delete", {"document": doc_id})
    assert load_sources(workspace.inputs) == {}


def test_moving_an_imported_document_carries_its_source(served, cli):
    base, workspace = served
    _, payload = call(base, "/api/documents/import-gdoc", {"url": URL})
    doc_id = payload["document"]["id"]
    (workspace.inputs / "notes").mkdir()

    call(base, "/api/documents/move", {"from": doc_id, "to": f"notes/{doc_id}"})

    sources = load_sources(workspace.inputs)
    assert list(sources) == [f"notes/{doc_id}"]
    assert sources[f"notes/{doc_id}"]["google_id"] == DOC_ID


def test_health_says_whether_this_machine_can_read_a_doc(served, monkeypatch):
    """The browser hides the option rather than offering one that will fail.
    A container has no `meta`."""
    base, _ = served
    with urllib.request.urlopen(base + "/api/health", timeout=10) as response:
        assert json.load(response)["gdoc"] == gdoc.available()


# --- the one thing a stub cannot check --------------------------------------

@pytest.mark.skipif(
    shutil.which("meta") is None, reason="the meta CLI is not on this machine"
)
def test_the_cli_contract_is_what_we_think():
    """Everything above stubs `meta google.docs`, which proves the plumbing
    and nothing about the tool. This checks the two flags the import depends
    on still exist -- without reading anybody's document."""
    import subprocess

    helped = subprocess.run(
        ["meta", "google.docs", "get", "--help"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    assert "--output=markdown" in helped
    assert "--images=strip" in helped

    described = subprocess.run(
        ["meta", "google.docs", "describe", "--help"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    assert "--output=json" in described or "--id=" in described
