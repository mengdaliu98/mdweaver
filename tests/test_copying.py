"""Tests for copying a selection as markdown.

Cmd-C over rendered prose should put the *source* of that prose on the
clipboard, not the flattened text. Three halves, as it were: the extraction in
`edits.py`, which is where the thinking is; the endpoint that exposes it; and
the browser's side of the bargain, which is that everything it cannot do
degrades to the copy the browser would have made anyway.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave import edits
from mdweave.render import render_markdown
from mdweave.serve import Workspace, make_server

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"

DOC = """# Copying things

A paragraph with **bold**, a [link](https://example.com/x) and `code` in it,
running across two source lines.

## A subheading

- one
- two with *emphasis*

```python
x = 1
```

> A quoted line.

A footnote reference.[^1]

[^1]: The definition.
"""

LINES = DOC.splitlines()


def line_of(text: str) -> int:
    return LINES.index(text)


def block_at(text: str) -> tuple[int, int]:
    """The [line, end) range the renderer gives the block starting at `text`."""
    import re

    html = render_markdown(DOC)
    spans = [
        (int(a), int(b))
        for a, b in re.findall(r'data-src-start="(\d+)" data-src-end="(\d+)"', html)
    ]
    line = line_of(text)
    return next(span for span in spans if span[0] == line)


def visible(text: str) -> str:
    line, end = block_at(text)
    return edits.rendered_text_in(DOC, line, end)


def copied(text: str, start: int, stop: int) -> str:
    """What the clipboard gets for a selection inside one named block."""
    line, end = block_at(text)
    return edits.extract_spans(DOC, [edits.Span(line=line, end=end, start=start, stop=stop)])


def all_of(text: str) -> str:
    """Copy every character of a block a reader could actually reach.

    Not `0, len(body)`: a list's visible text opens with the newline before its
    first `<li>` and a fence's ends with the one after the last line of code,
    and no drag of the mouse can include either.
    """
    body = visible(text)
    return copied(text, len(body) - len(body.lstrip()), len(body.rstrip()))


# --- a whole block comes back verbatim -------------------------------------

PARAGRAPH = "A paragraph with **bold**, a [link](https://example.com/x) and `code` in it,"


def test_copying_a_whole_paragraph_gives_back_its_markdown_verbatim():
    """The property the whole feature is for: paste and nothing is lost."""
    assert all_of(PARAGRAPH) == PARAGRAPH + "\nrunning across two source lines."


def test_a_whole_heading_keeps_its_hashes():
    assert all_of("## A subheading") == "## A subheading"


def test_a_whole_list_keeps_its_bullets():
    assert visible("- one").startswith("\n"), "the fixture must exercise that newline"
    assert all_of("- one") == "- one\n- two with *emphasis*"


def test_a_whole_code_block_keeps_its_fences():
    """Deliberate: this copies markdown, so a code block arrives as one."""
    assert all_of("```python") == "```python\nx = 1\n```"


def test_a_whole_quote_keeps_its_marker():
    assert all_of("> A quoted line.") == "> A quoted line."


def test_a_whole_footnote_reference_comes_back_as_source():
    """Rendered it reads `[1]`; what belongs on the clipboard is `[^1]`."""
    assert visible("A footnote reference.[^1]") == "A footnote reference.[1]"
    assert all_of("A footnote reference.[^1]") == "A footnote reference.[^1]"


# --- inline syntax inside a partial selection ------------------------------

@pytest.mark.parametrize(
    "source, span, expected",
    [
        # A construct the selection covers entirely keeps its syntax.
        ("a **bold** word", (2, 6), "**bold**"),
        ("a *thin* word", (2, 6), "*thin*"),
        ("a ~~struck~~ word", (2, 8), "~~struck~~"),
        ("use `code` here", (4, 8), "`code`"),
        ("see [a link](http://x) now", (4, 10), "[a link](http://x)"),
        # One it only overlaps loses it, and comes back as plain text.
        ("a **bold** word", (2, 4), "bo"),
        ("use `code` here", (5, 8), "ode"),
        ("see [a link](http://x) now", (6, 10), "link"),
        # A selection running out of a construct keeps the construct whole
        # rather than stranding half its delimiters.
        ("a **bold** word", (0, 6), "a **bold**"),
        ("a **bold** word", (2, 9), "**bold** wo"),
        ("see [a link](http://x) now", (4, 14), "[a link](http://x) now"),
    ],
)
def test_inline_syntax_survives_exactly_when_the_selection_covers_it(source, span, expected):
    assert edits.extract_block(source, span[0], span[1]) == expected


def test_a_partial_selection_never_leaks_a_url():
    """`link](http://x)` on the clipboard is worse than no formatting at all."""
    for stop in range(6, 12):
        assert "](" not in edits.extract_block("see [a link](http://x) now", 6, stop)


def test_the_hashes_of_a_heading_are_not_copied_with_part_of_it():
    assert edits.extract_block("## A subheading", 2, 12) == "subheading"


def test_an_empty_selection_extracts_nothing():
    assert edits.extract_block("a **bold** word", 4, 4) == ""
    assert edits.extract_spans(DOC, []) == ""


DOCUMENTS = [
    "A paragraph with **bold**, a [link](http://x) and `code` in it.\n",
    "# Heading\n\nText with *emphasis* and ~~a strikethrough~~ in it.\n",
    "- one **two**\n- three `four`\n",
]


@pytest.mark.parametrize("markdown", DOCUMENTS)
def test_every_selection_either_renders_back_or_degrades_to_plain_text(markdown):
    """The invariant that keeps broken markdown off the clipboard.

    An extraction is only allowed two outcomes: markdown that renders as
    exactly the text that was selected, or that text with no markup at all.
    Anything in between -- a stranded `**`, half a link -- pastes as garbage,
    and this sweep over every word-aligned span is what rules it out.
    """
    import re

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(render_markdown(markdown), "html.parser")
    checked = 0

    for element in soup.select("[data-src-start]"):
        line, end = int(element["data-src-start"]), int(element["data-src-end"])
        block = edits.block_source(markdown, line, end)
        text = element.get_text()
        bounds = [0] + [m.end() for m in re.finditer(r"\S+", text)] + [len(text)]

        for i, start in enumerate(bounds):
            for stop in bounds[i + 1 :]:
                wanted = text[start:stop]
                if not wanted.strip():
                    continue
                got = edits.extract_block(block, start, stop, rendered=text)
                assert got == block or got == wanted or (
                    edits.rendered_text(got).strip() == wanted.strip()
                ), f"{wanted!r} extracted as {got!r}"
                checked += 1

    assert checked > 10


# --- selections that cross blocks ------------------------------------------

def test_blocks_are_joined_by_the_blank_line_that_separates_them():
    """Otherwise the paste is one run-on paragraph."""
    quote = block_at("> A quoted line.")
    fence = block_at("```python")
    got = edits.extract_spans(DOC, [
        edits.Span(line=fence[0], end=fence[1], start=0, stop=5),
        edits.Span(line=quote[0], end=quote[1], start=1, stop=15),
    ])
    assert got == "```python\nx = 1\n```\n\n> A quoted line."


def test_blocks_come_back_in_document_order():
    """The browser sends them in order; a hand-rolled payload need not."""
    heading = block_at("## A subheading")
    quote = block_at("> A quoted line.")
    got = edits.extract_spans(DOC, [
        edits.Span(line=quote[0], end=quote[1], start=0, stop=14),
        edits.Span(line=heading[0], end=heading[1], start=0, stop=12),
    ])
    assert got.index("subheading") < got.index("quoted")


def test_a_selection_across_three_blocks_keeps_the_middle_one_whole():
    """Only the ends of a selection are partial; everything between is not."""
    para = block_at(PARAGRAPH)
    heading = block_at("## A subheading")
    items = block_at("- one")
    tail = len(visible(PARAGRAPH))
    got = edits.extract_spans(DOC, [
        edits.Span(line=para[0], end=para[1], start=tail - 13, stop=tail),
        edits.Span(line=heading[0], end=heading[1], start=0, stop=12),
        edits.Span(line=items[0], end=items[1], start=1, stop=4),
    ])
    assert "## A subheading" in got
    assert got.endswith("\n\none")


def test_a_block_measured_against_the_whole_document_not_on_its_own():
    """The invariant `rendered_text_in` exists for, on the copying side.

    `[^1]` renders as `[1]` only when its definition is in scope, so a block
    measured alone is one character narrower than the one the browser measured
    and every offset after the reference lands in the wrong place.
    """
    line, end = block_at("A footnote reference.[^1]")
    block = edits.block_source(DOC, line, end)
    assert edits.rendered_text(block) != edits.rendered_text_in(DOC, line, end)

    # "reference" -- offsets taken from what the browser sees.
    body = edits.rendered_text_in(DOC, line, end)
    at = body.index("reference")
    assert copied("A footnote reference.[^1]", at, at + 9) == "reference"


def test_a_span_pointing_off_the_end_of_the_document_is_ignored():
    assert edits.extract_spans(DOC, [edits.Span(line=900, end=901, start=0, stop=4)]) == ""


def test_a_cut_is_a_span_so_the_two_endpoints_parse_the_same_payload():
    assert issubclass(edits.Cut, edits.Span)


# --- the endpoint -----------------------------------------------------------

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


def test_extract_hands_back_the_markdown_for_a_selection(server):
    base, _ = server
    line, end = block_at("## A subheading")
    status, payload = call(base, "POST", "/api/extract", {
        "document": "doc",
        "spans": [{"start": line, "end": end, "from": 0, "to": 12}],
    })
    assert status == 200
    assert payload["markdown"] == "## A subheading"


def test_extract_writes_nothing(server):
    """A copy is a read. Nothing on disk may move because of one."""
    base, workspace = server
    before = {
        path: path.read_bytes()
        for path in sorted(workspace.outputs.rglob("*")) + [workspace.inputs / "doc.md"]
        if path.is_file()
    }

    line, end = block_at("> A quoted line.")
    call(base, "POST", "/api/extract", {
        "document": "doc",
        "spans": [{"start": line, "end": end, "from": 0, "to": 14}],
    })

    assert {path: path.read_bytes() for path in before} == before


def test_extract_rejects_an_empty_list(server):
    base, _ = server
    assert call(base, "POST", "/api/extract", {"document": "doc", "spans": []})[0] == 400


def test_extract_rejects_a_malformed_span(server):
    base, _ = server
    for spans in ([{"start": 0, "end": 1, "from": -1, "to": 4}],
                  [{"start": 4, "end": 2, "from": 0, "to": 4}],
                  ["not an object"]):
        assert call(base, "POST", "/api/extract", {
            "document": "doc", "spans": spans,
        })[0] == 400


def test_extract_rejects_an_unknown_document(server):
    base, _ = server
    assert call(base, "POST", "/api/extract", {
        "document": "nope", "spans": [{"start": 0, "end": 1, "from": 0, "to": 4}],
    })[0] == 404


def test_cutting_still_works_now_that_it_shares_the_payload_parsing(server):
    base, workspace = server
    line, end = block_at("> A quoted line.")
    status, payload = call(base, "POST", "/api/cut", {
        "document": "doc",
        "cuts": [{"start": line, "end": end, "from": 0, "to": 14}],
    })
    assert status == 200 and payload["changed"] is True
    assert "A quoted line." not in (workspace.inputs / "doc.md").read_text()


# --- the browser side -------------------------------------------------------

def test_the_selection_decomposition_is_shared_not_duplicated():
    """One idea of "character 40 of this paragraph", used by both features."""
    ui = (ASSETS / "ui.js").read_text(encoding="utf-8")
    edit = (ASSETS / "edit.js").read_text(encoding="utf-8")
    copy = (ASSETS / "copy.js").read_text(encoding="utf-8")

    assert "blockRanges: blockRanges" in ui
    assert "ui.blockRanges(" in edit and "ui.blockRanges(" in copy
    # Neither keeps its own copy of the range walk.
    for module in (edit, copy):
        assert "function offsetIn(" not in module
        assert "intersectsNode" not in module


def test_copying_is_gated_on_the_server_being_there():
    """No server, no markdown -- and a file:// page still copies its prose."""
    source = (ASSETS / "copy.js").read_text(encoding="utf-8")
    assert "ui.api()" in source
    assert "ready = true" in source
    assert "if (!ready" in source


def test_the_rendered_text_is_written_before_the_copy_is_taken_over():
    """The ordering is the whole fallback story, so it is asserted literally."""
    source = (ASSETS / "copy.js").read_text(encoding="utf-8")
    set_at = source.index("clipboardData.setData(")
    prevented_at = source.index("event.preventDefault()")
    assert set_at < prevented_at


def test_the_new_asset_is_registered_in_both_places():
    """Listed for the page but not copied next to it is a 404 on every load."""
    render = (Path(__file__).resolve().parents[1] / "mdweave" / "render.py").read_text(
        encoding="utf-8"
    )
    assert '"assets/copy.js"' in render
    assert '"copy.js"' in render


# --- the browser side, actually run ----------------------------------------

# copy.js touches nothing but a handful of globals, so a stub of each is enough
# to run its decisions for real rather than reading them off the source. What
# is being checked is the degradation: every path that cannot produce markdown
# has to leave the browser's own copy alone.
HARNESS = """
const fs = require("fs");
const source = fs.readFileSync(process.argv[2], "utf8");

function scenario(options) {
  const log = { prevented: false, copied: null, written: null, requested: null };
  const listeners = {};

  global.window = {
    mdweaveUI: {
      blockRanges: function () {
        return options.spans === false ? null : [{ start: 2, end: 4, from: 0, to: 5 }];
      },
      api: function () {
        return Promise.resolve(options.server === false ? null : { ok: true });
      },
    },
    getSelection: function () {
      return {
        isCollapsed: !!options.collapsed,
        rangeCount: 1,
        getRangeAt: function () {
          return { commonAncestorContainer: {} };
        },
        toString: function () {
          return "rendered text";
        },
      };
    },
    mdweaveEdit: { isBusy: function () { return !!options.busy; } },
  };

  global.document = {
    body: { dataset: { document: "doc" } },
    getElementById: function (id) {
      if (id !== "doc") return null;
      return { contains: function () { return options.inDoc !== false; } };
    },
    addEventListener: function (name, fn) { listeners[name] = fn; },
  };

  // defineProperty, not assignment: node has had its own read-only `navigator`
  // since 21, and assigning to it fails silently.
  Object.defineProperty(global, "navigator", {
    configurable: true,
    value: options.clipboard === false ? {} : {
      clipboard: {
        writeText: function (text) { log.written = text; return Promise.resolve(); },
      },
    },
  });

  global.fetch = function (url, init) {
    log.requested = JSON.parse(init.body);
    if (options.offline) return Promise.reject(new Error("offline"));
    return Promise.resolve({
      ok: options.status !== "error",
      json: function () { return Promise.resolve({ markdown: "## markdown" }); },
    });
  };

  eval(source);

  const settle = function () {
    return new Promise(function (resolve) { setTimeout(resolve, 0); });
  };

  return settle().then(function () {
    listeners.copy({
      target: { tagName: options.tagName || "P" },
      clipboardData: { setData: function (type, value) { log.copied = value; } },
      preventDefault: function () { log.prevented = true; },
    });
    return settle().then(settle).then(function () { return log; });
  });
}

const cases = JSON.parse(process.argv[3]);
const out = {};
cases.reduce(function (chain, entry) {
  return chain.then(function () {
    return scenario(entry.options).then(function (log) { out[entry.name] = log; });
  });
}, Promise.resolve()).then(function () {
  process.stdout.write(JSON.stringify(out));
});
"""

CASES = [
    {"name": "prose", "options": {}},
    {"name": "textarea", "options": {"tagName": "TEXTAREA"}},
    {"name": "editing", "options": {"busy": True}},
    {"name": "outside", "options": {"inDoc": False}},
    {"name": "not_a_block", "options": {"spans": False}},
    {"name": "no_clipboard", "options": {"clipboard": False}},
    {"name": "no_server", "options": {"server": False}},
    {"name": "offline", "options": {"offline": True}},
    {"name": "refused", "options": {"status": "error"}},
]


@pytest.fixture(scope="module")
def behaviour(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    harness = tmp_path_factory.mktemp("copyjs") / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run(
        ["node", str(harness), str(ASSETS / "copy.js"), json.dumps(CASES)],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_copy_of_prose_puts_the_markdown_on_the_clipboard(behaviour):
    run = behaviour["prose"]
    assert run["prevented"] is True
    assert run["written"] == "## markdown"
    assert run["requested"] == {
        "document": "doc",
        "spans": [{"start": 2, "end": 4, "from": 0, "to": 5}],
    }


def test_the_rendered_text_goes_on_the_clipboard_first(behaviour):
    """Cancelling the copy without replacing it would lose the paragraph."""
    assert behaviour["prose"]["copied"] == "rendered text"


@pytest.mark.parametrize("name", ["textarea", "editing", "outside", "not_a_block",
                                  "no_clipboard", "no_server"])
def test_a_copy_we_cannot_improve_on_is_left_to_the_browser(behaviour, name):
    run = behaviour[name]
    assert run["prevented"] is False
    assert run["copied"] is None, "the default copy must not even be touched"
    assert run["requested"] is None


@pytest.mark.parametrize("name", ["offline", "refused"])
def test_a_failed_request_leaves_the_plain_copy_in_place(behaviour, name):
    """It is already on the clipboard, so there is nothing to put right."""
    run = behaviour[name]
    assert run["copied"] == "rendered text"
    assert run["written"] is None
