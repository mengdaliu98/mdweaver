"""Tests for editing the prose through the rendered page.

Two halves: the source mapping in `edits.py`, which is where the thinking is,
and the endpoints that expose it.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave import edits
from mdweave.render import render_markdown
from mdweave.serve import Workspace, make_server

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"

DOC = """# Title

First paragraph
across two lines.

Second paragraph with **bold** in it.

- one
- two

```python
x = 1
```

Last paragraph.
"""

LINES = DOC.splitlines()


def line_of(text: str) -> int:
    """Index of the line starting a given block, so tests do not hardcode."""
    return LINES.index(text)


# --- source maps in the markup --------------------------------------------

@pytest.mark.parametrize(
    "markdown, tag",
    [
        ("# Heading", "h1"),
        ("A paragraph.", "p"),
        ("- a\n- b", "ul"),
        ("> quoted", "blockquote"),
        ("```py\nx = 1\n```", "pre"),
        ("| a |\n| - |\n| 1 |", "table"),
    ],
)
def test_every_top_level_block_records_its_source_lines(markdown, tag):
    """The browser cannot ask to edit a block it cannot locate."""
    html = render_markdown(markdown)
    assert f"<{tag} data-src-start=" in html or f'<{tag} data-src-start="0"' in html


def test_nested_blocks_are_not_tagged():
    """Only the outermost block is editable -- a <li> is part of its list."""
    html = render_markdown("- one\n- two")
    assert html.count("data-src-start") == 1


def test_the_ranges_actually_address_the_right_lines():
    html = render_markdown(DOC)
    import re

    spans = [(int(a), int(b)) for a, b in re.findall(
        r'data-src-start="(\d+)" data-src-end="(\d+)"', html
    )]
    lines = DOC.splitlines()
    assert lines[spans[0][0]] == "# Title"
    assert lines[spans[1][0]] == "First paragraph"
    assert "".join(lines[spans[1][0]:spans[1][1]]).startswith("First paragraph")


# --- block_source / replace_block -----------------------------------------

def test_block_source_returns_just_that_block():
    assert edits.block_source(DOC, 2, 4) == "First paragraph\nacross two lines."


def test_block_source_trims_the_blank_line_a_list_swallows():
    markdown = "- one\n- two\n\nAfter.\n"
    assert edits.block_source(markdown, 0, 3) == "- one\n- two"


def test_block_source_rejects_a_range_outside_the_document():
    with pytest.raises(IndexError):
        edits.block_source(DOC, 900, 901)


def test_replace_block_swaps_only_that_block():
    updated = edits.replace_block(DOC, 2, 4, "Rewritten.")
    assert "Rewritten." in updated
    assert "First paragraph" not in updated
    assert "Second paragraph with **bold** in it." in updated
    assert "# Title" in updated


def test_replace_block_keeps_blocks_apart():
    """Without a blank line the next block would be absorbed into this one."""
    updated = edits.replace_block(DOC, 2, 4, "Rewritten.")
    assert "Rewritten.\n\nSecond paragraph" in updated


def test_replace_block_with_nothing_removes_it():
    updated = edits.replace_block(DOC, 2, 4, "")
    assert "First paragraph" not in updated
    assert "Second paragraph with **bold** in it." in updated


def test_a_paragraph_can_become_a_heading():
    updated = edits.replace_block(DOC, 2, 4, "## Promoted")
    assert "<h2" in render_markdown(updated)


# --- mapping visible offsets back to source -------------------------------

@pytest.mark.parametrize(
    "source, cut, expected",
    [
        ("one two three", (4, 8), "one three"),
        ("a **bold** word", (2, 6), "a  word"),          # emptied, delimiters swept
        ("a **bold** word", (2, 4), "a **ld** word"),    # partial, emphasis kept
        ("use `code` here", (4, 8), "use  here"),
        ("a [link](http://x) b", (2, 6), "a  b"),
        ("plain", (0, 5), ""),
    ],
)
def test_cutting_visible_text_maps_back_onto_the_markdown(source, cut, expected):
    assert edits.cut_block(source, cut[0], cut[1]) == expected


def test_a_cut_offset_is_an_offset_into_what_the_browser_sees():
    """The server and the browser have to agree on what character 2 is."""
    source = "a **bold** word"
    assert edits.rendered_text(source) == "a bold word"


def test_rendered_text_matches_element_text_content_not_the_fragment():
    """A trailing newline outside </p> would skew every offset after it."""
    assert edits.rendered_text("A line.") == "A line."
    assert not edits.rendered_text("A line.").endswith("\n")


def test_an_empty_cut_changes_nothing():
    assert edits.cut_block("unchanged", 3, 3) == "unchanged"


def test_cuts_are_applied_bottom_up():
    """Editing an early block would otherwise shift every later line number."""
    last = line_of("Last paragraph.")
    cuts = [
        edits.Cut(line=2, end=4, start=0, stop=len("First paragraph\nacross two lines.")),
        edits.Cut(line=last, end=last + 1, start=0, stop=len("Last paragraph.")),
    ]
    updated = edits.apply_cuts(DOC, cuts)
    assert "First paragraph" not in updated
    assert "Last paragraph." not in updated
    assert "Second paragraph with **bold** in it." in updated


def test_a_partial_cut_across_two_blocks_keeps_the_surviving_halves():
    markdown = "Alpha bravo.\n\nCharlie delta.\n"
    updated = edits.apply_cuts(markdown, [
        edits.Cut(line=0, end=1, start=6, stop=12),   # "bravo."
        edits.Cut(line=2, end=3, start=0, stop=8),    # "Charlie "
    ])
    assert "Alpha" in updated
    assert "delta." in updated
    assert "bravo" not in updated
    assert "Charlie" not in updated


def test_block_text_matches_what_the_browser_will_measure():
    """The load-bearing invariant of cutting.

    A cut arrives as offsets into `element.textContent`. The server maps them
    onto the source using its own idea of the block's visible text, so if the
    two ever disagree the cut lands in the wrong place -- silently, and in the
    middle of someone's prose. Every block of a document with footnotes,
    tables, code and nested lists has to agree exactly.
    """
    from bs4 import BeautifulSoup

    markdown = DOC + "\nA footnote.[^1]\n\n[^1]: The definition.\n"
    soup = BeautifulSoup(render_markdown(markdown), "html.parser")

    checked = 0
    for element in soup.select("[data-src-start]"):
        line = int(element["data-src-start"])
        end = int(element["data-src-end"])
        assert element.get_text() == edits.rendered_text_in(markdown, line, end)
        checked += 1
    assert checked > 5, "the fixture should exercise several block types"


def test_a_footnote_reference_is_why_context_matters():
    """Rendered alone, `[^1]` has no definition and stays literal."""
    markdown = "Text.[^1]\n\n[^1]: A note.\n"
    assert edits.rendered_text_in(markdown, 0, 1) == "Text.[1]"
    assert edits.rendered_text("Text.[^1]") == "Text.[^1]"


# --- normalise -------------------------------------------------------------

def test_blank_line_runs_collapse():
    assert edits.normalise("a\n\n\n\n\nb") == "a\n\nb\n"


def test_blank_lines_inside_a_fence_are_content():
    """Collapsing these would silently rewrite someone's code."""
    source = "```\nx\n\n\n\ny\n```\n"
    assert "x\n\n\n\ny" in edits.normalise(source)


def test_normalise_ends_with_exactly_one_newline():
    assert edits.normalise("a\n\n\n").endswith("a\n")
    assert not edits.normalise("a\n\n\n").endswith("\n\n")


# --- the endpoints ---------------------------------------------------------

@pytest.fixture()
def server(tmp_path):
    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "doc.md").write_text(DOC, encoding="utf-8")

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


def test_get_block_hands_back_the_markdown(server):
    base, _ = server
    status, payload = call(base, "GET", "/api/block?document=doc&start=2&end=4")
    assert status == 200
    assert payload["markdown"] == "First paragraph\nacross two lines."


def test_get_block_rejects_a_bad_range(server):
    base, _ = server
    assert call(base, "GET", "/api/block?document=doc&start=4&end=2")[0] == 400
    assert call(base, "GET", "/api/block?document=doc&start=-1&end=2")[0] == 400
    assert call(base, "GET", "/api/block?document=doc&start=x&end=4")[0] == 400


def test_an_empty_range_is_an_append_point_not_a_bad_one(server):
    """`start == end` names a block that does not exist yet -- what the "start
    writing" box on an empty document points at. Reading it gives nothing,
    which is the truth, and writing it inserts."""
    base, workspace = server
    lines = len(workspace.source_of("doc").splitlines())

    status, payload = call(base, "GET", f"/api/block?document=doc&start={lines}&end={lines}")
    assert status == 200 and payload["markdown"] == ""

    status, _ = call(base, "POST", "/api/block",
                     {"document": "doc", "start": lines, "end": lines, "text": "Appended."})
    assert status == 200
    assert workspace.source_of("doc").rstrip().endswith("Appended.")


def test_get_block_rejects_an_unknown_document(server):
    base, _ = server
    assert call(base, "GET", "/api/block?document=nope&start=0&end=1")[0] == 404


def test_saving_a_block_rewrites_the_file_and_the_html(server):
    base, workspace = server
    status, payload = call(base, "POST", "/api/block", {
        "document": "doc", "start": 2, "end": 4, "text": "Rewritten entirely.",
    })

    assert status == 200 and payload["changed"] is True
    assert "Rewritten entirely." in (workspace.inputs / "doc.md").read_text()
    assert "Rewritten entirely." in (workspace.outputs / "doc.html").read_text()
    assert "Rewritten entirely." in payload["body"]


def test_the_response_body_is_the_article_not_a_whole_page(server):
    """It gets dropped straight into <article class="doc">."""
    base, _ = server
    _, payload = call(base, "POST", "/api/block", {
        "document": "doc", "start": 2, "end": 4, "text": "New text.",
    })
    assert "<html" not in payload["body"]
    assert "<article" not in payload["body"]
    assert "data-src-start" in payload["body"], "the new blocks stay editable"


def test_saving_an_unchanged_block_does_not_rewrite(server):
    base, workspace = server
    before = (workspace.inputs / "doc.md").read_text()
    _, payload = call(base, "POST", "/api/block", {
        "document": "doc", "start": 2, "end": 4,
        "text": "First paragraph\nacross two lines.",
    })
    assert payload["changed"] is False
    assert (workspace.inputs / "doc.md").read_text() == before


def test_saving_rejects_a_non_string(server):
    base, _ = server
    assert call(base, "POST", "/api/block", {
        "document": "doc", "start": 2, "end": 4, "text": 17,
    })[0] == 400


def test_saving_rejects_a_negative_line(server):
    base, _ = server
    assert call(base, "POST", "/api/block", {
        "document": "doc", "start": -1, "end": 4, "text": "x",
    })[0] == 400


def test_cutting_across_blocks(server):
    base, workspace = server
    last = line_of("Last paragraph.")
    status, payload = call(base, "POST", "/api/cut", {
        "document": "doc",
        "cuts": [
            {"start": 2, "end": 4, "from": 0, "to": 33},
            {"start": last, "end": last + 1, "from": 0, "to": 15},
        ],
    })

    assert status == 200 and payload["changed"] is True
    source = (workspace.inputs / "doc.md").read_text()
    assert "First paragraph" not in source
    assert "Last paragraph." not in source
    assert "Second paragraph" in source


def test_cutting_rejects_an_empty_list(server):
    base, _ = server
    assert call(base, "POST", "/api/cut", {"document": "doc", "cuts": []})[0] == 400


def test_cutting_leaves_the_document_renderable(server):
    """Cut a bolded word out and the stranded ** must not be left behind."""
    base, workspace = server
    line = line_of("Second paragraph with **bold** in it.")
    visible = "Second paragraph with bold in it."
    at = visible.index("bold")

    _, payload = call(base, "POST", "/api/cut", {
        "document": "doc",
        "cuts": [{"start": line, "end": line + 1, "from": at, "to": at + 4}],
    })

    assert payload["changed"] is True
    source = (workspace.inputs / "doc.md").read_text()
    assert "bold" not in source
    assert "****" not in source, "emptied delimiters must not survive"
    assert "Second paragraph with" in source
    render_markdown(source)  # must not raise


def test_an_edit_survives_a_round_trip_through_the_page(server):
    """Save, then read the block back: what you typed is what is there."""
    base, _ = server
    call(base, "POST", "/api/block", {
        "document": "doc", "start": 2, "end": 4, "text": "Round trip.",
    })
    _, payload = call(base, "GET", "/api/block?document=doc&start=2&end=3")
    assert payload["markdown"] == "Round trip."


# --- the browser side -----------------------------------------------------

def test_editing_is_gated_on_the_server_being_there():
    source = (ASSETS / "edit.js").read_text(encoding="utf-8")
    assert "ui.api()" in source
    assert "ready = true" in source
    assert "if (!ready" in source


def test_the_editor_never_shows_the_whole_document_as_markdown():
    """One block at a time is the whole point; no full-source mode."""
    source = (ASSETS / "edit.js").read_text(encoding="utf-8")
    assert "/api/block?document=" in source
    assert "/api/source" not in source


def test_blur_is_the_save_gesture():
    source = (ASSETS / "edit.js").read_text(encoding="utf-8")
    assert 'addEventListener("blur", commit)' in source


def test_clicking_a_highlight_still_belongs_to_annotate_js():
    source = (ASSETS / "edit.js").read_text(encoding="utf-8")
    assert 'closest("mark.hl")' in source


def test_notes_are_re_adopted_after_a_swap():
    """Replacing the article orphans every note element behind it.

    The swap itself lives in ui.js, shared with the refresh button; edit.js
    just calls it.
    """
    ui = (ASSETS / "ui.js").read_text(encoding="utf-8")
    edit = (ASSETS / "edit.js").read_text(encoding="utf-8")
    notes = (ASSETS / "notes.js").read_text(encoding="utf-8")
    assert "window.mdweave.refresh()" in ui
    assert "ui.adopt(" in edit
    assert "refresh: refresh" in notes


# --- bold, italic, and back to plain ----------------------------------------
#
# The other half of the selection menu. Unlike a comment or a highlight this
# edits the *markdown*: `**` goes into the file and travels with it. Everything
# below is about the seam between what the reader can see -- visible characters
# of a rendered block -- and where those characters came from in the source.

from mdweave.edits import Span, apply_formats, format_block  # noqa: E402


@pytest.mark.parametrize(
    "source, start, stop, style, want",
    [
        ("alpha bravo charlie", 6, 11, "bold", "alpha **bravo** charlie"),
        ("alpha bravo charlie", 6, 11, "italic", "alpha *bravo* charlie"),
        # A drag very often takes a space with it, and markdown will not open
        # an emphasis run against one -- `** bravo **` is four asterisks.
        ("alpha bravo charlie", 5, 12, "bold", "alpha **bravo** charlie"),
        ("alpha bravo charlie", 0, 19, "bold", "**alpha bravo charlie**"),
        # Already exactly this: nesting it would not make it more bold.
        ("alpha **bravo** charlie", 6, 11, "bold", "alpha **bravo** charlie"),
        # Emphasis inside emphasis is a real thing and stays one.
        ("alpha **bravo** charlie", 6, 11, "italic", "alpha ***bravo*** charlie"),
        ("alpha *bravo* charlie", 6, 11, "bold", "alpha ***bravo*** charlie"),
        # A run of three is bold *and* italic, so both are already on. Neither
        # a suffix test nor a length test sees that: `**` ends with `*`, and
        # `***` is bold without being two characters long.
        ("alpha ***bravo*** charlie", 6, 11, "bold", "alpha ***bravo*** charlie"),
        ("alpha ***bravo*** charlie", 6, 11, "italic", "alpha ***bravo*** charlie"),
        ("alpha *bravo* charlie", 6, 11, "italic", "alpha *bravo* charlie"),
    ],
)
def test_wrapping_a_selection(source, start, stop, style, want):
    assert format_block(source, start, stop, style) == want


@pytest.mark.parametrize(
    "source, start, stop, want",
    [
        ("alpha **bravo** charlie", 6, 11, "alpha bravo charlie"),
        ("alpha *bravo* charlie", 6, 11, "alpha bravo charlie"),
        ("alpha ***bravo*** charlie", 6, 11, "alpha bravo charlie"),
        # Part of a run: the whole run goes. Removing half a pair would leave
        # the other half as a literal asterisk, and splitting the run in two is
        # a larger promise than "make this plain" makes.
        ("alpha **bravo charlie** delta", 6, 11, "alpha bravo charlie delta"),
        ("nothing to undo here", 0, 7, "nothing to undo here"),
    ],
)
def test_taking_emphasis_off(source, start, stop, want):
    assert format_block(source, start, stop, "plain") == want


def test_a_marker_is_never_put_inside_a_code_span():
    """The reader selected visible text and cannot see where the backticks
    are. A marker dropped between them is a literal asterisk and breaks the
    span, so a boundary landing inside one is pushed out to its edge."""
    # Visible text is "use a*b here"; 4..9 is "a*b h", which starts inside the
    # code span.
    assert format_block("use `a*b` here", 4, 9, "bold") == "use **`a*b` h**ere"
    assert "`a*b`" in format_block("use `a*b` here", 4, 9, "bold")


def test_an_asterisk_inside_code_is_not_an_emphasis_delimiter():
    assert format_block("a `x*y*z` b", 0, 1, "plain") == "a `x*y*z` b"


def test_a_selection_with_nothing_in_it_changes_nothing():
    assert format_block("alpha bravo", 3, 3, "bold") == "alpha bravo"
    assert format_block("alpha bravo", 5, 6, "bold") == "alpha bravo"  # just a space


def test_an_unknown_style_is_refused():
    with pytest.raises(ValueError):
        format_block("alpha", 0, 5, "underline")


def test_formatting_spans_several_blocks_one_at_a_time():
    """A selection across two paragraphs cannot be one emphasis run, so each
    block gets its own -- and the blocks are done bottom-first, or the line
    numbers in the ones above would be stale by the time they are reached."""
    source = "# Title\n\nfirst paragraph\n\nsecond paragraph\n"
    out = apply_formats(
        source, [Span(2, 3, 0, 5), Span(4, 5, 0, 6)], "bold"
    )
    assert "**first** paragraph" in out
    assert "**second** paragraph" in out


# --- through the endpoint ---------------------------------------------------

def test_the_endpoint_rewrites_the_markdown_and_the_page(server):
    base, workspace = server
    line, end, start, stop = _first_paragraph(workspace)

    status, _ = call(base, "POST", "/api/format", {
        "document": "doc",
        "style": "bold",
        "spans": [{"start": line, "end": end, "from": start, "to": stop}],
    })

    assert status == 200
    assert "**First**" in workspace.source_of("doc")
    assert "<strong>First</strong>" in (workspace.outputs / "doc.html").read_text(
        encoding="utf-8"
    )


def test_the_endpoint_refuses_a_style_it_does_not_know(server):
    base, workspace = server
    line, end, start, stop = _first_paragraph(workspace)
    before = workspace.source_of("doc")

    status, payload = call(base, "POST", "/api/format", {
        "document": "doc", "style": "blink",
        "spans": [{"start": line, "end": end, "from": start, "to": stop}],
    })

    assert status == 400 and "unknown style" in payload["error"]
    assert workspace.source_of("doc") == before


def _first_paragraph(workspace):
    """The line range of the first paragraph, and the first word in it."""
    lines = workspace.source_of("doc").splitlines()
    for n, text in enumerate(lines):
        if text.strip() and not text.startswith("#"):
            return n, n + 2, 0, len(text.split()[0])
    raise AssertionError("no paragraph in the fixture")
