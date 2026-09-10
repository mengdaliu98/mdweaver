"""Colour schemes: the file, the arithmetic, and the two kinds of change.

The distinction everything here turns on: **switching** a scheme moves every
highlight to the same position in a different palette and rewrites nothing,
because an annotation stores a slot. **Reordering** a scheme is the opposite --
the colours move, so the annotations have to be renumbered to stay the colour
they were, and that is the only edit in this feature that touches a sidecar.

Getting those two backwards would either repaint a knowledge base nobody asked
to repaint, or silently renumber notes under a scheme that is not in use. Both
are quiet, and both are the kind of thing you find months later.
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

from mdweave import scheme as schemes
from mdweave.model import Annotation, Comment, TextTarget
from mdweave.scheme import DEFAULT_SCHEME, SLOTS, Scheme, Theme
from mdweave.serve import Workspace, make_server
from mdweave.sources import sidecar

ASSETS = Path(__file__).resolve().parents[1] / "mdweave" / "assets"
SETTINGS_JS = (ASSETS / "settings.js").read_text(encoding="utf-8")

INK = Scheme(
    name="Ink",
    sidebar="#1b1b22",
    paper="#101014",
    colors=["#7a3b3b", "#4b3b7a", "#3b4b7a", "#3b7a4b", "#7a7a3b", "#7a5b3b"],
)


# --- the file ---------------------------------------------------------------

def test_a_knowledge_base_with_no_theme_file_gets_the_palette_it_had(tmp_path):
    """Schemes changed who owns the colours, not what a fresh install looks
    like. Anything else would restyle every existing knowledge base."""
    theme = schemes.load(tmp_path)
    assert theme.current() == DEFAULT_SCHEME


def test_a_theme_survives_a_round_trip(tmp_path):
    schemes.save(tmp_path, Theme(schemes=[DEFAULT_SCHEME, INK], active="Ink"))
    back = schemes.load(tmp_path)

    assert [s.name for s in back.schemes] == ["Warm paper", "Ink"]
    assert back.current().colors == INK.colors


def test_a_mangled_theme_file_falls_back_rather_than_failing(tmp_path):
    """A broken dotfile must not take the prose down with it: the reader would
    lose their documents over a colour."""
    (tmp_path / schemes.THEME_FILE).write_text("{ not json", encoding="utf-8")
    assert schemes.load(tmp_path).current() == DEFAULT_SCHEME


def test_one_bad_scheme_does_not_lose_the_others(tmp_path):
    (tmp_path / schemes.THEME_FILE).write_text(
        json.dumps({
            "active": "Ink",
            "schemes": [{"name": "Broken", "sidebar": "red", "paper": "#fff", "colors": []},
                        INK.to_dict()],
        }),
        encoding="utf-8",
    )
    theme = schemes.load(tmp_path)
    assert [s.name for s in theme.schemes] == ["Ink"]


def test_an_active_name_nobody_has_falls_back_to_the_first(tmp_path):
    schemes.save(tmp_path, Theme(schemes=[INK], active="Ink"))
    raw = json.loads((tmp_path / schemes.THEME_FILE).read_text())
    raw["active"] = "Deleted"
    (tmp_path / schemes.THEME_FILE).write_text(json.dumps(raw), encoding="utf-8")

    assert schemes.load(tmp_path).current().name == "Ink"


@pytest.mark.parametrize(
    "broken",
    [
        {"name": "", "sidebar": "#000000", "paper": "#ffffff", "colors": ["#000000"] * 6},
        {"name": "X", "sidebar": "red", "paper": "#ffffff", "colors": ["#000000"] * 6},
        {"name": "X", "sidebar": "#000000", "paper": "#ffffff", "colors": ["#000000"] * 5},
        {"name": "X", "sidebar": "#000000", "paper": "#ffffff", "colors": ["#000000"] * 7},
        {"name": "X", "sidebar": "#000000", "paper": "#ffffff", "colors": ["nope"] * 6},
    ],
)
def test_a_scheme_has_to_be_six_colours_and_real_ones(broken):
    with pytest.raises(ValueError):
        Scheme.from_dict(broken)


# --- the arithmetic ---------------------------------------------------------

@pytest.mark.parametrize("fill", DEFAULT_SCHEME.colors + INK.colors + ["#ffffff", "#000000"])
def test_the_derived_values_stay_legible_whatever_the_fill(fill):
    """Only the fill is chosen. The pin carries white text and the card's ink
    sits on the card, so both are darkened until they clear AA -- otherwise
    picking a pale colour would produce a pin nobody can read."""
    got = schemes.derive(fill)
    assert schemes.contrast(got.edge, schemes.WHITE) >= 4.5
    assert schemes.contrast(got.note_bg, got.note_ink) >= 4.5
    assert schemes.contrast(got.dark_note_bg, got.dark_note_ink) >= 4.5


def test_a_dark_fill_is_reported_rather_than_refused():
    """The reader picked it. A warning is help; a veto is someone else's
    taste enforced through an error message."""
    assert schemes.unreadable(DEFAULT_SCHEME) == []

    complaints = schemes.unreadable(INK)
    assert complaints, "black-on-dark should be mentioned"
    assert any("page background" in c for c in complaints)


def test_the_generated_stylesheet_defines_every_slot():
    css = schemes.css(INK)
    for n in range(1, SLOTS + 1):
        for name in (f"--hl-c{n}-bg", f"--hl-c{n}-edge", f"--note-c{n}-bg", f"--note-c{n}-ink"):
            assert f"{name}:" in css
        assert f".hl--c{n}, .note--c{n}, .swatch--c{n}" in css
    assert "--paper: rgb(16 16 20)" in css
    assert "--sidebar-bg: rgb(27 27 34)" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_preview_paints_exactly_what_apply_would_write(tmp_path):
    """The browser derives the same four values as the server, or Preview is
    not a preview -- it is a different picture that happens to appear first.

    Two implementations of one piece of arithmetic is a cost paid on purpose:
    the alternative is a round trip per keystroke while dragging a colour
    picker. Paid on the condition that a test compares them, which is this.
    """
    program = """
const state = null;
const document = { getElementById: () => null, addEventListener() {} };
const window = { mdweaveUI: {}, addEventListener() {} };
__SOURCE__
const out = {};
for (const scheme of SCHEMES) out[scheme.name] = window.mdweaveSettings.previewCss(scheme);
console.log(JSON.stringify(out));
"""
    # The module is an IIFE that hangs itself off `window`; give it one.
    source = SETTINGS_JS.replace("window.mdweaveSettings =", "globalThis.mdweaveSettings =")
    source = source.replace("var ui = window.mdweaveUI;", "var ui = {};")
    source += "\nconst SCHEMES = " + json.dumps(
        [DEFAULT_SCHEME.to_dict(), INK.to_dict()]
    ) + ";\nwindow.mdweaveSettings = globalThis.mdweaveSettings;"

    script = tmp_path / "preview.js"
    script.write_text(program.replace("__SOURCE__", source), encoding="utf-8")
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    for scheme in (DEFAULT_SCHEME, INK):
        for line in got[scheme.name].splitlines():
            line = line.strip().rstrip(";")
            if not line.startswith("--"):
                continue
            assert line + ";" in schemes.css(scheme), (
                f"{scheme.name}: the browser and the server disagree about {line}"
            )


# --- switching versus reordering -------------------------------------------

DOC = "# Doc\n\nalpha bravo charlie delta echo foxtrot.\n"


@pytest.fixture()
def served(tmp_path):
    inputs, outputs = tmp_path / "markdown_inputs", tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "doc.md").write_text(DOC, encoding="utf-8")
    sidecar.save(
        sidecar.sidecar_path(inputs / "doc.md"),
        [
            Annotation(id="a1", target=TextTarget(quote="alpha"), kind="highlight", color=5),
            Annotation(id="a2", target=TextTarget(quote="bravo"), kind="highlight", color=1),
            Annotation(id="a3", target=TextTarget(quote="charlie"),
                       color="amber", thread=[Comment(body="legacy")]),
        ],
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


def call(base, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def slots(workspace) -> list[int | None]:
    return [a.slot for a in workspace.annotations_for("doc")]


def test_the_schemes_can_be_read_back(served):
    base, _ = served
    status, body = call(base, "GET", "/api/schemes")

    assert status == 200
    assert body["active"] == "Warm paper" and body["slots"] == SLOTS
    assert len(body["schemes"][0]["colors"]) == SLOTS


def test_switching_schemes_repaints_without_touching_a_sidecar(served):
    """The whole reason a colour is a slot. Every annotation keeps the number
    it had; only what that number looks like changes."""
    base, workspace = served
    before = slots(workspace)

    status, body = call(base, "POST", "/api/schemes", {
        "active": "Ink", "schemes": [DEFAULT_SCHEME.to_dict(), INK.to_dict()]
    })

    assert status == 200 and body["renumbered"] == 0
    assert slots(workspace) == before
    css = (workspace.outputs / "assets" / "mdweave.css").read_text(encoding="utf-8")
    assert "--hl-c1-bg: rgb(122 59 59)" in css, "the page was not repainted"
    assert "hl--c5" in (workspace.outputs / "doc.html").read_text(encoding="utf-8")


def test_reordering_renumbers_the_annotations_so_nothing_looks_different(served):
    """The opposite case. Dragging the fifth colour to the front means the
    reader is arranging the palette, not restyling their notes -- so every
    annotation has to follow its colour to the new position."""
    base, workspace = served
    assert slots(workspace) == [5, 1, 6]

    moved = [5, 1, 2, 3, 4, 6]  # the fifth colour is now first
    shuffled = DEFAULT_SCHEME.to_dict()
    shuffled["colors"] = [DEFAULT_SCHEME.colors[old - 1] for old in moved]

    status, body = call(base, "POST", "/api/schemes", {
        "active": "Warm paper", "schemes": [shuffled], "remap": moved
    })

    assert status == 200 and body["renumbered"] == 1
    # 5 -> 1, 1 -> 2, 6 stays where it is.
    assert slots(workspace) == [1, 2, 6]

    # And the point of all of it: the colour on the page is unchanged.
    fill = DEFAULT_SCHEME.colors[4]
    css = (workspace.outputs / "assets" / "mdweave.css").read_text(encoding="utf-8")
    rgb = tuple(int(fill[i:i + 2], 16) for i in (1, 3, 5))
    assert f"--hl-c1-bg: rgb({rgb[0]} {rgb[1]} {rgb[2]})" in css


def test_a_legacy_name_is_renumbered_into_a_number(served):
    """`amber` was slot 6 and is written as 6 once it moves, because a file
    that says both is a file where the next reader has to know the history."""
    base, workspace = served
    call(base, "POST", "/api/schemes", {
        "active": "Warm paper",
        "schemes": [DEFAULT_SCHEME.to_dict()],
        "remap": [6, 1, 2, 3, 4, 5],
    })
    stored = json.loads(sidecar.sidecar_path(workspace.inputs / "doc.md").read_text())
    assert [a["color"] for a in stored["annotations"]] == [6, 2, 1]


@pytest.mark.parametrize(
    "remap", [[1, 1, 2, 3, 4, 6], [1, 2, 3, 4, 5], [0, 1, 2, 3, 4, 5], "nope"]
)
def test_a_remap_that_is_not_a_permutation_is_refused(served, remap):
    """It renumbers every annotation in the knowledge base. Anything but a
    permutation loses or duplicates a slot, and silently."""
    base, workspace = served
    before = slots(workspace)

    status, _ = call(base, "POST", "/api/schemes", {
        "active": "Warm paper", "schemes": [DEFAULT_SCHEME.to_dict()], "remap": remap
    })

    assert status == 400
    assert slots(workspace) == before


def test_two_schemes_cannot_share_a_name(served):
    base, _ = served
    status, body = call(base, "POST", "/api/schemes", {
        "active": "Warm paper",
        "schemes": [DEFAULT_SCHEME.to_dict(), DEFAULT_SCHEME.to_dict()],
    })
    assert status == 400 and "share a name" in body["error"]


def test_activating_a_scheme_that_was_not_sent_is_refused(served):
    base, _ = served
    status, _ = call(base, "POST", "/api/schemes", {
        "active": "Nowhere", "schemes": [DEFAULT_SCHEME.to_dict()]
    })
    assert status == 400


def test_a_scheme_the_server_refuses_leaves_the_file_alone(served):
    base, workspace = served
    call(base, "POST", "/api/schemes", {
        "active": "Ink", "schemes": [DEFAULT_SCHEME.to_dict(), INK.to_dict()]
    })

    call(base, "POST", "/api/schemes", {"active": "X", "schemes": [{"name": "X"}]})

    assert workspace.theme().current().name == "Ink", "a bad request rolled the theme back"


def test_the_warnings_come_back_with_the_scheme(served):
    base, _ = served
    _, body = call(base, "POST", "/api/schemes", {
        "active": "Ink", "schemes": [DEFAULT_SCHEME.to_dict(), INK.to_dict()]
    })
    assert any("page background" in w for w in body["warnings"])


def test_the_theme_travels_with_the_documents(served):
    """Beside the prose, not beside the tool: the schemes are the reader's,
    and they follow the knowledge base to the next machine."""
    base, workspace = served
    call(base, "POST", "/api/schemes", {
        "active": "Ink", "schemes": [DEFAULT_SCHEME.to_dict(), INK.to_dict()]
    })
    assert (workspace.inputs / schemes.THEME_FILE).exists()
