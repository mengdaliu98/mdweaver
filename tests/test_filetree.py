"""Tests for managing the files from the sidebar.

Creating, renaming, deleting, moving a document into another folder, and
remembering the order the rows were dragged into. All four are the same kind of
write -- the markdown root is rearranged and every page is rebuilt from it --
so they share one fixture and one set of traversal tests.
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

from mdweave.serve import Workspace, make_server
from mdweave.tree import ORDER_FILE, build_tree, document_ids, load_order, save_order

ROOT = Path(__file__).resolve().parents[1] / "mdweave"
ASSETS = ROOT / "assets"
TEMPLATES = ROOT / "templates"
THEME = ROOT / "theme"


# --- the arrangement dotfile ----------------------------------------------

def test_a_listed_folder_comes_out_in_the_order_given():
    tree = build_tree(["alpha", "bravo", "charlie"], {"": ["charlie", "alpha", "bravo"]})
    assert [n.name for n in tree] == ["charlie", "alpha", "bravo"]


def test_unlisted_names_keep_the_old_sort_after_the_listed_ones():
    """Someone else's new document must not land in the middle of your order."""
    tree = build_tree(["alpha", "bravo", "charlie", "delta"], {"": ["delta", "charlie"]})
    assert [n.name for n in tree] == ["delta", "charlie", "alpha", "bravo"]


def test_an_unlisted_folder_still_sorts_before_unlisted_documents():
    tree = build_tree(["aaa_file", "zzz_folder/doc"], {"": []})
    assert [n.is_dir for n in tree] == [True, False]


def test_an_order_can_put_a_document_above_a_folder():
    """The whole point of dragging: the folders-first rule is a default, not a law."""
    tree = build_tree(["aaa_file", "zzz_folder/doc"], {"": ["aaa_file"]})
    assert [n.name for n in tree] == ["aaa_file", "zzz_folder"]


def test_every_depth_is_ordered_not_just_the_root():
    tree = build_tree(
        ["notes/alpha", "notes/bravo", "notes/charlie"],
        {"notes": ["charlie", "bravo"]},
    )
    assert [n.name for n in tree[0].children] == ["charlie", "bravo", "alpha"]


def test_the_order_file_is_never_a_document(tmp_path):
    """A dotfile at the markdown root has to stay out of the navigation."""
    (tmp_path / "real.md").write_text("x")
    save_order(tmp_path, {"": ["real"]})

    assert (tmp_path / ORDER_FILE).exists()
    assert list(document_ids(tmp_path)) == ["real"]


def test_an_order_survives_a_round_trip(tmp_path):
    save_order(tmp_path, {"": ["b", "a"], "notes": ["z"]})
    assert load_order(tmp_path) == {"": ["b", "a"], "notes": ["z"]}


def test_a_missing_or_broken_order_file_falls_back_rather_than_failing(tmp_path):
    """A hand-mangled dotfile must not take the whole sidebar down with it."""
    assert load_order(tmp_path) == {}

    (tmp_path / ORDER_FILE).write_text("{ not json", encoding="utf-8")
    assert load_order(tmp_path) == {}

    (tmp_path / ORDER_FILE).write_text('{"": "alpha", "ok": ["a", 7]}', encoding="utf-8")
    assert load_order(tmp_path) == {"ok": ["a"]}


def test_an_empty_order_takes_the_file_away_again(tmp_path):
    save_order(tmp_path, {"": ["a"]})
    save_order(tmp_path, {"": []})
    assert not (tmp_path / ORDER_FILE).exists()


# --- the endpoints ---------------------------------------------------------

@pytest.fixture()
def server(tmp_path):
    inputs = tmp_path / "markdown_inputs"
    outputs = tmp_path / "html_outputs"
    (inputs / "notes").mkdir(parents=True)
    (inputs / "archive").mkdir()
    (inputs / "top.md").write_text("# Top\n\nAt the root.\n", encoding="utf-8")
    (inputs / "other.md").write_text("# Other\n\nAlso at the root.\n", encoding="utf-8")
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


# --- creating --------------------------------------------------------------

def test_creating_writes_a_document_at_the_top_level(server):
    base, workspace = server
    status, payload = call(base, "/api/documents/create", {"path": "fresh"})

    assert status == 201
    assert payload["document"] == {
        "id": "fresh", "href": "fresh.html", "label": "Fresh"
    }
    assert (workspace.inputs / "fresh.md").exists()


def test_creating_inside_a_folder_lands_inside_it(server):
    base, workspace = server
    status, payload = call(base, "/api/documents/create", {"path": "notes/scratch"})

    assert status == 201
    assert payload["document"]["id"] == "notes/scratch"
    assert payload["document"]["label"] == "Scratch", "the label is the leaf, not the path"
    assert (workspace.inputs / "notes" / "scratch.md").exists()


def test_a_new_document_has_something_to_click(server):
    """A file with no blocks in it renders a page nothing can start an edit on."""
    base, workspace = server
    call(base, "/api/documents/create", {"path": "My new note"})

    assert (workspace.inputs / "My_new_note.md").read_text() == "# My new note\n"
    assert "<h1" in (workspace.outputs / "My_new_note.html").read_text()


def test_a_created_document_is_in_every_other_sidebar_at_once(server):
    base, workspace = server
    call(base, "/api/documents/create", {"path": "fresh"})
    assert 'href="fresh.html"' in (workspace.outputs / "top.html").read_text()


def test_creating_over_an_existing_document_is_refused(server):
    base, workspace = server
    status, payload = call(base, "/api/documents/create", {"path": "top"})

    assert status == 409
    assert "already exists" in payload["error"]
    assert "At the root." in (workspace.inputs / "top.md").read_text()


def test_creating_in_a_folder_that_is_not_there_is_a_404(server):
    base, _ = server
    assert call(base, "/api/documents/create", {"path": "nowhere/doc"})[0] == 404


def test_creating_needs_a_path(server):
    base, _ = server
    assert call(base, "/api/documents/create", {})[0] == 400


# --- moving and renaming ---------------------------------------------------

def test_moving_a_document_into_a_folder_takes_its_files_with_it(server):
    base, workspace = server
    status, payload = call(
        base, "/api/documents/move", {"from": "top", "to": "notes/top"}
    )

    assert status == 200
    assert payload["document"]["id"] == "notes/top"
    assert not (workspace.inputs / "top.md").exists()
    assert (workspace.inputs / "notes" / "top.md").read_text() == "# Top\n\nAt the root.\n"


def test_the_sidecar_travels_with_the_prose(server):
    """Its name is derived from the markdown's, so it would silently orphan."""
    base, workspace = server
    call(base, "/api/annotations", {
        "document": "top", "quote": "At the root", "body": "A comment.",
    })
    assert (workspace.inputs / "top.ann.json").exists()

    call(base, "/api/documents/move", {"from": "top", "to": "notes/top"})

    assert not (workspace.inputs / "top.ann.json").exists()
    moved = json.loads((workspace.inputs / "notes" / "top.ann.json").read_text())
    assert moved["annotations"][0]["thread"][0]["body"] == "A comment."


def test_a_move_leaves_no_page_behind_at_the_old_address(server):
    base, workspace = server
    assert (workspace.outputs / "top.html").exists()

    call(base, "/api/documents/move", {"from": "top", "to": "notes/top"})

    assert not (workspace.outputs / "top.html").exists()
    assert (workspace.outputs / "notes" / "top.html").exists()


def test_a_moved_document_is_gone_from_the_old_url(server):
    base, _ = server
    call(base, "/api/documents/move", {"from": "top", "to": "notes/top"})

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(base + "/top.html", timeout=10)
    assert caught.value.code == 404


def test_moving_out_of_a_folder_reaches_the_top_level(server):
    base, workspace = server
    status, payload = call(
        base, "/api/documents/move", {"from": "notes/weekly", "to": "weekly"}
    )

    assert status == 200
    assert payload["document"]["id"] == "weekly"
    assert (workspace.inputs / "weekly.md").exists()


def test_renaming_is_the_same_operation_as_moving(server):
    base, workspace = server
    status, payload = call(
        base, "/api/documents/move", {"from": "notes/weekly", "to": "notes/summary"}
    )

    assert status == 200
    assert payload["document"] == {
        "id": "notes/summary",
        "href": "notes/summary.html",
        "label": "Summary",
    }
    assert (workspace.inputs / "notes" / "summary.md").exists()


def test_a_renamed_document_gets_a_safe_filename(server):
    base, workspace = server
    _, payload = call(
        base, "/api/documents/move", {"from": "top", "to": 'My New "Name"'}
    )
    assert payload["document"]["id"] == "My_New_Name"
    assert (workspace.inputs / "My_New_Name.md").exists()


def test_a_rename_typed_with_the_suffix_does_not_double_it(server):
    base, workspace = server
    _, payload = call(base, "/api/documents/move", {"from": "top", "to": "renamed.md"})
    assert payload["document"]["id"] == "renamed"
    assert (workspace.inputs / "renamed.md").exists()


def test_moving_onto_an_existing_document_is_refused(server):
    base, workspace = server
    status, payload = call(base, "/api/documents/move", {"from": "top", "to": "other"})

    assert status == 409
    assert "already exists" in payload["error"]
    assert (workspace.inputs / "top.md").exists(), "nothing should have been touched"
    assert "Also at the root." in (workspace.inputs / "other.md").read_text()


def test_moving_a_document_nowhere_is_harmless(server):
    base, workspace = server
    status, payload = call(base, "/api/documents/move", {"from": "top", "to": "top"})

    assert status == 200
    assert payload["document"]["id"] == "top"
    assert (workspace.inputs / "top.md").exists()


def test_moving_a_document_that_is_not_there_is_a_404(server):
    base, _ = server
    assert call(base, "/api/documents/move", {"from": "ghost", "to": "notes/ghost"})[0] == 404


def test_moving_into_a_folder_that_is_not_there_is_a_404(server):
    base, _ = server
    assert call(base, "/api/documents/move", {"from": "top", "to": "nowhere/top"})[0] == 404


def test_every_sidebar_follows_a_move(server):
    base, workspace = server
    call(base, "/api/documents/move", {"from": "top", "to": "notes/top"})

    other = (workspace.outputs / "other.html").read_text()
    assert 'href="notes/top.html"' in other
    assert 'href="top.html"' not in other


# --- deleting --------------------------------------------------------------

def test_deleting_takes_the_prose_the_comments_and_the_page(server):
    base, workspace = server
    call(base, "/api/annotations", {
        "document": "top", "quote": "At the root", "body": "A comment.",
    })

    status, payload = call(base, "/api/documents/delete", {"document": "top"})

    assert status == 200
    assert payload == {"deleted": "top"}
    assert not (workspace.inputs / "top.md").exists()
    assert not (workspace.inputs / "top.ann.json").exists()
    assert not (workspace.outputs / "top.html").exists()


def test_deleting_leaves_the_other_documents_alone(server):
    base, workspace = server
    call(base, "/api/documents/delete", {"document": "top"})

    assert (workspace.inputs / "other.md").exists()
    assert 'href="top.html"' not in (workspace.outputs / "other.html").read_text()


def test_deleting_a_document_that_is_not_there_is_a_404(server):
    base, _ = server
    assert call(base, "/api/documents/delete", {"document": "ghost"})[0] == 404


def test_deleting_needs_a_document(server):
    base, _ = server
    assert call(base, "/api/documents/delete", {})[0] == 400


# --- the order -------------------------------------------------------------

def test_an_order_is_written_to_the_dotfile_at_the_markdown_root(server):
    base, workspace = server
    status, payload = call(
        base, "/api/tree/order", {"folder": "", "order": ["other", "notes", "top"]}
    )

    assert status == 200
    assert payload == {"folder": "", "order": ["other", "notes", "top"]}
    assert load_order(workspace.inputs) == {"": ["other", "notes", "top"]}


def test_an_order_shows_up_in_every_sidebar(server):
    base, workspace = server
    call(base, "/api/tree/order", {"folder": "", "order": ["other", "notes", "top"]})

    nav = (workspace.outputs / "top.html").read_text().split("<nav")[1].split("</nav>")[0]
    assert nav.index(">Other<") < nav.index(">Notes<") < nav.index(">Top<")


def test_names_that_are_not_children_are_dropped(server):
    """The file is read by people too; letting it collect ghosts is unkind."""
    base, workspace = server
    _, payload = call(
        base, "/api/tree/order", {"folder": "", "order": ["top", "ghost", "other"]}
    )
    assert payload["order"] == ["top", "other"]
    assert load_order(workspace.inputs) == {"": ["top", "other"]}


def test_a_folder_counts_as_a_child_of_its_parent(server):
    base, _ = server
    _, payload = call(base, "/api/tree/order", {"folder": "", "order": ["notes"]})
    assert payload["order"] == ["notes"]


def test_ordering_a_folder_that_is_not_there_is_a_404(server):
    base, _ = server
    assert call(base, "/api/tree/order", {"folder": "nowhere", "order": []})[0] == 404


@pytest.mark.parametrize(
    "payload", [{"folder": ""}, {"folder": "", "order": "top"}, {"folder": "", "order": [1]}]
)
def test_a_bad_order_payload_is_refused(server, payload):
    base, _ = server
    assert call(base, "/api/tree/order", payload)[0] == 400


def test_a_rename_keeps_its_place_in_the_order(server):
    """Otherwise a better name costs a row its position in the folder."""
    base, workspace = server
    call(base, "/api/tree/order", {"folder": "", "order": ["other", "top"]})
    call(base, "/api/documents/move", {"from": "other", "to": "renamed"})

    assert load_order(workspace.inputs)[""] == ["renamed", "top"]


def test_a_move_out_of_a_folder_lets_go_of_its_rank(server):
    base, workspace = server
    call(base, "/api/tree/order", {"folder": "", "order": ["other", "top"]})
    call(base, "/api/documents/move", {"from": "other", "to": "notes/other"})

    assert load_order(workspace.inputs)[""] == ["top"]


def test_a_deleted_document_leaves_the_order(server):
    base, workspace = server
    call(base, "/api/tree/order", {"folder": "", "order": ["other", "top"]})
    call(base, "/api/documents/delete", {"document": "other"})

    assert load_order(workspace.inputs)[""] == ["top"]


# --- importing into a folder -----------------------------------------------

def test_an_import_can_name_the_folder_it_lands_in(server):
    base, workspace = server
    status, payload = call(base, "/api/documents", {
        "name": "dropped.md", "content": "# Dropped\n", "folder": "notes",
    })

    assert status == 201
    assert payload["document"]["id"] == "notes/dropped"
    assert (workspace.inputs / "notes" / "dropped.md").exists()


def test_an_import_with_no_folder_still_lands_at_the_root(server):
    base, workspace = server
    _, payload = call(base, "/api/documents", {"name": "dropped.md", "content": "x"})
    assert payload["document"]["id"] == "dropped"
    assert (workspace.inputs / "dropped.md").exists()


def test_importing_into_a_folder_that_is_not_there_is_a_404(server):
    base, _ = server
    status, _ = call(base, "/api/documents", {
        "name": "dropped.md", "content": "x", "folder": "nowhere",
    })
    assert status == 404


# --- the guard -------------------------------------------------------------

ESCAPES = [
    "../escape",
    "../../../../tmp/escape",
    "/etc/escape",
    "..\\..\\escape",
    "notes/../../escape",
    "./../escape",
]


@pytest.mark.parametrize("target", ESCAPES)
def test_no_create_can_write_outside_the_markdown_root(server, target):
    """Every path a client sends is either looked up or reduced to a bare name."""
    base, workspace = server
    before = _snapshot(workspace)

    status, _ = call(base, "/api/documents/create", {"path": target})

    assert status in (400, 404, 409)
    assert _snapshot(workspace) == before
    assert not (workspace.inputs.parent / "escape.md").exists()


@pytest.mark.parametrize("target", ESCAPES)
def test_no_move_can_write_outside_the_markdown_root(server, target):
    base, workspace = server
    before = _snapshot(workspace)

    status, _ = call(base, "/api/documents/move", {"from": "top", "to": target})

    assert status in (400, 404, 409)
    assert _snapshot(workspace) == before
    assert not (workspace.inputs.parent / "escape.md").exists()


@pytest.mark.parametrize("folder", ["..", "../..", "/etc", "notes/..", "..\\.."])
def test_no_order_can_be_written_outside_the_markdown_root(server, folder):
    base, workspace = server

    assert call(base, "/api/tree/order", {"folder": folder, "order": ["x"]})[0] == 404
    assert not (workspace.inputs.parent / ORDER_FILE).exists()


@pytest.mark.parametrize("folder", ESCAPES + ["..", "/etc"])
def test_no_import_can_name_a_folder_outside_the_markdown_root(server, folder):
    base, workspace = server
    before = _snapshot(workspace)

    status, _ = call(base, "/api/documents", {
        "name": "escape.md", "content": "x", "folder": folder,
    })

    assert status == 404
    assert _snapshot(workspace) == before


def test_a_document_id_is_looked_up_never_joined(server):
    """`from` and `document` name a document that is there, or nothing at all."""
    base, workspace = server
    outside = workspace.inputs.parent / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")

    for path, payload in [
        ("/api/documents/move", {"from": "../outside", "to": "stolen"}),
        ("/api/documents/delete", {"document": "../outside"}),
    ]:
        assert call(base, path, payload)[0] == 404
    assert outside.exists()


def test_the_root_itself_is_the_only_empty_folder_path(server):
    """`folder_for` normalises the slashes a client might send around "the root"."""
    base, workspace = server
    for folder in ["", "/", "//"]:
        assert call(base, "/api/tree/order", {"folder": folder, "order": ["top"]})[0] == 200


def _snapshot(workspace: Workspace) -> set[str]:
    """Every file under both roots, so a stray write anywhere shows up."""
    return {
        str(path.relative_to(workspace.inputs.parent))
        for path in workspace.inputs.parent.rglob("*")
        if path.is_file()
    }


# --- the browser side ------------------------------------------------------

def test_the_new_module_is_registered_in_both_places():
    """One list puts the tag on the page; the other copies the file next to it."""
    render = (ROOT / "render.py").read_text(encoding="utf-8")
    assert '"assets/filetree.js"' in render
    assert '"filetree.js"' in render
    assert (ASSETS / "filetree.js").exists()


def test_the_one_import_button_in_the_header_is_gone():
    """It could only ever mean the root; the row it hangs off says where now."""
    template = (TEMPLATES / "sidebar.html.j2").read_text(encoding="utf-8")
    assert 'id="sidebar-import"' not in template
    assert 'id="sidebar-file"' in template, "the picker is shared by every row"


def _actions_on(html: str, marker: str, until: str = "</li>") -> set[str]:
    """The data-action values in the row `marker` identifies.

    `until` matters for a folder: its <li> wraps the whole subtree, so
    stopping at </li> would sweep in every child row's buttons too.
    """
    import re

    row = html.split(marker)[1].split(until)[0]
    return set(re.findall(r'data-action="([a-z-]+)"', row))


def test_a_document_row_offers_only_rename_and_delete():
    """A document has nothing inside it, so create/new-folder/import there
    never meant anything -- they belonged to the folder it sits in."""
    from mdweave.render import render_sidebar
    from mdweave.tree import build_tree

    html = render_sidebar(build_tree(["alpha", "notes/inner"], None, ["notes"]), "alpha")
    assert _actions_on(html, 'data-doc="alpha"') == {"rename", "delete"}


def test_a_folder_row_offers_the_full_set():
    from mdweave.render import render_sidebar
    from mdweave.tree import build_tree

    html = render_sidebar(build_tree(["notes/inner"], None, ["notes"]), "notes/inner")
    assert _actions_on(html, 'data-folder="notes"', "</summary>") == {
        "create", "new-folder", "import", "rename-folder", "delete-folder"
    }


def test_the_top_level_controls_sit_beside_the_title():
    """Not on a strip below the tree: the thing they act on is what you are
    looking at, and the header is where "the top level" reads as a place."""
    from mdweave.render import render_sidebar
    from mdweave.tree import build_tree

    html = render_sidebar(build_tree(["alpha"], None, []), "alpha")
    head = html.split('class="sidebar__head"')[1].split("</div>")[0]
    assert _actions_on(html, 'class="sidebar__head"', "</span>\n    </div>") >= {
        "create", "new-folder", "import"
    }
    # And nothing destructive up there -- there is no root to rename or delete.
    assert "rename-folder" not in head and "delete-folder" not in head


def test_rename_and_delete_are_offered_on_rows_but_not_on_the_root():
    """Folders get them too now; the top level has no name and no self to drop."""
    template = (TEMPLATES / "sidebar.html.j2").read_text(encoding="utf-8")
    guarded = template.split('{% if what != "root" %}')[1].split("{% endif %}")[0]

    # One pair of buttons, switching action by what the row is.
    assert '"rename" if what == "file" else "rename-folder"' in guarded
    assert '"delete" if what == "file" else "delete-folder"' in guarded


def test_the_root_can_make_the_first_folder():
    """Without this the nested tree is unreachable: every other way of getting
    a folder needs a folder to already exist."""
    template = (TEMPLATES / "sidebar.html.j2").read_text(encoding="utf-8")
    source = (ASSETS / "filetree.js").read_text(encoding="utf-8")

    assert '{{ row_actions("root", ' in template, "the root strip carries controls"
    assert 'data-action="new-folder"' in template
    assert '"new-folder": newFolder' in source
    assert '"/api/folders/create"' in source


def test_the_controls_are_reachable_without_a_mouse():
    """`display: none` would take them out of the tab order entirely."""
    template = (TEMPLATES / "sidebar.html.j2").read_text(encoding="utf-8")
    css = (THEME / "sidebar.css").read_text(encoding="utf-8")

    assert 'tabindex="-1"' not in template
    assert "aria-label=" in template.split('data-action="create"')[1].split(">")[0]

    # The base rule specifically -- other rules reveal the set on hover, and a
    # plain "first .tree__actions {" would read whichever came first in the file.
    block = re.search(r"(?m)^\.tree__actions \{([^}]*)\}", css)
    assert block, "no base .tree__actions rule"
    assert "opacity: 0" in block.group(1)
    assert "display: none" not in block.group(1)
    assert ":focus-within > .tree__actions" in css


def test_the_controls_are_hidden_until_a_server_answers():
    source = (ASSETS / "filetree.js").read_text(encoding="utf-8")
    css = (THEME / "sidebar.css").read_text(encoding="utf-8")

    assert "ui.api()" in source
    assert 'sidebar.classList.add("sidebar--manageable")' in source
    assert ".sidebar--manageable" in css


def test_deleting_asks_first():
    """Destructive and not undoable -- and it takes the comments with it."""
    source = (ASSETS / "filetree.js").read_text(encoding="utf-8")
    body = source.split("function remove(")[1].split("\n  }")[0]
    assert "window.confirm(" in body
    assert body.index("window.confirm(") < body.index('post("/api/documents/delete"')
    assert "cannot be undone" in body


def test_a_rename_goes_through_move():
    source = (ASSETS / "filetree.js").read_text(encoding="utf-8")
    assert '"/api/documents/move"' in source.split("function rename(")[1]


def test_a_drag_within_the_tree_is_told_apart_from_a_file_off_the_desktop():
    """dataTransfer.getData is off-limits until the drop, so the type is all there is."""
    filetree = (ASSETS / "filetree.js").read_text(encoding="utf-8")
    sidebar = (ASSETS / "sidebar.js").read_text(encoding="utf-8")

    assert 'ROW_TYPE = "application/x-mdweave-row"' in filetree
    assert "isRowDrag(event)" in filetree
    assert "setData(ROW_TYPE" in filetree
    # And the other way: a row being dragged must not put the panel into the
    # "drop a file here" state.
    assert "function hasFiles(" in sidebar
    assert 'indexOf.call(types, "Files")' in sidebar


def test_a_drop_onto_a_folder_imports_into_that_folder():
    """The <details> spans its subtree, so the innermost one is the answer."""
    sidebar = (ASSETS / "sidebar.js").read_text(encoding="utf-8")
    assert 'closest(".tree__folder")' in sidebar
    assert "folderAt(event.target)" in sidebar
    assert "folder: folder || \"\"" in sidebar


def test_importing_is_not_implemented_twice():
    filetree = (ASSETS / "filetree.js").read_text(encoding="utf-8")
    sidebar = (ASSETS / "sidebar.js").read_text(encoding="utf-8")

    assert "window.mdweaveSidebar" in sidebar and "importInto:" in sidebar
    assert "window.mdweaveSidebar" in filetree
    assert "/api/documents\"" not in filetree, "uploading belongs to sidebar.js"


def test_the_drop_indicator_says_between_or_inside():
    filetree = (ASSETS / "filetree.js").read_text(encoding="utf-8")
    css = (THEME / "sidebar.css").read_text(encoding="utf-8")

    for mark in ("tree__row--into", "tree__row--before", "tree__row--after"):
        assert mark in filetree and "." + mark in css


def test_a_mutation_takes_a_fresh_page_rather_than_editing_the_sidebar():
    """Every page bakes its own sidebar; one patched by hand would soon lie."""
    source = (ASSETS / "filetree.js").read_text(encoding="utf-8")
    assert "window.location.reload()" in source
    assert "window.location.href" in source


def test_every_browser_asset_is_valid_javascript():
    """There is no build step, so a syntax error only shows up in the browser."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    for path in sorted(ASSETS.glob("*.js")):
        done = subprocess.run([node, "--check", str(path)], capture_output=True)
        assert done.returncode == 0, f"{path.name}: {done.stderr.decode()}"


# --- making folders --------------------------------------------------------
#
# The gap that made everything above unreachable: every other folder operation
# needs a folder to already exist, and a fresh knowledge base has none.

def test_a_folder_can_be_created(server):
    base, workspace = server
    status, payload = call(base, "/api/folders/create", {"path": "research"})

    assert status == 201
    assert payload["folder"]["path"] == "research"
    assert (workspace.inputs / "research").is_dir()


def test_a_folder_can_be_created_inside_another(server):
    base, workspace = server
    assert call(base, "/api/folders/create", {"path": "notes/2026"})[0] == 201
    assert (workspace.inputs / "notes" / "2026").is_dir()


def test_a_new_folder_becomes_a_drop_target(server):
    """Creating one is only useful if a document can then be moved into it."""
    base, workspace = server
    call(base, "/api/folders/create", {"path": "research"})

    assert call(base, "/api/documents/move", {"from": "top", "to": "research/top"})[0] == 200
    assert (workspace.inputs / "research" / "top.md").exists()
    assert not (workspace.inputs / "top.md").exists()


def test_a_new_folder_shows_up_in_every_sidebar(server):
    base, workspace = server
    call(base, "/api/folders/create", {"path": "research"})
    assert "Research" in (workspace.outputs / "other.html").read_text(encoding="utf-8")


def test_creating_a_folder_that_exists_is_a_conflict(server):
    base, _ = server
    assert call(base, "/api/folders/create", {"path": "notes"})[0] == 409


def test_a_folder_name_is_sanitised(server):
    base, workspace = server
    _, payload = call(base, "/api/folders/create", {"path": "My Research Notes"})
    assert payload["folder"]["path"] == "My_Research_Notes"
    assert (workspace.inputs / "My_Research_Notes").is_dir()


@pytest.mark.parametrize(
    "path", ["../escape", "/etc/escape", "..\\escape", "notes/../../escape", "."]
)
def test_a_folder_cannot_be_created_outside_the_root(server, path):
    base, workspace = server
    before = sorted(p.name for p in workspace.inputs.parent.iterdir())

    call(base, "/api/folders/create", {"path": path})

    assert sorted(p.name for p in workspace.inputs.parent.iterdir()) == before
    assert not (workspace.inputs.parent / "escape").exists()


def test_a_leading_dot_is_stripped_rather_than_honoured(server):
    """A dotted directory is skipped by document_ids, so honouring the name
    would create a folder that could never appear. The dot goes instead."""
    base, workspace = server
    _, payload = call(base, "/api/folders/create", {"path": ".hidden"})

    assert payload["folder"]["path"] == "hidden"
    assert not (workspace.inputs / ".hidden").exists()
    assert (workspace.inputs / "hidden").is_dir()


# --- renaming and deleting folders -----------------------------------------

def test_renaming_a_folder_carries_its_documents(server):
    base, workspace = server
    status, payload = call(base, "/api/folders/rename", {"from": "notes", "to": "journal"})

    assert status == 200 and payload["folder"]["path"] == "journal"
    assert (workspace.inputs / "journal" / "weekly.md").exists()
    assert not (workspace.inputs / "notes").exists()
    assert "journal/weekly" in workspace.documents()
    # The page under the old id would otherwise be served forever.
    assert not (workspace.outputs / "notes" / "weekly.html").exists()


def test_renaming_a_folder_moves_its_arrangement(server):
    base, workspace = server
    call(base, "/api/tree/order", {"folder": "notes", "order": ["weekly"]})
    call(base, "/api/folders/rename", {"from": "notes", "to": "journal"})

    from mdweave.tree import load_order

    order = load_order(workspace.inputs)
    assert "notes" not in order
    assert order.get("journal") == ["weekly"]


def test_a_folder_cannot_be_renamed_into_itself(server):
    base, _ = server
    assert call(base, "/api/folders/rename", {"from": "notes", "to": "notes/inner"})[0] == 400


def test_the_root_cannot_be_renamed_or_deleted(server):
    base, _ = server
    assert call(base, "/api/folders/rename", {"from": "", "to": "x"})[0] == 400
    assert call(base, "/api/folders/delete", {"folder": ""})[0] == 400


def test_an_empty_folder_deletes_without_ceremony(server):
    base, workspace = server
    status, payload = call(base, "/api/folders/delete", {"folder": "archive"})

    assert status == 200 and payload["removed"] == 0
    assert not (workspace.inputs / "archive").exists()


def test_a_folder_with_documents_refuses_a_plain_delete(server):
    """A mis-aimed request must not take a subtree with it."""
    base, workspace = server
    status, payload = call(base, "/api/folders/delete", {"folder": "notes"})

    assert status == 409
    assert "recursive" in payload["error"]
    assert (workspace.inputs / "notes" / "weekly.md").exists()


def test_a_recursive_delete_says_what_it_took(server):
    base, workspace = server
    status, payload = call(
        base, "/api/folders/delete", {"folder": "notes", "recursive": True}
    )

    assert status == 200 and payload["removed"] == 1
    assert not (workspace.inputs / "notes").exists()
    assert not (workspace.outputs / "notes" / "weekly.html").exists()
    assert "top" in workspace.documents(), "the rest of the base is untouched"


def test_an_unknown_folder_is_a_404(server):
    base, _ = server
    assert call(base, "/api/folders/rename", {"from": "ghost", "to": "x"})[0] == 404
    assert call(base, "/api/folders/delete", {"folder": "ghost"})[0] == 404


def test_an_empty_folder_is_still_drawn(server):
    """Otherwise a new folder is invisible, and the only way to put a document
    in one is to drag onto its row -- so it could never stop being empty."""
    base, workspace = server
    call(base, "/api/folders/create", {"path": "research"})

    tree = workspace.tree()
    assert "research" in [n.name for n in tree if n.is_dir]
    assert "Research" in (workspace.outputs / "top.html").read_text(encoding="utf-8")


def test_an_empty_folder_survives_a_plain_build(tmp_path):
    """`mdweave build` renders from the same tree the server does."""
    from mdweave.cli import main

    src = tmp_path / "markdown_inputs"
    (src / "empty_one").mkdir(parents=True)
    (src / "doc.md").write_text("# Doc\n", encoding="utf-8")
    out = tmp_path / "html_outputs"

    assert main(["build", str(src), "-o", str(out)]) == 0
    assert "Empty one" in (out / "doc.html").read_text(encoding="utf-8")


# --- keeping it quick ------------------------------------------------------
#
# A tree change alters the navigation on every page and none of their prose.
# Re-rendering all of it took two seconds to move one row.

def test_a_tree_change_does_not_re_render_the_prose(server):
    """The give-away: an untouched page keeps its exact bytes but for the nav."""
    base, workspace = server
    page = workspace.outputs / "other.html"
    before = page.read_text(encoding="utf-8")

    call(base, "/api/tree/order", {"folder": "", "order": ["notes", "top", "other"]})

    after = page.read_text(encoding="utf-8")
    assert after != before, "the navigation should have moved"
    assert after.split("</nav>")[1] == before.split("</nav>")[1], "prose was re-rendered"


def test_a_page_without_the_markers_is_rebuilt_in_full(server):
    """Written by an older version -- splicing has nothing to aim at."""
    base, workspace = server
    page = workspace.outputs / "other.html"
    page.write_text("<html>ancient</html>", encoding="utf-8")

    call(base, "/api/documents/create", {"path": "fresh"})

    rebuilt = page.read_text(encoding="utf-8")
    assert "Also at the root." in rebuilt
    assert 'href="fresh.html"' in rebuilt


def test_a_missing_page_is_written_rather_than_skipped(server):
    base, workspace = server
    (workspace.outputs / "other.html").unlink()

    call(base, "/api/tree/order", {"folder": "", "order": ["top"]})

    assert (workspace.outputs / "other.html").exists()


def test_a_block_edit_leaves_every_other_page_alone(server):
    """A label comes from the filename, so prose cannot change another page."""
    base, workspace = server
    untouched = workspace.outputs / "other.html"
    before = untouched.read_text(encoding="utf-8")

    status, _ = call(base, "/api/block", {
        "document": "top", "start": 0, "end": 1, "text": "# Renamed heading",
    })

    assert status == 200
    assert untouched.read_text(encoding="utf-8") == before
    assert "Renamed heading" in (workspace.outputs / "top.html").read_text(encoding="utf-8")


# --- swapping the panel instead of reloading -------------------------------

def test_a_mutation_hands_back_the_caller_s_panel(server):
    base, _ = server
    _, payload = call(
        base, "/api/documents/create", {"path": "fresh", "page": "top"}
    )

    assert 'href="fresh.html"' in payload["sidebar"]
    assert "<!--mdweave:sidebar-->" in payload["sidebar"]


def test_the_panel_is_rendered_for_the_page_that_asked(server):
    """Each page's sidebar differs: the active row, and how far it has to climb."""
    base, _ = server
    _, root_page = call(base, "/api/tree/order", {"folder": "", "order": [], "page": "top"})
    _, nested = call(base, "/api/tree/order", {"folder": "", "order": [], "page": "notes/weekly"})

    assert 'href="top.html"' in root_page["sidebar"]
    assert 'href="../top.html"' in nested["sidebar"], "a nested page climbs out"


def test_no_panel_comes_back_for_a_page_that_just_went_away(server):
    """The caller has to navigate, not swap -- and needs to be told so."""
    base, _ = server
    _, payload = call(
        base, "/api/documents/delete", {"document": "top", "page": "top"}
    )
    assert payload["sidebar"] is None


def test_a_caller_that_names_no_page_gets_no_panel(server):
    base, _ = server
    _, payload = call(base, "/api/tree/order", {"folder": "", "order": []})
    assert "sidebar" not in payload


def test_the_client_swaps_rather_than_reloading():
    tree = (ASSETS / "filetree.js").read_text(encoding="utf-8")
    ui = (ASSETS / "ui.js").read_text(encoding="utf-8")

    assert "adoptSidebar" in ui and "adoptSidebar: adoptSidebar" in ui
    assert "ui.adoptSidebar(payload && payload.sidebar)" in tree
    assert "payload.page = CURRENT" in tree, "the server cannot guess the page"

    # The one reload left is the fallback inside settle().
    assert tree.count("window.location.reload()") == 1


def test_the_panel_s_own_controls_are_re_wired_after_a_swap():
    """filetree.js delegates to the panel and survives; these do not."""
    sidebar = (ASSETS / "sidebar.js").read_text(encoding="utf-8")
    ui = (ASSETS / "ui.js").read_text(encoding="utf-8")

    assert "rewire: rewire" in sidebar
    assert "wireFolders()" in sidebar and "wireHead()" in sidebar
    assert "api.rewire()" in ui
