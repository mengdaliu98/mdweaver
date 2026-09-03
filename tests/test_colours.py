"""Tests for the annotation colour palette.

Five colours, pickable in the browser and stored in the sidecar. Two things
here are worth more than a spot check. The older six-name palette is still in
the user's files, so a render has to resolve those names without rewriting
them; and the four values per token only earn their keep if the fill, the pin
and the card stay legible together, which is arithmetic and so is testable.
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
    LEGACY_COLOR_ALIASES,
    Annotation,
    Comment,
    TextTarget,
    resolve_color_token,
)
from mdweave.render import render_document
from mdweave.serve import Workspace, make_server
from mdweave.sources import sidecar

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"
THEME = Path(__file__).resolve().parents[1] / "mdweave" / "theme"
ANNOTATIONS_CSS = (THEME / "annotations.css").read_text(encoding="utf-8")
ANNOTATE_JS = (ASSETS / "annotate.js").read_text(encoding="utf-8")


def ann(**kw) -> Annotation:
    kw.setdefault("thread", [Comment(body="note body")])
    return Annotation(id="a1", target=TextTarget(quote="CRAM/BAM"), **kw)


# --- the palette ----------------------------------------------------------

def test_the_palette_is_five_colours():
    assert COLOR_TOKENS == ("yellow", "orange", "green", "pink", "purple")
    assert DEFAULT_COLOR in COLOR_TOKENS


@pytest.mark.parametrize("token", COLOR_TOKENS)
def test_every_token_defines_all_four_of_its_values(token):
    for name in ("hl-{}-bg", "hl-{}-edge", "note-{}-bg", "note-{}-ink"):
        assert f"--{name.format(token)}:" in ANNOTATIONS_CSS


@pytest.mark.parametrize("token", COLOR_TOKENS)
def test_every_token_is_mapped_onto_the_variables_the_components_read(token):
    """A token declared but never mapped renders as an unstyled highlight."""
    for selector in (f".hl--{token}", f".note--{token}", f".swatch--{token}"):
        assert selector in ANNOTATIONS_CSS


def test_the_dark_scheme_restyles_every_card():
    """A token missed here shows dark text on a dark background."""
    dark = ANNOTATIONS_CSS.split("prefers-color-scheme: dark")[1]
    for token in COLOR_TOKENS:
        assert f"--note-{token}-bg:" in dark
        assert f"--note-{token}-ink:" in dark


# --- contrast -------------------------------------------------------------

# The page's own colours, from base.css: black-ish text on white.
PAGE_INK = (24, 27, 33)
PAGE_SURFACE = (255, 255, 255)
AA = 4.5

_VALUE = re.compile(r"--([\w-]+):\s*rgb\(([^)]+)\)")


def palette() -> dict[str, tuple[float, ...]]:
    """Every `--name: rgb(r g b / a)` in the light-mode block, as numbers."""
    light = ANNOTATIONS_CSS.split("prefers-color-scheme: dark")[0]
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
    assert contrast(values[f"hl-{token}-edge"], PAGE_SURFACE) >= AA


@pytest.mark.parametrize("token", COLOR_TOKENS)
def test_a_card_is_readable_against_its_own_background(token):
    values = palette()
    assert contrast(values[f"note-{token}-bg"], values[f"note-{token}-ink"]) >= AA


# --- the palette these five replaced --------------------------------------

@pytest.mark.parametrize(
    "legacy, token",
    [
        ("amber", "yellow"),
        ("rose", "pink"),
        ("mint", "green"),
        ("violet", "purple"),
        ("sky", "purple"),
        ("slate", "yellow"),
    ],
)
def test_a_legacy_colour_name_resolves_to_its_replacement(legacy, token):
    assert resolve_color_token(legacy) == token
    assert ann(color=legacy).color_token == token


def test_two_of_the_legacy_names_lose_a_distinction():
    """Deliberate: `sky` and `slate` have no counterpart among the five.

    Annotations that used to be told apart by colour now look the same. The
    only way back is to recolour them, which the picker can do.
    """
    assert resolve_color_token("sky") == resolve_color_token("violet") == "purple"
    assert resolve_color_token("slate") == resolve_color_token("amber") == "yellow"


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
    assert restored.color_token == "yellow"
    assert json.loads(path.read_text())["annotations"][0]["color"] == "amber"


def test_a_legacy_colour_renders_as_its_replacement():
    html = render_document("Serve over CRAM/BAM.", [ann(color="sky")]).html

    assert "hl--purple" in html
    assert "note--purple" in html
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
    status, payload = comment(base, color="green")

    assert status == 201
    assert payload["annotation"]["color"] == "green"
    assert saved_in(workspace).color == "green"
    assert "hl--green" in (workspace.outputs / "doc.html").read_text(encoding="utf-8")


def test_patch_recolours_an_annotation_in_the_sidecar_and_the_page(server):
    base, workspace = server
    _, payload = comment(base)
    ann_id = payload["annotation"]["id"]

    status, body = call(
        base, "PATCH", f"/api/annotations/{ann_id}", {"document": "doc", "color": "pink"}
    )
    assert status == 200
    assert body["annotation"]["color"] == "pink"
    assert saved_in(workspace).color == "pink"

    html = (workspace.outputs / "doc.html").read_text(encoding="utf-8")
    assert "hl--pink" in html
    assert "hl--yellow" not in html


def test_recolouring_disturbs_nothing_else_about_the_annotation(server):
    base, _ = server
    _, created = comment(base)
    before = created["annotation"]

    call(base, "PATCH", f"/api/annotations/{before['id']}",
         {"document": "doc", "offset": {"dx": 40, "dy": 8}})
    _, body = call(base, "PATCH", f"/api/annotations/{before['id']}",
                   {"document": "doc", "color": "orange"})
    after = body["annotation"]

    assert after["target"] == before["target"]
    assert after["thread"] == before["thread"]
    assert after["offset"] == {"dx": 40.0, "dy": 8.0}


@pytest.mark.parametrize("color", ["chartreuse", "rgb(255 61 148)", "amber", "", 7])
def test_only_a_palette_token_may_arrive_from_the_browser(server, color):
    """A raw colour is emitted into a style attribute, so it is not something
    to take from a client -- and the picker cannot produce one anyway."""
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

    call(base, "PATCH", f"/api/annotations/{ann_id}", {"document": "doc", "color": "purple"})

    assert saved_in(workspace).color == "purple"
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
    assert f'var DEFAULT_COLOR = "{DEFAULT_COLOR}";' in ANNOTATE_JS


def test_the_browser_never_offers_a_legacy_name():
    for legacy in LEGACY_COLOR_ALIASES:
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
var row = colorPicker("green", function (color) { picked.push(color); });
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
document.activeElement = swatches[COLORS.indexOf("green")];
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

    Five radio buttons sharing one tab stop is only correct if the tabindex
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

    assert result["names"] == ["Yellow", "Orange", "Green", "Pink", "Purple"]
    assert result["roles"] == ["radio"] * 5

    # One tab stop, and it is always the current colour -- never none, and
    # never five.
    assert result["before"] == {"checked": ["green"], "stops": ["green"]}
    assert result["arrowed"] == {
        "checked": ["pink"],
        "stops": ["pink"],
        "focused": "pink",
    }
    assert result["clicked"] == {"checked": ["yellow"], "stops": ["yellow"]}
    assert result["picked"] == ["pink", "yellow"]


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
