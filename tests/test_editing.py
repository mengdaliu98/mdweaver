"""Tests for the select-text-and-comment path.

The browser derives an anchor from a selection (`annotate.js: selectorFor`) and
Python must find that exact span again when it re-renders (`anchors.find`).
Those two live in different languages, so the round-trip property is tested
here against the Python pair, and a drift guard checks the shared constant.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from mdweave import anchors
from mdweave.anchors import TextIndex, selector_for
from mdweave.render import render_markdown
from mdweave.model import Annotation, TextTarget
from mdweave import serve
from mdweave.serve import Workspace, make_server
from mdweave.sources import sidecar

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"


def index_of(markdown: str) -> TextIndex:
    return TextIndex(BeautifulSoup(render_markdown(markdown), "html.parser"))


# --- selector round trip --------------------------------------------------

REPEATED = "the cat sat. the cat sat. the cat sat."

DOCUMENTS = [
    "# Title\n\nServe minimal byte ranges over CRAM/BAM + index.\n",
    REPEATED,
    "one two\nthree four five\n\nsix seven eight\n",
    "A highlight may span **bold text** and carry on past it.\n",
    "| Format | Compression |\n| - | - |\n| BAM | block |\n| CRAM | reference |\n",
    "- alpha\n- beta\n- gamma\n\n> quoted alpha beta\n",
    "Text with `inline code` and a [link](https://example.com) inside.\n",
]


@pytest.mark.parametrize("markdown", DOCUMENTS)
def test_selector_round_trip_over_every_span(markdown):
    """Every span, selected then re-found, lands back on the same characters."""
    index = index_of(markdown)
    text = index.text
    # Word-ish boundaries -- the spans a human selection actually produces.
    bounds = [0] + [m.end() for m in re.finditer(r"\S+", text)] + [len(text)]

    checked = 0
    for i, start in enumerate(bounds):
        for end in bounds[i + 1 :]:
            if not text[start:end].strip():
                continue
            target = selector_for(text, start, end)
            found = index.find(target)
            assert found is not None, f"lost {text[start:end]!r}"
            assert text[found[0] : found[1]] == target.quote, (
                f"{text[start:end]!r} re-resolved to {text[found[0]:found[1]]!r}"
            )
            checked += 1
    assert checked > 0


def test_selector_distinguishes_identical_repeated_sentences():
    """The case context alone cannot settle: the same words three times over."""
    index = index_of(REPEATED)
    text = index.text
    starts = [m.start() for m in re.finditer("cat", text)]
    assert len(starts) == 3

    for n, start in enumerate(starts):
        target = selector_for(text, start, start + 3)
        assert target.occurrence == n
        assert index.find(target) == (start, start + 3)


def test_selector_survives_edits_elsewhere_in_the_document():
    """An anchor keeps working when unrelated prose around it changes."""
    original = "# Doc\n\nIntro line.\n\nServe over CRAM/BAM + index.\n"
    index = index_of(original)
    start = index.text.index("CRAM/BAM")
    target = selector_for(index.text, start, start + len("CRAM/BAM"))

    edited = index_of("# Doc\n\nA totally rewritten intro.\n\nMore.\n\nServe over CRAM/BAM + index.\n")
    found = edited.find(target)
    assert found is not None
    assert edited.text[found[0] : found[1]] == "CRAM/BAM"


def test_occurrence_is_an_index_into_the_unfiltered_match_list():
    """Regression: occurrence used to be read against the filtered list."""
    text = "x TARGET y. z TARGET y. x TARGET y."
    index = index_of(text)
    flat = index.text
    starts = [m.start() for m in re.finditer("TARGET", flat)]

    # Two of the three share the prefix "x", so prefix filtering alone leaves
    # an ambiguity that only a correctly-based occurrence can break.
    for n, start in enumerate(starts):
        target = selector_for(flat, start, start + len("TARGET"))
        assert index.find(target) == (start, start + len("TARGET")), f"occurrence {n}"


def test_occurrence_index_counts_overlapping_matches():
    assert anchors.occurrence_index("aaaa", "aa", 0) == 0
    assert anchors.occurrence_index("aaaa", "aa", 2) == 2  # matches at 0 and 1


def test_selector_normalises_a_selection_spanning_a_line_break():
    index = index_of("one two\nthree four")
    start = index.text.index("two")
    target = selector_for(index.text, start, start + len("two three"))
    assert target.quote == "two three"
    assert index.find(target) == (start, start + len("two three"))


# --- the JS mirror --------------------------------------------------------

def test_annotate_js_context_window_matches_python():
    """`selectorFor` in the browser must use the same context width."""
    source = (ASSETS / "annotate.js").read_text(encoding="utf-8")
    match = re.search(r"var CONTEXT = (\d+);", source)
    assert match, "CONTEXT constant not found in annotate.js"
    assert int(match.group(1)) == anchors.CONTEXT_CHARS


def test_notes_js_exposes_the_hooks_annotate_js_uses():
    notes = (ASSETS / "notes.js").read_text(encoding="utf-8")
    annotate = (ASSETS / "annotate.js").read_text(encoding="utf-8")

    used = set(re.findall(r"\bmdw\.(\w+)", annotate))
    for name in used:
        assert re.search(rf"\b{name}:", notes), f"notes.js does not export {name}"


# --- the server -----------------------------------------------------------

DOC = "# Notes\n\nServe minimal byte ranges over CRAM/BAM + index.\n\nA second paragraph.\n"


@pytest.fixture()
def server(tmp_path):
    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "doc.md").write_text(DOC, encoding="utf-8")

    workspace = Workspace(inputs=inputs, outputs=outputs)
    httpd = make_server(workspace, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", workspace
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def call(base: str, method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def comment(base: str, **overrides):
    payload = {
        "document": "doc",
        "quote": "CRAM/BAM",
        "prefix": "Serve minimal byte ranges over",
        "suffix": "+ index.",
        "occurrence": 0,
        "body": "What are these formats?",
        "author": "me",
        "at": "2026-08-14T12:00:00.000Z",
    }
    payload.update(overrides)
    return call(base, "POST", "/api/annotations", payload)


def test_health_lists_the_documents(server):
    base, _ = server
    status, payload = call(base, "GET", "/api/health")
    assert status == 200
    assert payload["ok"] is True
    assert payload["documents"] == ["doc"]


def test_posting_a_comment_writes_the_sidecar(server):
    base, workspace = server
    status, payload = comment(base)
    assert status == 201

    annotation = payload["annotation"]
    assert annotation["target"]["quote"] == "CRAM/BAM"
    assert annotation["thread"][0]["body"] == "What are these formats?"
    assert len(annotation["id"]) == 5

    saved = sidecar.load(sidecar.sidecar_path(workspace.inputs / "doc.md"))
    assert [a.id for a in saved] == [annotation["id"]]
    assert saved[0].thread[0].author == "me"


def test_posting_a_comment_regenerates_the_html(server):
    base, workspace = server
    _, payload = comment(base)
    ann_id = payload["annotation"]["id"]

    html = (workspace.outputs / "doc.html").read_text(encoding="utf-8")
    assert f'data-ann="{ann_id}"' in html
    assert f'id="note-{ann_id}"' in html
    assert ">CRAM/BAM</mark>" in html


def test_a_second_comment_does_not_lose_the_first(server):
    base, workspace = server
    _, first = comment(base)
    status, second = comment(
        base, quote="second paragraph", prefix="A", suffix="", body="Another one."
    )
    assert status == 201
    assert second["annotation"]["id"] != first["annotation"]["id"]

    saved = sidecar.load(sidecar.sidecar_path(workspace.inputs / "doc.md"))
    assert len(saved) == 2


def test_an_unanchorable_quote_is_rejected_and_nothing_is_written(server):
    base, workspace = server
    status, payload = comment(base, quote="text that is not in the document")

    assert status == 409
    assert "could not anchor" in payload["error"]
    assert not sidecar.sidecar_path(workspace.inputs / "doc.md").exists()


def test_an_overlapping_selection_is_rejected(server):
    base, _ = server
    assert comment(base, quote="byte ranges over CRAM")[0] == 201
    status, payload = comment(base, quote="over CRAM/BAM", prefix="byte ranges")
    assert status == 409
    assert "overlaps" in payload["error"]


def test_deleting_a_comment_removes_it_from_sidecar_and_html(server):
    base, workspace = server
    _, payload = comment(base)
    ann_id = payload["annotation"]["id"]

    status, body = call(base, "DELETE", f"/api/annotations/{ann_id}?document=doc")
    assert status == 200
    assert body["deleted"] == ann_id

    assert sidecar.load(sidecar.sidecar_path(workspace.inputs / "doc.md")) == []
    assert f'data-ann="{ann_id}"' not in (workspace.outputs / "doc.html").read_text()


def test_deleting_an_unknown_id_is_a_404(server):
    base, _ = server
    assert call(base, "DELETE", "/api/annotations/nope?document=doc")[0] == 404


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"document": "nonexistent"}, 404),
        ({"document": "../../../etc/passwd"}, 404),
        ({"body": "   "}, 400),
        ({"quote": ""}, 400),
    ],
)
def test_bad_requests_are_rejected(server, overrides, expected):
    base, _ = server
    assert comment(base, **overrides)[0] == expected


def test_malformed_json_is_a_400_and_the_server_stays_up(server):
    base, _ = server
    request = urllib.request.Request(
        base + "/api/annotations",
        data=b"{not json",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("expected an error")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400

    assert call(base, "GET", "/api/health")[0] == 200


def test_unknown_api_route_is_a_404(server):
    base, _ = server
    assert call(base, "GET", "/api/nope")[0] == 404


def test_static_files_are_served(server):
    base, workspace = server
    comment(base)  # triggers a render
    with urllib.request.urlopen(base + "/doc.html", timeout=10) as response:
        assert response.status == 200
        assert b'data-document="doc"' in response.read()


def test_get_annotations_returns_what_was_saved(server):
    base, _ = server
    _, payload = comment(base)
    status, listing = call(base, "GET", "/api/annotations?document=doc")

    assert status == 200
    assert [a["id"] for a in listing["annotations"]] == [payload["annotation"]["id"]]


# --- dragging a note ------------------------------------------------------

def test_patch_saves_an_offset_to_the_sidecar(server):
    base, workspace = server
    _, payload = comment(base)
    ann_id = payload["annotation"]["id"]

    status, body = call(
        base,
        "PATCH",
        f"/api/annotations/{ann_id}",
        {"document": "doc", "offset": {"dx": 140, "dy": -32}},
    )
    assert status == 200
    assert body["annotation"]["offset"] == {"dx": 140.0, "dy": -32.0}

    (saved,) = sidecar.load(sidecar.sidecar_path(workspace.inputs / "doc.md"))
    assert saved.offset.dx == 140.0
    assert saved.offset.dy == -32.0


def test_a_saved_offset_is_rendered_as_data_attributes(server):
    base, workspace = server
    _, payload = comment(base)
    ann_id = payload["annotation"]["id"]
    call(base, "PATCH", f"/api/annotations/{ann_id}",
         {"document": "doc", "offset": {"dx": 140, "dy": -32}})

    html = (workspace.outputs / "doc.html").read_text(encoding="utf-8")
    assert 'data-dx="140.0"' in html
    assert 'data-dy="-32.0"' in html


def test_a_zero_offset_clears_the_stored_position(server):
    base, workspace = server
    _, payload = comment(base)
    ann_id = payload["annotation"]["id"]
    call(base, "PATCH", f"/api/annotations/{ann_id}",
         {"document": "doc", "offset": {"dx": 90, "dy": 10}})

    status, body = call(base, "PATCH", f"/api/annotations/{ann_id}",
                        {"document": "doc", "offset": {"dx": 0, "dy": 0}})
    assert status == 200
    assert "offset" not in body["annotation"]

    (saved,) = sidecar.load(sidecar.sidecar_path(workspace.inputs / "doc.md"))
    assert saved.offset is None
    assert "data-dx" not in (workspace.outputs / "doc.html").read_text()


def test_a_null_offset_also_clears_it(server):
    base, _ = server
    _, payload = comment(base)
    ann_id = payload["annotation"]["id"]
    call(base, "PATCH", f"/api/annotations/{ann_id}",
         {"document": "doc", "offset": {"dx": 90, "dy": 10}})

    _, body = call(base, "PATCH", f"/api/annotations/{ann_id}",
                   {"document": "doc", "offset": None})
    assert "offset" not in body["annotation"]


def test_an_extreme_offset_is_clamped_rather_than_rejected(server):
    base, _ = server
    _, payload = comment(base)
    ann_id = payload["annotation"]["id"]

    _, body = call(base, "PATCH", f"/api/annotations/{ann_id}",
                   {"document": "doc", "offset": {"dx": 10**9, "dy": -(10**9)}})
    assert body["annotation"]["offset"] == {"dx": 4000.0, "dy": -4000.0}


def test_patch_does_not_disturb_the_annotation_it_moves(server):
    base, _ = server
    _, payload = comment(base)
    before = payload["annotation"]
    ann_id = before["id"]

    _, body = call(base, "PATCH", f"/api/annotations/{ann_id}",
                   {"document": "doc", "offset": {"dx": 20, "dy": 20}})
    after = body["annotation"]

    assert after["target"] == before["target"]
    assert after["thread"] == before["thread"]
    assert after["color"] == before["color"]


def test_patch_leaves_other_annotations_alone(server):
    base, workspace = server
    _, first = comment(base)
    _, second = comment(base, quote="second paragraph", prefix="A", body="Another.")

    call(base, "PATCH", f"/api/annotations/{second['annotation']['id']}",
         {"document": "doc", "offset": {"dx": 50, "dy": 50}})

    saved = {a.id: a for a in sidecar.load(sidecar.sidecar_path(workspace.inputs / "doc.md"))}
    assert saved[first["annotation"]["id"]].offset is None
    assert saved[second["annotation"]["id"]].offset.dx == 50.0


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"document": "doc", "offset": "far left"}, 400),
        ({"document": "doc", "offset": {"dx": "abc", "dy": 0}}, 400),
        ({"document": "doc", "offset": {"dx": float("inf"), "dy": 0}}, 400),
        ({"document": "nonexistent", "offset": {"dx": 1, "dy": 1}}, 404),
        ({"offset": {"dx": 1, "dy": 1}}, 400),
    ],
)
def test_bad_patch_requests_are_rejected(server, payload, expected):
    base, _ = server
    _, created = comment(base)
    ann_id = created["annotation"]["id"]
    body = json.dumps(payload).replace("Infinity", "1e999")
    request = urllib.request.Request(
        base + f"/api/annotations/{ann_id}",
        data=body.encode(),
        method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == expected
    except urllib.error.HTTPError as exc:
        assert exc.code == expected


def test_patching_an_unknown_id_is_a_404(server):
    base, _ = server
    assert call(base, "PATCH", "/api/annotations/nope",
                {"document": "doc", "offset": {"dx": 1, "dy": 1}})[0] == 404


def test_offset_survives_a_sidecar_round_trip(tmp_path):
    from mdweave.model import Offset

    original = Annotation(
        id="x",
        target=TextTarget(quote="q"),
        offset=Offset(dx=12.5, dy=-3.25),
    )
    path = tmp_path / "d.ann.json"
    sidecar.save(path, [original])
    (restored,) = sidecar.load(path)

    assert restored.offset == Offset(dx=12.5, dy=-3.25)
    assert restored.to_dict() == original.to_dict()


def test_a_zero_offset_is_treated_as_no_offset():
    from mdweave.model import Annotation as A, Offset

    assert not Offset(0, 0)
    assert A.from_dict({"id": "x", "target": {"quote": "q"},
                        "offset": {"dx": 0, "dy": 0}}).offset is None
    assert "offset" not in A(id="x", target=TextTarget(quote="q"),
                             offset=Offset(0, 0)).to_dict()


# --- staleness detection --------------------------------------------------

def test_health_reports_a_fingerprint(server):
    base, _ = server
    _, payload = call(base, "GET", "/api/health")
    assert payload["fingerprint"] == serve.RUNNING_FINGERPRINT
    assert len(payload["fingerprint"]) == 12


def test_the_fingerprint_changes_when_source_changes(tmp_path, monkeypatch):
    """A server running old code must be distinguishable from a current one.

    Regression: the launcher reused a long-lived process, so a newly added
    endpoint answered 501 until someone thought to restart it by hand. This is
    what `mdweave start` compares to decide whether to replace what is running.
    """
    fake = tmp_path / "pkg"
    (fake / "assets").mkdir(parents=True)
    (fake / "serve.py").write_text("x = 1")
    (fake / "assets" / "notes.js").write_text("// v1")

    monkeypatch.setattr(serve, "__file__", str(fake / "serve.py"))
    before = serve.source_fingerprint()

    (fake / "assets" / "notes.js").write_text("// v2")
    assert serve.source_fingerprint() != before, "an edited asset must change the hash"

    (fake / "assets" / "notes.js").write_text("// v1")
    assert serve.source_fingerprint() == before, "the hash must be stable for fixed input"


def test_the_fingerprint_ignores_unrelated_files(tmp_path, monkeypatch):
    fake = tmp_path / "pkg"
    fake.mkdir()
    (fake / "serve.py").write_text("x = 1")
    monkeypatch.setattr(serve, "__file__", str(fake / "serve.py"))

    before = serve.source_fingerprint()
    (fake / "notes.pyc").write_bytes(b"\x00\x01")
    (fake / "scratch.txt").write_text("ignore me")
    assert serve.source_fingerprint() == before


def test_fingerprint_command_matches_the_module(capsys):
    from mdweave.cli import main

    assert main(["fingerprint"]) == 0
    assert capsys.readouterr().out.strip() == serve.source_fingerprint()


# --- composer placement ---------------------------------------------------

def strip_js_comments(source: str) -> str:
    """Drop // and /* */ comments so a grep sees code, not prose about it."""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", source)


def js_function(source: str, name: str) -> str:
    """Extract one `function name(...) { ... }` body by brace matching."""
    start = source.index("function " + name)
    depth, i = 0, source.index("{", start)
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[start : j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_open_composer_does_not_destroy_its_own_anchor():
    """Regression: the composer rendered at the page's top-left corner.

    `openComposer` used to call `closeComposer`, which unwraps
    `mark.hl--pending` -- the very highlight the caller had just created for it
    to anchor to. With no anchor, `layoutComposer` bailed out and the panel kept
    its CSS default of left:0/top:0.
    """
    source = strip_js_comments((ASSETS / "annotate.js").read_text(encoding="utf-8"))
    body = js_function(source, "openComposer")

    # Only the setup matters. Handlers wired up afterwards may legitimately call
    # closeComposer -- Escape has to tear the whole session down.
    setup = body[: body.index("layer.appendChild(composer)")]
    assert "closeComposer()" not in setup, "openComposer must not unwrap its own anchor"
    assert "unwrap(" not in setup
    assert "removeComposer()" in setup


def test_the_pending_highlight_is_created_before_the_composer_opens():
    """Order matters: the anchor has to exist when layoutComposer measures."""
    source = strip_js_comments((ASSETS / "annotate.js").read_text(encoding="utf-8"))
    # The semicolon distinguishes the call site from the declaration.
    wrap_at = source.index('"hl hl--amber hl--pending", PENDING)')
    open_at = source.index("openComposer(selector);")
    assert wrap_at < open_at, "the pending highlight must exist before the panel opens"


def test_closing_the_composer_still_clears_the_pending_highlight():
    source = strip_js_comments((ASSETS / "annotate.js").read_text(encoding="utf-8"))
    body = js_function(source, "closeComposer")
    assert 'unwrap("mark.hl--pending")' in body
    assert "removeComposer()" in body


def test_the_composer_anchors_to_the_pending_mark():
    """The mark and the panel must agree on the placeholder annotation id."""
    source = (ASSETS / "annotate.js").read_text(encoding="utf-8")
    assert 'var PENDING = "__pending__";' in source
    assert "composer.dataset.ann = PENDING;" in source
    # wrapSpan only sets data-ann when given an id, so it must be passed one.
    assert '"hl hl--amber hl--pending", PENDING' in source


def test_composer_and_note_cards_share_one_gap_value():
    """`layoutComposer` positions in JS what CSS positions for a real note."""
    js = (ASSETS / "notes.js").read_text(encoding="utf-8")
    css = (ASSETS.parent / "theme" / "base.css").read_text(encoding="utf-8")

    js_gap = re.search(r"var CARD_GAP = (\d+);", js)
    css_gap = re.search(r"--card-gap:\s*(\d+)px", css)
    assert js_gap and css_gap, "both CARD_GAP and --card-gap must be declared"
    assert int(js_gap.group(1)) == int(css_gap.group(1))

    js_pin = re.search(r"var PIN = (\d+);", js)
    css_pin = re.search(r"--pin:\s*(\d+)px", css)
    assert js_pin and css_pin
    assert int(js_pin.group(1)) == int(css_pin.group(1))


def test_the_card_offsets_in_css_are_expressed_with_the_shared_variables():
    """A hard-coded 6px here would silently drift from CARD_GAP."""
    css = (ASSETS.parent / "theme" / "annotations.css").read_text(encoding="utf-8")
    body = css[css.index(".note__body {") : css.index(".note--open .note__body")]
    assert "var(--card-gap)" in body
    assert "6px" not in body
