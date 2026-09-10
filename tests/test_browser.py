"""The tests only a real browser can answer.

Everything else in this suite reasons about the browser code without running
it: a function is lifted out of its file and driven under `node` against a
stub DOM, or the source is read and asserted about. That catches logic, and
it caught none of the three bugs that actually reached the reader --

  * dragging a folder up or down threw `Cannot set properties of null`, while
    every source-level test around it passed, because the shape of the code
    was right and only the runtime value was wrong;
  * creating a folder answered `unknown folder`, from a server that was
    perfectly self-consistent until something used it;
  * a slash in a typed name quietly made a hierarchy.

All three needed a page, a click and a drag. So: a real page, real clicks,
real drags, and `pageerror` wired up -- an uncaught exception in the panel
fails the test rather than sitting in a console nobody is reading.

Skipped, not failed, when the browser is not installed:

    uv pip install --python .venv/bin/python playwright && .venv/bin/playwright install chromium
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")

from playwright.sync_api import sync_playwright  # noqa: E402

from mdweave.serve import Workspace, make_server  # noqa: E402
from mdweave.tree import ORDER_FILE  # noqa: E402


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as play:
        try:
            engine = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001 -- the package without the binary
            pytest.skip(f"chromium is not installed: {exc}")
        yield engine
        engine.close()


@pytest.fixture()
def served(tmp_path):
    """A knowledge base with two folders and two documents, served."""
    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    (inputs / "notes").mkdir(parents=True)
    (inputs / "archive").mkdir()
    (inputs / "top.md").write_text("# Top\n\nAt the root.\n", encoding="utf-8")
    (inputs / "notes" / "weekly.md").write_text("# Weekly\n\nInside.\n", encoding="utf-8")

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


@pytest.fixture()
def page(browser, served):
    """A loaded page whose uncaught errors fail the test that caused them."""
    base, _ = served
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()

    broke: list[str] = []
    page.on("pageerror", lambda exc: broke.append(str(exc)))

    page.goto(f"{base}/top.html", wait_until="networkidle")
    # The row controls only appear once the server has answered /api/ping.
    page.wait_for_selector(".sidebar--manageable", timeout=10_000)

    yield page

    context.close()
    assert not broke, "the panel threw: " + "; ".join(broke)


def folder_row(page, name: str):
    return page.locator(f'.tree__folder[data-path="{name}"] > summary.tree__row--folder')


# The root list, and only it. Every nested `<ul>` is a `.tree` as well, so
# `.tree > .tree__item` reaches into the folders and reports their contents as
# though they were at the top level.
ROOT_LIST = 'ul.tree[data-path=""]'


def rows(page) -> list[str]:
    """The top-level row names, in the order they are drawn."""
    return page.eval_on_selector_all(
        f'{ROOT_LIST} > .tree__item',
        """items => items.map(i => {
             const f = i.querySelector('.tree__folder');
             if (f) return f.dataset.path;
             const a = i.querySelector('.tree__row--file');
             return a ? a.dataset.name : '?';
           })""",
    )


# --- dragging ---------------------------------------------------------------

def test_a_folder_can_be_dragged_below_another(page, served):
    """Regression: this threw `Cannot set properties of null (setting
    'sidebar')`. A reorder inside one folder issues no move, so that promise
    resolves to null -- and the code merging the two responses wrote to it."""
    _, workspace = served
    assert rows(page)[:2] == ["archive", "notes"], "the fixture starts sorted"

    target = folder_row(page, "notes")
    box = target.bounding_box()
    # The lower edge of a row means "after it"; the middle of a folder would
    # mean "inside it", which is a different operation.
    page.drag_and_drop(
        f'.tree__folder[data-path="archive"] > summary.tree__row--folder',
        f'.tree__folder[data-path="notes"] > summary.tree__row--folder',
        target_position={"x": box["width"] / 2, "y": box["height"] - 2},
    )

    page.wait_for_function(
        """() => {
             const first = document.querySelector(
               'ul.tree[data-path=""] > .tree__item .tree__folder');
             return first && first.dataset.path === 'notes';
           }""",
        timeout=10_000,
    )
    assert rows(page)[:2] == ["notes", "archive"]

    # And it reached the disk, not just the DOM.
    order = json.loads((workspace.inputs / ORDER_FILE).read_text(encoding="utf-8"))
    assert order[""].index("notes") < order[""].index("archive")


def test_a_document_can_be_dragged_into_a_folder(page, served):
    """The middle of a folder row means inside it."""
    _, workspace = served
    target = folder_row(page, "archive")
    box = target.bounding_box()

    page.drag_and_drop(
        '.tree__row--file[data-name="top"]',
        '.tree__folder[data-path="archive"] > summary.tree__row--folder',
        target_position={"x": box["width"] / 2, "y": box["height"] / 2},
    )

    page.wait_for_function(
        """() => !document.querySelector(
             'ul.tree[data-path=""] > .tree__item > .tree__file')""",
        timeout=10_000,
    )
    assert (workspace.inputs / "archive" / "top.md").exists()
    assert not (workspace.inputs / "top.md").exists()


# --- the prompts ------------------------------------------------------------

def new_folder_at_root(page, answer: str):
    page.once("dialog", lambda dialog: dialog.accept(answer))
    page.click('.sidebar__actions [data-action="new-folder"]')


def test_a_folder_can_be_made_at_the_root(page, served):
    """Regression: this answered `unknown folder: 'x'`, x being the name being
    created -- the server read a typed name as a path and looked up its first
    half as a parent."""
    _, workspace = served
    new_folder_at_root(page, "Research")

    page.wait_for_selector('.tree__folder[data-path="Research"]', timeout=10_000)
    assert (workspace.inputs / "Research").is_dir()


def test_a_typed_slash_makes_one_folder_not_a_hierarchy(page, served):
    _, workspace = served
    new_folder_at_root(page, "Research/Papers")

    page.wait_for_selector('.tree__folder[data-path="Research_Papers"]', timeout=10_000)
    assert (workspace.inputs / "Research_Papers").is_dir()
    assert not (workspace.inputs / "Research").exists(), "the slash made a hierarchy"
    # And it reads as prose, the underscore being how a space is written here.
    assert page.inner_text('.tree__folder[data-path="Research_Papers"] .tree__label') \
        == "Research papers"


# --- appearance -------------------------------------------------------------

def rgb(page, selector: str, prop: str) -> str:
    return page.eval_on_selector(
        selector, f"el => getComputedStyle(el).{prop}"
    )


def test_the_panel_is_black_on_the_colour_it_was_given(page):
    """Computed from the live cascade, not read off the stylesheet: a token can
    be perfectly correct and still be overridden by something later."""
    assert rgb(page, ".sidebar", "backgroundColor") == "rgb(209, 199, 183)"
    assert rgb(page, ".tree__row--file .tree__label", "color") == "rgb(0, 0, 0)"
    assert rgb(page, ".sidebar__title", "color") == "rgb(0, 0, 0)"
    # An icon at less than full strength is a grey icon whatever it was set to.
    assert rgb(page, ".tree__icon", "opacity") == "1"


def test_the_open_document_row_matches_the_page_behind_it(page):
    """The row for what is on screen carries the reading pane's own colour."""
    paper = rgb(page, "body", "backgroundColor")
    assert paper == "rgb(242, 239, 228)"
    assert rgb(page, ".tree__row--active", "backgroundColor") == paper


# --- the settings window ----------------------------------------------------

def open_settings(page):
    page.click("#sidebar-settings")
    page.wait_for_selector(".settings", timeout=10_000)


def slot_pickers(page):
    return page.eval_on_selector_all(
        ".settings__slot .settings__picker", "els => els.map(e => e.value)"
    )


def test_the_gear_sits_between_import_and_the_collapse_control(page):
    """Where it was asked for, and the order is the meaning: it acts on the
    whole knowledge base, so it belongs past the four that write files and
    beside the panel's own control."""
    order = page.eval_on_selector_all(
        ".sidebar__actions button",
        "els => els.map(e => e.id || e.dataset.action)",
    )
    assert order[-3:] == ["import", "sidebar-settings", "sidebar-collapse"]


def test_the_window_opens_and_can_be_moved(page):
    open_settings(page)
    window = page.locator(".settings")
    before = window.bounding_box()

    bar = page.locator(".settings__bar")
    box = bar.bounding_box()
    page.mouse.move(box["x"] + 40, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] - 160, box["y"] + 120, steps=8)
    page.mouse.up()

    after = window.bounding_box()
    assert after["x"] < before["x"] - 100 and after["y"] > before["y"] + 80

    # A window is not a modal: the prose underneath stays live.
    assert page.locator(".doc").is_visible()


def test_the_window_is_kept_on_screen(page):
    """Dragged past the edge it would be unrecoverable without knowing where
    it went."""
    open_settings(page)
    bar = page.locator(".settings__bar")
    box = bar.bounding_box()
    page.mouse.move(box["x"] + 40, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(-500, -500, steps=6)
    page.mouse.up()

    after = page.locator(".settings").bounding_box()
    assert after["x"] >= 0 and after["y"] >= 0


def test_it_offers_six_slots_and_the_two_backgrounds(page):
    open_settings(page)
    assert len(slot_pickers(page)) == 6
    labels = page.eval_on_selector_all(
        ".settings__field .settings__label", "els => els.map(e => e.textContent)"
    )
    assert "Left panel" in labels and "Document background" in labels


def test_preview_changes_the_page_and_apply_is_what_writes_it(page, served):
    """The two halves of the promise: you can see it before you mean it, and
    nothing reaches the disk until you do."""
    _, workspace = served
    open_settings(page)

    page.eval_on_selector(
        ".settings__picker[data-field='sidebar']",
        """el => { el.value = '#123456';
                   el.dispatchEvent(new Event('input', {bubbles: true})); }""",
    )
    # Nothing yet: the box is unticked.
    assert rgb(page, ".sidebar", "backgroundColor") == "rgb(209, 199, 183)"

    page.check(".settings__preview input")
    assert rgb(page, ".sidebar", "backgroundColor") == "rgb(18, 52, 86)"
    assert not (workspace.inputs / ".mdweave-theme.json").exists(), "preview wrote to disk"

    # Apply reloads, because every page was just rewritten -- including this
    # one -- so the assertions after it are about the real stylesheet rather
    # than the preview's override.
    with page.expect_navigation(wait_until="networkidle", timeout=20_000):
        page.click(".settings__button--go")

    assert (workspace.inputs / ".mdweave-theme.json").exists()
    assert page.evaluate("() => !document.getElementById('mdweave-preview')")
    assert rgb(page, ".sidebar", "backgroundColor") == "rgb(18, 52, 86)"


def test_closing_takes_the_preview_back(page):
    open_settings(page)
    page.eval_on_selector(
        ".settings__picker[data-field='sidebar']",
        """el => { el.value = '#123456';
                   el.dispatchEvent(new Event('input', {bubbles: true})); }""",
    )
    page.check(".settings__preview input")
    assert rgb(page, ".sidebar", "backgroundColor") == "rgb(18, 52, 86)"

    page.click(".settings__close")
    assert rgb(page, ".sidebar", "backgroundColor") == "rgb(209, 199, 183)"


def test_a_slot_can_be_dragged_to_another_position(page):
    open_settings(page)
    before = slot_pickers(page)

    page.drag_and_drop(
        ".settings__slot[data-index='4']", ".settings__slot[data-index='0']"
    )
    after = slot_pickers(page)

    assert after[0] == before[4], "the fifth colour did not land first"
    assert after[1:5] == before[0:4]
    assert sorted(after) == sorted(before), "a colour was lost in the reorder"


def test_a_new_scheme_starts_from_the_one_on_screen(page):
    open_settings(page)
    before = slot_pickers(page)

    page.click(".settings__row .settings__button")
    assert slot_pickers(page) == before
    name = page.input_value(".settings__text")
    assert name and name != "Warm paper", "a copy needs a name of its own"


# --- reordering must never be visible ---------------------------------------
#
# Reported: rearranging the swatches repainted existing text. The cause was a
# guard that only renumbered when the scheme being edited was the one already
# in use -- but Apply *activates* whatever is being edited, so pressing New and
# rearranging the copy skipped the renumbering and the drag became visible.
#
# These run the whole path, because the arithmetic was never the broken part:
# the server-side test for renumbering passed throughout.

import json as _json  # noqa: E402

from mdweave import scheme as _schemes  # noqa: E402
from mdweave.model import Annotation as _Annotation, TextTarget as _TextTarget  # noqa: E402
from mdweave.sources import sidecar as _sidecar  # noqa: E402

WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]

INK = _schemes.Scheme(
    name="Ink", sidebar="#1b1b22", paper="#101014",
    colors=["#7a3b3b", "#4b3b7a", "#3b4b7a", "#3b7a4b", "#7a7a3b", "#7a5b3b"],
)


@pytest.fixture()
def highlighted(tmp_path):
    """One document wearing all six colours, so a repaint cannot hide."""
    inputs, outputs = tmp_path / "markdown_inputs", tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "doc.md").write_text(f"# Doc\n\n{' '.join(WORDS)}.\n", encoding="utf-8")
    _sidecar.save(
        _sidecar.sidecar_path(inputs / "doc.md"),
        [
            _Annotation(id=f"a{n}", target=_TextTarget(quote=word),
                        kind="highlight", color=n)
            for n, word in enumerate(WORDS, start=1)
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


def loaded(browser, base):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.goto(f"{base}/doc.html", wait_until="networkidle")
    page.wait_for_selector(".sidebar--manageable", timeout=10_000)
    return context, page


def fills(page) -> dict:
    return dict(
        page.eval_on_selector_all(
            "mark.hl",
            "els => els.map(e => [e.textContent, getComputedStyle(e).backgroundColor])",
        )
    )


def drag_slot(page, source: int, target: int):
    page.drag_and_drop(
        f".settings__slot[data-index='{source}']",
        f".settings__slot[data-index='{target}']",
    )


def apply_(page):
    with page.expect_navigation(wait_until="networkidle", timeout=20_000):
        page.click(".settings__button--go")


@pytest.mark.parametrize("make_new", [False, True], ids=["this scheme", "a new scheme"])
def test_reordering_never_changes_what_the_page_looks_like(browser, highlighted, make_new):
    """The whole promise of dragging a swatch: the palette is rearranged and
    the notes are not restyled."""
    base, workspace = highlighted
    context, page = loaded(browser, base)
    try:
        before = fills(page)
        assert len(set(before.values())) == 6, "the fixture should wear all six"

        page.click("#sidebar-settings")
        page.wait_for_selector(".settings", timeout=10_000)
        if make_new:
            page.click(".settings__row .settings__button")
        drag_slot(page, 4, 0)
        apply_(page)

        assert fills(page) == before

        # Invisible, but not a no-op: the numbers underneath moved.
        stored = _json.loads(
            _sidecar.sidecar_path(workspace.inputs / "doc.md").read_text(encoding="utf-8")
        )
        assert [a["color"] for a in stored["annotations"]] == [2, 3, 4, 5, 1, 6]
    finally:
        context.close()


def test_a_reorder_adds_nothing_on_top_of_a_switch(browser, highlighted):
    """Switching palettes does change the colours -- that is what switching
    means. The rule is that a drag contributes nothing beyond it, so both
    routes have to land on the same page."""
    base, workspace = highlighted
    _schemes.save(
        workspace.inputs,
        _schemes.Theme(schemes=[_schemes.DEFAULT_SCHEME, INK], active="Warm paper"),
    )
    workspace.rebuild_all()

    def switch(reorder: bool) -> dict:
        context, page = loaded(browser, base)
        try:
            page.click("#sidebar-settings")
            page.wait_for_selector(".settings", timeout=10_000)
            page.select_option(".settings__select", "Ink")
            if reorder:
                drag_slot(page, 4, 0)
            apply_(page)
            return fills(page)
        finally:
            context.close()

    plain = switch(False)
    # Put the knowledge base back the way it started before the second run.
    _schemes.save(
        workspace.inputs,
        _schemes.Theme(schemes=[_schemes.DEFAULT_SCHEME, INK], active="Warm paper"),
    )
    _sidecar.save(
        _sidecar.sidecar_path(workspace.inputs / "doc.md"),
        [
            _Annotation(id=f"a{n}", target=_TextTarget(quote=word),
                        kind="highlight", color=n)
            for n, word in enumerate(WORDS, start=1)
        ],
    )
    workspace.rebuild_all()

    assert switch(True) == plain
