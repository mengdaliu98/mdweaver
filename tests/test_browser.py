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
