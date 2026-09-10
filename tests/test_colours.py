"""Tests for the annotation colour palette.

Six colours, pickable in the browser and stored in the sidecar. Two things
here are worth more than a spot check. The older palette is still in the
user's files, so a render has to resolve those names without rewriting them;
and the four values per token only earn their keep if the fill, the pin and
the card stay legible together, which is arithmetic and so is testable.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mdweave.model import (
    COLOR_TOKENS,
    DEFAULT_COLOR,
    Annotation,
    Comment,
    TextTarget,
    resolve_color_token,
)
from mdweave.scheme import DEFAULT_SCHEME, LEGACY_SLOTS, SLOTS
from mdweave import scheme as schemes
from mdweave.render import render_document
from mdweave.serve import Workspace, make_server
from mdweave.sources import sidecar

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"
THEME = Path(__file__).resolve().parents[1] / "mdweave" / "theme"
# Two different files, and the split is the point of this change. The palette
# is generated from the active scheme -- so the tests below read what the
# renderer would actually emit, not a stylesheet someone hand-maintained --
# while annotations.css keeps only the structure, which no scheme alters.
GENERATED_CSS = schemes.css(DEFAULT_SCHEME)
STRUCTURE_CSS = (THEME / "annotations.css").read_text(encoding="utf-8")
ANNOTATE_JS = (ASSETS / "annotate.js").read_text(encoding="utf-8")


def ann(**kw) -> Annotation:
    kw.setdefault("thread", [Comment(body="note body")])
    return Annotation(id="a1", target=TextTarget(quote="CRAM/BAM"), **kw)


# --- the palette ----------------------------------------------------------

def test_the_palette_is_six_slots_and_no_hues():
    """A slot, not a colour: `c3` means "the third", whatever it looks like."""
    assert COLOR_TOKENS == ("c1", "c2", "c3", "c4", "c5", "c6")
    assert resolve_color_token(DEFAULT_COLOR) in COLOR_TOKENS
    assert len(DEFAULT_SCHEME.colors) == SLOTS


@pytest.mark.parametrize("token", COLOR_TOKENS)
def test_every_token_defines_all_four_of_its_values(token):
    for name in ("hl-{}-bg", "hl-{}-edge", "note-{}-bg", "note-{}-ink"):
        assert f"--{name.format(token)}:" in GENERATED_CSS


@pytest.mark.parametrize("token", COLOR_TOKENS)
def test_every_token_is_mapped_onto_the_variables_the_components_read(token):
    """A token declared but never mapped renders as an unstyled highlight."""
    for selector in (f".hl--{token}", f".note--{token}", f".swatch--{token}"):
        assert selector in GENERATED_CSS


def test_the_dark_scheme_restyles_every_card():
    """A token missed here shows dark text on a dark background."""
    dark = GENERATED_CSS.split("prefers-color-scheme: dark")[1]
    for token in COLOR_TOKENS:
        assert f"--note-{token}-bg:" in dark
        assert f"--note-{token}-ink:" in dark


# --- contrast -------------------------------------------------------------

# The page's own colours, from base.css: black-ish text on warm paper. WHITE
# is not the page -- it is the pin's own text, which is white whatever the
# paper behind the pin happens to be.
PAGE_INK = (24, 27, 33)
PAGE_SURFACE = (242, 239, 228)
WHITE = (255, 255, 255)
AA = 4.5

_VALUE = re.compile(r"--([\w-]+):\s*rgb\(([^)]+)\)")


def palette() -> dict[str, tuple[float, ...]]:
    """Every `--name: rgb(r g b / a)` in the light-mode block, as numbers."""
    light = GENERATED_CSS.split("prefers-color-scheme: dark")[0]
    found = {}
    for name, inside in _VALUE.findall(light):
        channels, _, alpha = inside.partition("/")
        found[name] = tuple(float(c) for c in channels.split()) + (
            float(alpha) if alpha.strip() else 1.0,
        )
    return found


def _channel(value: float) -> float:
    value /= 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def luminance(color) -> float:
    r, g, b = (_channel(c) for c in color[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(one, other) -> float:
    a, b = luminance(one), luminance(other)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def flatten(color, backdrop):
    """A translucent colour as it actually appears over `backdrop`."""
    alpha = color[3]
    return tuple(color[i] * alpha + backdrop[i] * (1 - alpha) for i in range(3))


@pytest.mark.parametrize("token", COLOR_TOKENS)
def test_body_text_stays_readable_through_the_highlight(token):
    values = palette()
    fill = flatten(values[f"hl-{token}-bg"], PAGE_SURFACE)
    assert contrast(fill, PAGE_INK) >= AA


@pytest.mark.parametrize("token", COLOR_TOKENS)
def test_the_pin_carries_white_text(token):
    """The edge colour is the pin's background, and the initial on it is white."""
    values = palette()
    assert contrast(values[f"hl-{token}-edge"], WHITE) >= AA


@pytest.mark.parametrize("token", COLOR_TOKENS)
def test_a_card_is_readable_against_its_own_background(token):
    values = palette()
    assert contrast(values[f"note-{token}-bg"], values[f"note-{token}-ink"]) >= AA


# --- the palette these five replaced --------------------------------------

@pytest.mark.parametrize(
    "legacy, slot",
    [
        ("pink", "c1"), ("purple", "c2"), ("blue", "c3"),
        ("green", "c4"), ("yellow", "c5"), ("orange", "c6"),
        ("rose", "c1"), ("violet", "c2"), ("sky", "c3"),
        ("mint", "c4"), ("slate", "c5"), ("amber", "c6"),
    ],
)
def test_a_hue_that_used_to_be_a_colour_resolves_to_the_slot_it_held(legacy, slot):
    """Two palettes' worth of names are in the reader's files. Both resolve to
    the position the colour occupied at the time, so nothing has to be
    rewritten and nothing changes appearance."""
    assert resolve_color_token(legacy) == slot
    assert ann(color=legacy).color_token == slot


def test_a_number_is_a_slot_and_a_css_colour_is_not():
    assert resolve_color_token(3) == "c3"
    assert resolve_color_token("3") == "c3"
    assert resolve_color_token(0) is None and resolve_color_token(7) is None
    assert resolve_color_token("rgb(255 0 128)") is None
    assert resolve_color_token(True) is None, "a bool is not slot 1"


def test_a_legacy_name_is_not_mistaken_for_a_raw_css_colour():
    """Without the alias map "mint" falls through as a CSS colour, and there
    is no such CSS colour -- the highlight would come out unpainted."""
    assert ann(color="mint").custom_color is None
    assert "--hl-custom" not in render_document("over CRAM/BAM", [ann(color="mint")]).html


def test_a_legacy_colour_is_not_rewritten_in_the_sidecar(tmp_path):
    """Resolving happens on the way to the page. The file is the user's."""
    path = tmp_path / "doc.ann.json"
    sidecar.save(path, [ann(color="amber")])
    (restored,) = sidecar.load(path)

    assert restored.color == "amber"
    assert restored.color_token == "c6"
    assert json.loads(path.read_text())["annotations"][0]["color"] == "amber"


def test_a_legacy_colour_renders_as_the_slot_it_names():
    html = render_document("Serve over CRAM/BAM.", [ann(color="sky")]).html

    assert "hl--c3" in html
    assert "note--c3" in html
    assert "sky" not in html


def test_a_raw_css_colour_still_bypasses_the_palette():
    custom = ann(color="rgb(255 61 148)")
    assert custom.color_token == "custom"
    assert custom.custom_color == "rgb(255 61 148)"


# --- the server -----------------------------------------------------------

DOC = "# Notes\n\nServe minimal byte ranges over CRAM/BAM + index.\n"


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
        "body": "What are these formats?",
    }
    payload.update(overrides)
    return call(base, "POST", "/api/annotations", payload)


def saved_in(workspace) -> Annotation:
    (annotation,) = sidecar.load(sidecar.sidecar_path(workspace.inputs / "doc.md"))
    return annotation


def test_a_new_comment_without_a_colour_gets_the_default(server):
    base, workspace = server
    _, payload = comment(base)

    assert payload["annotation"]["color"] == DEFAULT_COLOR
    assert saved_in(workspace).color == DEFAULT_COLOR


def test_a_new_comment_keeps_the_colour_it_was_written_in(server):
    base, workspace = server
    status, payload = comment(base, color="c4")

    assert status == 201
    assert payload["annotation"]["color"] == 4
    assert saved_in(workspace).color == 4
    assert "hl--c4" in (workspace.outputs / "doc.html").read_text(encoding="utf-8")


def test_patch_recolours_an_annotation_in_the_sidecar_and_the_page(server):
    base, workspace = server
    _, payload = comment(base)
    ann_id = payload["annotation"]["id"]

    status, body = call(
        base, "PATCH", f"/api/annotations/{ann_id}", {"document": "doc", "color": "c1"}
    )
    assert status == 200
    assert body["annotation"]["color"] == 1
    assert saved_in(workspace).color == 1

    html = (workspace.outputs / "doc.html").read_text(encoding="utf-8")
    assert "hl--c1" in html
    assert "hl--c5" not in html


def test_recolouring_disturbs_nothing_else_about_the_annotation(server):
    base, _ = server
    _, created = comment(base)
    before = created["annotation"]

    call(base, "PATCH", f"/api/annotations/{before['id']}",
         {"document": "doc", "offset": {"dx": 40, "dy": 8}})
    _, body = call(base, "PATCH", f"/api/annotations/{before['id']}",
                   {"document": "doc", "color": "c3"})
    after = body["annotation"]

    assert after["target"] == before["target"]
    assert after["thread"] == before["thread"]
    assert after["offset"] == {"dx": 40.0, "dy": 8.0}


@pytest.mark.parametrize("color", ["chartreuse", "rgb(255 61 148)", "", 7, 0, None])
def test_only_a_slot_may_arrive_from_the_browser(server, color):
    """A raw colour is emitted into a style attribute, so it is not something
    to take from a client -- and the picker cannot produce one anyway.

    A hue the palette used to have is not in this list: `amber` names slot 6
    and always did, so accepting it costs nothing. What is refused is anything
    that is not a position -- which is exactly the set that could reach a
    style attribute.
    """
    base, _ = server
    _, created = comment(base)

    status, body = call(base, "PATCH", f"/api/annotations/{created['annotation']['id']}",
                        {"document": "doc", "color": color})
    assert status == 400
    assert "unknown colour" in body["error"]


def test_a_comment_posted_in_an_unknown_colour_is_refused(server):
    base, workspace = server
    assert comment(base, color="turquoise")[0] == 400
    assert not sidecar.sidecar_path(workspace.inputs / "doc.md").exists()


def test_recolouring_replaces_a_raw_colour_rather_than_layering_over_it(server):
    """The one hand-written raw colour in the wild must be recolourable."""
    base, workspace = server
    _, created = comment(base)
    ann_id = created["annotation"]["id"]

    path = sidecar.sidecar_path(workspace.inputs / "doc.md")
    annotation = saved_in(workspace)
    annotation.color = "rgb(255 61 148)"
    sidecar.save(path, [annotation])

    call(base, "PATCH", f"/api/annotations/{ann_id}", {"document": "doc", "color": "c2"})

    assert saved_in(workspace).color == 2
    html = (workspace.outputs / "doc.html").read_text(encoding="utf-8")
    assert "hl--custom" not in html
    assert "rgb(255 61 148)" not in html


# --- the browser side -----------------------------------------------------

def test_the_browser_offers_exactly_the_palette_python_knows():
    """A swatch the server would refuse is a 400 the reader cannot explain."""
    match = re.search(r"var COLORS = \[(.+?)\];", ANNOTATE_JS)
    assert match, "COLORS not found in annotate.js"
    offered = tuple(re.findall(r'"(\w+)"', match.group(1)))
    assert offered == COLOR_TOKENS


def test_the_browser_and_python_agree_on_the_default_colour():
    assert f'var DEFAULT_COLOR = "c{DEFAULT_COLOR}";' in ANNOTATE_JS


def test_the_browser_never_offers_a_legacy_name():
    for legacy in LEGACY_SLOTS:
        assert f'"{legacy}"' not in ANNOTATE_JS


def test_changing_the_colour_of_an_existing_note_is_persisted():
    body = js_function(ANNOTATE_JS, "persistColor")
    assert '"PATCH"' in body
    assert "color: color" in body
    assert "persistColor(annId, color)" in ANNOTATE_JS


def test_choosing_a_colour_while_writing_shows_before_it_is_saved():
    """The pending highlight and the panel are the only preview there is."""
    body = js_function(ANNOTATE_JS, "openComposer")
    assert "recolor(PENDING, composer, picked)" in body
    assert "color: color" in js_function(ANNOTATE_JS, "submit")


def test_picking_a_token_sheds_a_raw_colour_completely():
    """`.hl--custom` sits below the tokens at equal specificity, so leaving it
    on would beat the colour just chosen."""
    body = js_function(ANNOTATE_JS, "recolor")
    assert 'removeProperty("--hl-custom")' in body
    assert 'removeProperty("--note-custom")' in body
    assert 'var PALETTE = COLORS.concat(["custom"]);' in ANNOTATE_JS


def test_every_swatch_has_an_accessible_name():
    body = js_function(ANNOTATE_JS, "colorPicker")
    assert 'setAttribute("aria-label", paletteName(color))' in body
    assert 'row.setAttribute("aria-label", "Highlight colour")' in body
    assert 'row.setAttribute("role", "radiogroup")' in body


# --- the swatches, actually run -------------------------------------------

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


def js_const(source: str, name: str) -> str:
    match = re.search(rf"var {name} = .+?;\n", source, re.DOTALL)
    assert match, f"{name} not found"
    return match.group(0)


# Enough of a DOM for `colorPicker` to build its group and take a keystroke.
# It touches nothing else: no layout, no fetch, no styling.
DOM_STUB = """
function El(tag) {
  this.tagName = tag;
  this.className = "";
  this.children = [];
  this.dataset = {};
  this.attrs = {};
  this.handlers = {};
  this.tabIndex = 0;
}
El.prototype.setAttribute = function (key, value) { this.attrs[key] = value; };
El.prototype.appendChild = function (child) { this.children.push(child); };
El.prototype.addEventListener = function (type, fn) {
  (this.handlers[type] = this.handlers[type] || []).push(fn);
};
El.prototype.focus = function () { document.activeElement = this; };
El.prototype.closest = function (selector) {
  return this.className.split(" ").indexOf(selector.slice(1)) === -1 ? null : this;
};

var document = {
  activeElement: null,
  createElement: function (tag) { return new El(tag); },
};

function fire(el, type, event) {
  (el.handlers[type] || []).forEach(function (fn) { fn(event); });
}
"""

DRIVER = """
var picked = [];
var row = colorPicker("c4", function (color) { picked.push(color); });
var swatches = row.children;

function where(attribute, value) {
  return swatches
    .filter(function (s) { return s.attrs[attribute] === value; })
    .map(function (s) { return s.dataset.color; });
}

function tabbable() {
  return swatches
    .filter(function (s) { return s.tabIndex === 0; })
    .map(function (s) { return s.dataset.color; });
}

var before = { checked: where("aria-checked", "true"), stops: tabbable() };

// ArrowRight from the focused swatch moves on, and selects as it moves.
document.activeElement = swatches[COLORS.indexOf("c4")];
fire(row, "keydown", { key: "ArrowRight", preventDefault: function () {} });
var arrowed = {
  checked: where("aria-checked", "true"),
  stops: tabbable(),
  focused: document.activeElement.dataset.color,
};

// A click picks directly, wherever the focus happens to be.
fire(row, "click", { target: swatches[0], stopPropagation: function () {} });

console.log(JSON.stringify({
  names: swatches.map(function (s) { return s.attrs["aria-label"]; }),
  roles: swatches.map(function (s) { return s.attrs.role; }),
  before: before,
  arrowed: arrowed,
  clicked: { checked: where("aria-checked", "true"), stops: tabbable() },
  picked: picked,
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_swatches_are_one_tab_stop_the_arrow_keys_move_within(tmp_path):
    """The keyboard contract, run rather than grepped for.

    Radio buttons sharing one tab stop is only correct if the tabindex
    actually roves and the arrows actually move the selection.
    """
    program = "\n".join(
        [
            DOM_STUB,
            js_const(ANNOTATE_JS, "COLORS"),
            js_const(ANNOTATE_JS, "ARROWS"),
            js_function(ANNOTATE_JS, "paletteName"),
            js_function(ANNOTATE_JS, "colorPicker"),
            DRIVER,
        ]
    )
    script = tmp_path / "picker.js"
    script.write_text(program, encoding="utf-8")

    done = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)

    assert result["names"] == [f"Colour {n}" for n in range(1, 7)]
    assert result["roles"] == ["radio"] * 6

    # One tab stop, and it is always the current colour -- never none, and
    # never one per swatch.
    assert result["before"] == {"checked": ["c4"], "stops": ["c4"]}
    # Right from the fourth is the fifth.
    assert result["arrowed"] == {"checked": ["c5"], "stops": ["c5"], "focused": "c5"}
    # A click lands on the first swatch, whatever the focus was.
    assert result["clicked"] == {"checked": ["c1"], "stops": ["c1"]}
    assert result["picked"] == ["c5", "c1"]


def test_note_actions_survive_an_in_place_swap():
    """Regression: a save replaced the notes layer and the footers vanished.

    `ui.adopt` swaps `#notes-layer` wholesale after an edit or a refresh, so
    every card annotate.js had decorated is discarded along with its Reset,
    Delete and colour swatches. They stayed gone until a full reload.
    """
    notes = (ASSETS / "notes.js").read_text(encoding="utf-8")
    annotate = (ASSETS / "annotate.js").read_text(encoding="utf-8")

    assert "setRefreshHandler" in notes and "setRefreshHandler" in annotate
    assert "mdw.setRefreshHandler(addNoteActions)" in annotate

    body = notes.split("function refresh()")[1].split("}")[0]
    assert "refreshHandler()" in body
    assert body.index("refreshHandler()") < body.index("layout()"), \
        "a card measured without its footer is placed as though it were shorter"


# --- the exact palette that was asked for ----------------------------------

REQUESTED = {
    "c1": (0xd4, 0xb0, 0xb5),
    "c2": (0xc3, 0xb0, 0xd4),
    "c3": (0xb0, 0xb2, 0xd4),
    "c4": (0xb0, 0xd4, 0xb1),
    "c5": (0xed, 0xdf, 0x91),
    "c6": (0xfa, 0xce, 0x98),
}


@pytest.mark.parametrize("token, rgb", sorted(REQUESTED.items()))
def test_the_fill_is_the_colour_that_was_specified(token, rgb):
    """Opaque, not tinted: these are already pastels, and putting them behind
    an alpha would show something other than the colour that was chosen."""
    values = palette()
    fill = values[f"hl-{token}-bg"]
    assert tuple(int(c) for c in fill[:3]) == rgb
    assert fill[3] == 1.0, "an alpha here would change the colour on screen"


BASE_CSS = (THEME / "base.css").read_text(encoding="utf-8")
SIDEBAR_CSS = (THEME / "sidebar.css").read_text(encoding="utf-8")


def test_the_page_and_the_panel_are_the_two_backgrounds_that_were_asked_for():
    assert "--paper:        rgb(242, 239, 228);" in BASE_CSS   # #F2EFE4
    assert "--sidebar-bg:   rgb(209, 199, 183);" in BASE_CSS   # #D1C7B7

    body = BASE_CSS.split("body {")[1].split("}")[0]
    assert "background: var(--paper);" in body
    # Anchored at a line start: `body[...] .sidebar {` matches otherwise, and
    # its one-line body says `display: none` and nothing about a colour.
    panel = SIDEBAR_CSS.split("\n.sidebar {")[1].split("}")[0]
    assert "background: var(--sidebar-bg);" in panel


def test_the_panel_paints_itself_with_one_token_throughout():
    """The row-action strips are opaque and have to match the row behind them,
    so a strip left on the old token shows as a pale rectangle on a hover."""
    assert "--surface-sunk" not in SIDEBAR_CSS


def test_the_paper_and_the_panel_are_far_enough_apart_to_see():
    """Two warm tones a hair apart would read as a rendering artefact rather
    than as an edge; the border alone should not be doing all the work."""
    paper, panel = (242, 239, 228), (209, 199, 183)
    assert contrast(paper, panel) >= 1.2


def test_the_panel_is_black_text_on_its_own_background():
    """No grey in the panel: the ink ramp de-emphasised against near-white,
    and on #D1C7B7 its faint end reads as muddy rather than quiet."""
    assert "--sidebar-ink: rgb(0, 0, 0);" in SIDEBAR_CSS
    assert "--ink-muted" not in SIDEBAR_CSS
    assert "--ink-faint" not in SIDEBAR_CSS

    # A black icon drawn at 0.65 is a grey icon.
    icon = SIDEBAR_CSS.split(".tree__icon {")[1].split("}")[0]
    chevron = SIDEBAR_CSS.split(".tree__chevron {")[1].split("}")[0]
    assert "opacity" not in icon and "opacity" not in chevron


def test_the_open_document_row_is_painted_in_the_page_colour():
    """The row for what is on screen carries the pane's own background, so the
    panel reads as having a piece cut out of it rather than as a blue stripe."""
    active = SIDEBAR_CSS.split(".tree__row--active {")[1].split("}")[0]
    assert "background: var(--paper);" in active
    assert "--accent" not in active, "the accent was what made it blue"

    # The controls laid over that row's right-hand end are opaque, so a
    # mismatch here shows up as a rectangle of the wrong colour on the row.
    strip = SIDEBAR_CSS.split(".tree__row--active ~ .tree__actions {")[1].split("}")[0]
    assert "background: var(--paper);" in strip


def test_black_is_only_the_light_scheme():
    """Black on the dark panel would be unreadable."""
    dark = SIDEBAR_CSS.split("prefers-color-scheme: dark")[1]
    assert "--sidebar-ink: var(--ink);" in dark


def test_the_swatches_are_offered_in_the_order_they_were_given():
    """The menu is read left to right, so the list is part of what was asked
    for and not just the set of colours in it."""
    assert list(COLOR_TOKENS) == list(REQUESTED)
    assert 'var COLORS = ["c1", "c2", "c3", "c4", "c5", "c6"]' in ANNOTATE_JS


def test_the_default_scheme_is_the_palette_that_was_asked_for():
    """The six fills that were chosen by hand are what a fresh knowledge base
    still gets; schemes changed who owns them, not what they are."""
    assert [tuple(int(c[i:i+2], 16) for i in (1, 3, 5)) for c in DEFAULT_SCHEME.colors] \
        == list(REQUESTED.values())


# --- painting over what is already there -----------------------------------
#
# One rule underneath every case: pressing a colour means "make the selection
# this colour", except when it is already entirely and only that colour, which
# is the only reading of a second press that is not a no-op.

@pytest.fixture()
def painting(tmp_path):
    """A served document with room to overlap things."""
    import threading
    from mdweave.serve import Workspace, make_server

    inputs, outputs = tmp_path / "markdown_inputs", tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "doc.md").write_text(
        "# Doc\n\nalpha bravo charlie delta echo foxtrot golf hotel.\n", encoding="utf-8"
    )
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


def paint(base, quote, color):
    return _post(base, "/api/annotations", {
        "document": "doc", "kind": "highlight", "quote": quote, "color": color
    })


def comment_on(base, quote, color="c4"):
    return _post(base, "/api/annotations", {
        "document": "doc", "quote": quote, "body": "a real thread", "color": color
    })


def state(workspace):
    return [
        (a.target.quote, a.color_token, a.kind)
        for a in workspace.annotations_for("doc")
    ]


def _post(base, path, payload):
    import json, urllib.error, urllib.request

    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_a_fresh_selection_is_simply_highlighted(painting):
    base, workspace = painting
    status, payload = paint(base, "bravo charlie", "c5")

    assert status == 200 and payload["annotation"]["color"] == 5
    assert state(workspace) == [("bravo charlie", "c5", "highlight")]


def test_the_same_colour_over_the_same_span_takes_it_off(painting):
    """The only reading of a second press that is not a no-op."""
    base, workspace = painting
    _, first = paint(base, "bravo charlie", "c5")

    status, payload = paint(base, "bravo charlie", "c5")

    assert status == 200
    assert payload["cleared"] == [first["annotation"]["id"]]
    assert state(workspace) == []


def test_a_different_colour_repaints_rather_than_refusing(painting):
    """It used to be rejected as an overlap, which is not what a reader means."""
    base, workspace = painting
    paint(base, "bravo charlie", "c5")

    status, payload = paint(base, "bravo charlie", "c1")

    assert status == 200
    assert payload["replaced"], "the yellow one should have been absorbed"
    assert state(workspace) == [("bravo charlie", "c1", "highlight")]


def test_a_partly_covered_selection_is_painted_not_cleared(painting):
    """Same colour, but only part of the selection had it -- so the press
    means "make all of this that colour", and clearing would lose the rest."""
    base, workspace = painting
    paint(base, "bravo charlie", "c1")

    status, payload = paint(base, "bravo charlie delta", "c1")

    assert status == 200 and "annotation" in payload
    assert state(workspace) == [("bravo charlie delta", "c1", "highlight")]


def test_mixed_colours_underneath_become_one_clean_highlight(painting):
    base, workspace = painting
    paint(base, "bravo", "c5")
    paint(base, "delta", "c4")

    status, payload = paint(base, "bravo charlie delta", "c2")

    assert status == 200
    assert len(payload["replaced"]) == 2
    assert state(workspace) == [("bravo charlie delta", "c2", "highlight")]


def test_two_adjacent_highlights_together_count_as_covering(painting):
    """Neither covers the selection alone; between them they do, so pressing
    their shared colour clears rather than repaints."""
    base, workspace = painting
    paint(base, "bravo", "c5")
    paint(base, "charlie", "c5")

    status, payload = paint(base, "bravo charlie", "c5")

    assert status == 200
    assert len(payload.get("cleared", [])) == 2
    assert state(workspace) == []


def test_part_of_a_larger_highlight_clears_the_whole_of_it(painting):
    """A highlight is one thing; splitting it in two would be a stranger
    answer than removing what was pressed."""
    base, workspace = painting
    paint(base, "golf hotel", "c3")

    status, payload = paint(base, "golf", "c3")

    assert status == 200 and payload["cleared"]
    assert state(workspace) == []


def test_a_comment_is_never_absorbed(painting):
    """Its highlight is the handle for a thread; no colour press means delete."""
    base, workspace = painting
    comment_on(base, "echo foxtrot")

    status, payload = paint(base, "echo foxtrot", "c1")

    assert status == 409
    assert "comment" in payload["error"]
    assert state(workspace) == [("echo foxtrot", "c4", "comment")]


def test_a_selection_merely_touching_a_comment_is_refused_too(painting):
    base, workspace = painting
    comment_on(base, "echo foxtrot")

    status, _ = paint(base, "delta echo", "c1")

    assert status == 409
    assert len(state(workspace)) == 1, "nothing added, nothing removed"


def test_a_highlight_carries_no_card(painting):
    base, workspace = painting
    paint(base, "bravo", "c5")
    (annotation,) = workspace.annotations_for("doc")
    assert annotation.thread == [] and not annotation.has_card


# --- no underline ----------------------------------------------------------

def test_a_highlight_has_no_rule_under_it():
    """The fill is an opaque pastel and says the colour on its own."""
    block = STRUCTURE_CSS.split("mark.hl {")[1].split("}")[0]
    # A declaration, not the word: `transition` names box-shadow too.
    assert not re.search(r"(?m)^\s*box-shadow\s*:", block)


def test_resolved_keeps_its_rule():
    """Its fill is gone, so without one it would be invisible, not quiet."""
    block = STRUCTURE_CSS.split("mark.hl--resolved {")[1].split("}")[0]
    assert "box-shadow" in block and "background: none" in block
