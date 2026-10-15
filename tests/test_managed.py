"""Tests for managed documents: `mdweave serve <file.md>`.

A list of files from anywhere on the machine instead of a knowledge base
directory. The ids are whole paths so they never move; the sidebar shows only
the part of the filesystem below where the files part ways.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from mdweave import cli, managed
from mdweave.serve import ManagedWorkspace, Workspace, make_server


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or f"# {path.stem}\n\nSome prose.\n", encoding="utf-8")
    return path


def doc(path: Path) -> str:
    return managed.doc_id(path)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    where = tmp_path / "home"
    monkeypatch.setenv(managed.HOME_ENV, str(where))
    return where


@pytest.fixture()
def files(tmp_path):
    """The layout from the request: two files under root/A, one under root/B."""
    root = tmp_path / "root"
    return {
        "one": write(root / "A" / "a" / "file1.md"),
        "two": write(root / "A" / "b" / "file2.md"),
        "three": write(root / "B" / "b" / "file3.md"),
    }


def workspace_for(home: Path) -> ManagedWorkspace:
    return ManagedWorkspace(inputs=Path("/"), outputs=managed.outputs(home), home=home)


def shape(nodes) -> list:
    """A tree as nested names, folders marked with a slash."""
    return [
        (f"{n.name}/", shape(n.children)) if n.is_dir else n.name for n in nodes
    ]


# --- the list ----------------------------------------------------------------

def test_adding_records_the_absolute_path_once(home, files):
    path, added = managed.add(home, files["one"])
    assert added and path == files["one"]
    assert managed.add(home, files["one"]) == (files["one"], False)
    assert managed.load(home) == [files["one"]]


def test_removing_takes_it_off_the_list_and_leaves_the_file(home, files):
    managed.add(home, files["one"])
    assert managed.remove(home, files["one"])
    assert managed.load(home) == []
    assert files["one"].exists()


@pytest.mark.parametrize("name", ["notes.txt", "folder"])
def test_only_a_markdown_file_can_be_managed(tmp_path, name):
    target = tmp_path / name
    if name == "folder":
        target.mkdir()
    else:
        target.write_text("x")
    with pytest.raises(ValueError):
        managed.check(target)


def test_a_path_with_nothing_there_is_refused(tmp_path):
    with pytest.raises(ValueError, match="nothing at"):
        managed.check(tmp_path / "gone.md")


def test_a_mangled_list_is_an_empty_one(home):
    home.mkdir()
    (home / managed.REGISTRY).write_text("{not json")
    assert managed.load(home) == []


def test_an_id_is_the_whole_path():
    assert managed.doc_id(Path("/home/me/docs/design.md")) == "home/me/docs/design"


def test_the_common_folder_is_where_the_files_part_ways():
    assert managed.common_folder(["r/A/a/one", "r/A/b/two"]) == "r/A"
    assert managed.common_folder(["r/A/a/one", "r/A/b/two", "r/B/b/three"]) == "r"
    assert managed.common_folder(["r/A/a/one"]) == "r/A/a"
    assert managed.common_folder(["x/one", "y/two"]) == ""


# --- the tree ----------------------------------------------------------------

def test_two_files_show_only_the_folders_that_tell_them_apart(home, files):
    managed.add(home, files["one"])
    managed.add(home, files["two"])
    assert shape(workspace_for(home).tree()) == [
        ("a/", ["file1"]), ("b/", ["file2"])
    ]


def test_a_file_from_elsewhere_brings_the_level_above_back(home, files):
    for key in ("one", "two", "three"):
        managed.add(home, files[key])
    assert shape(workspace_for(home).tree()) == [
        ("A/", [("a/", ["file1"]), ("b/", ["file2"])]),
        ("B/", [("b/", ["file3"])]),
    ]


def test_ids_do_not_move_when_the_tree_grows(home, files):
    managed.add(home, files["one"])
    before = set(workspace_for(home).documents())
    managed.add(home, files["three"])
    assert before <= set(workspace_for(home).documents())


def test_a_single_file_is_a_single_row(home, files):
    managed.add(home, files["one"])
    assert shape(workspace_for(home).tree()) == ["file1"]


def test_rows_are_called_what_the_filesystem_calls_them(home, tmp_path):
    managed.add(home, write(tmp_path / "src" / "README.md"))
    managed.add(home, write(tmp_path / "docs" / "ome-zarr_notes.md"))
    labels = {n.name: n.label for n in workspace_for(home).tree()}
    assert labels == {"src": "src", "docs": "docs"}


def test_a_file_that_has_gone_is_skipped_but_stays_listed(home, files):
    managed.add(home, files["one"])
    managed.add(home, files["two"])
    files["two"].unlink()

    workspace = workspace_for(home)
    assert list(workspace.documents()) == [doc(files["one"])]
    assert workspace.missing() == [files["two"]]
    assert len(managed.load(home)) == 2


# --- the server --------------------------------------------------------------

@pytest.fixture()
def server(home, files):
    managed.add(home, files["one"])
    managed.add(home, files["two"])
    workspace = workspace_for(home)
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


def page(workspace, path: Path) -> BeautifulSoup:
    html = (workspace.outputs / f"{doc(path)}.html").read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


def test_health_says_the_server_is_managed(server):
    base, _ = server
    with urllib.request.urlopen(base + "/api/health") as response:
        assert json.loads(response.read())["managed"] is True


def test_the_page_lives_under_the_home_not_beside_the_file(server, files):
    _, workspace = server
    assert (workspace.outputs / f"{doc(files['one'])}.html").exists()
    assert sorted(p.name for p in files["one"].parent.iterdir()) == ["file1.md"]


def test_the_sidebar_hands_back_real_paths(server, files):
    _, workspace = server
    nav = page(workspace, files["one"]).find("nav", id="sidebar")
    shared = doc(files["one"]).rsplit("/", 2)[0]  # .../root/A

    assert "sidebar--managed" in nav["class"]
    assert nav.find("ul", class_="tree")["data-path"] == shared
    assert nav.find(id="sidebar-root")["data-path"] == shared
    assert [d["data-path"] for d in nav.select("details.tree__folder")] == [
        f"{shared}/a", f"{shared}/b"
    ]
    assert nav.find(class_="sidebar__title")["title"] == f"/{shared}"


def test_the_sidebar_offers_no_way_to_rearrange_the_filesystem(server, files):
    _, workspace = server
    nav = page(workspace, files["one"]).find("nav", id="sidebar")
    actions = {b["data-action"] for b in nav.select(".tree__action")}

    assert not actions & {"create", "new-folder", "import", "delete-folder"}
    bin_ = nav.select_one('.tree__file [data-action="delete"]')
    assert bin_.has_attr("data-managed") and bin_["title"] == "Stop managing"


def test_a_knowledge_base_sidebar_is_unchanged(tmp_path):
    inputs = tmp_path / "markdown_inputs"
    write(inputs / "notes" / "weekly.md")
    workspace = Workspace(inputs=inputs, outputs=tmp_path / "out")
    workspace.rebuild_all()

    html = (tmp_path / "out" / "notes" / "weekly.html").read_text(encoding="utf-8")
    nav = BeautifulSoup(html, "html.parser").find("nav", id="sidebar")
    assert "sidebar--managed" not in nav["class"]
    assert nav.find("ul", class_="tree")["data-path"] == ""
    assert {"create", "new-folder", "import", "delete-folder"} <= {
        b["data-action"] for b in nav.select(".tree__action")
    }


@pytest.mark.parametrize(
    "path, payload",
    [
        ("/api/documents/create", {"path": "fresh"}),
        ("/api/folders/create", {"path": "fresh"}),
        ("/api/documents", {"name": "x.md", "content": "# x\n"}),
    ],
)
def test_requests_that_would_write_into_the_filesystem_are_refused(server, path, payload):
    base, _ = server
    status, body = call(base, path, payload)
    assert status == 400
    assert "stays where it is" in body["error"]


def test_moving_a_document_is_refused_and_moves_nothing(server, files):
    base, _ = server
    status, _ = call(base, "/api/documents/move",
                     {"from": doc(files["one"]), "to": "elsewhere"})
    assert status == 400
    assert files["one"].exists()


def test_dropping_a_path_on_the_sidebar_adds_it(server, home, files):
    base, workspace = server
    status, body = call(base, "/api/documents/link",
                        {"path": str(files["three"]), "page": doc(files["one"])})

    assert status == 201 and body["managed"] is True
    assert body["document"]["id"] == doc(files["three"])
    assert files["three"] in managed.load(home)
    # The tree grew a level, and the page on screen was handed the new panel.
    assert 'data-folder="B"' in body["sidebar"]
    assert not files["three"].with_suffix("").is_symlink()


def test_the_bin_stops_managing_and_leaves_the_file_and_comments(server, home, files):
    base, workspace = server
    status, _ = call(base, "/api/annotations",
                     {"document": doc(files["two"]), "quote": "Some prose",
                      "body": "keep me"})
    assert status == 201

    status, _ = call(base, "/api/documents/delete", {"document": doc(files["two"])})
    assert status == 200
    assert files["two"] not in managed.load(home)
    assert files["two"].exists()
    assert files["two"].with_name("file2.ann.json").exists()
    assert not (workspace.outputs / f"{doc(files['two'])}.html").exists()


def test_editing_writes_the_real_file(server, files):
    base, _ = server
    status, body = call(base, "/api/block", {
        "document": doc(files["one"]), "start": 2, "end": 3, "text": "Rewritten.",
    })
    assert status == 200 and body["changed"]
    assert "Rewritten." in files["one"].read_text(encoding="utf-8")


def test_settings_live_in_the_home_not_beside_the_documents(server, home, files):
    base, _ = server
    shared = doc(files["one"]).rsplit("/", 2)[0]
    status, _ = call(base, "/api/tree/label",
                     {"key": f"{shared}/a", "kind": "folder", "label": "Alpha"})
    assert status == 200
    # The strip under the tree means the top of what is drawn.
    status, body = call(base, "/api/tree/order", {"folder": "", "order": ["b", "a"]})
    assert status == 200 and body["order"] == ["b", "a"]

    assert (home / ".mdweave-labels.json").exists()
    assert json.loads((home / ".mdweave-order.json").read_text()) == {shared: ["b", "a"]}
    root = files["one"].parents[2]
    assert not list(root.rglob(".mdweave-*"))


def test_pulling_is_refused(server):
    base, _ = server
    status, body = call(base, "/api/pull", {})
    assert status == 400 and "no remote" in body["error"]


# --- the command line --------------------------------------------------------

def test_serve_hands_a_file_to_the_managed_server_already_up(server, home, files, capsys):
    base, _ = server
    port = int(base.rsplit(":", 1)[1])

    code = cli.main(["serve", str(files["three"]), "--port", str(port)])

    assert code == 0
    assert files["three"] in managed.load(home)
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == f"http://127.0.0.1:{port}/{doc(files['three'])}.html"


def test_serve_will_not_add_to_a_knowledge_base_server(tmp_path, home, files, capsys):
    inputs = tmp_path / "markdown_inputs"
    write(inputs / "only.md")
    workspace = Workspace(inputs=inputs, outputs=tmp_path / "out")
    workspace.rebuild_all()
    httpd = make_server(workspace, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        code = cli.main(["serve", str(files["one"]), "--port", str(httpd.server_port)])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert code == 1
    assert "knowledge base" in capsys.readouterr().err
    assert managed.load(home) == []
    assert not any(p.is_symlink() for p in inputs.iterdir())


def test_serve_refuses_a_file_that_is_not_markdown(tmp_path, home, capsys):
    target = tmp_path / "notes.txt"
    target.write_text("x")
    assert cli.main(["serve", str(target)]) == 1
    assert "only .md" in capsys.readouterr().err


def test_managed_lists_and_removes(home, files, capsys):
    # Nothing is listening on this port, so the list is edited directly.
    managed.add(home, files["one"])
    managed.add(home, files["two"])
    files["two"].unlink()

    assert cli.main(["managed", "--port", "1"]) == 0
    listing = capsys.readouterr().out
    assert str(files["one"]) in listing and f"{files['two']}  (missing)" in listing

    assert cli.main(["managed", "--remove", str(files["one"]), "--port", "1"]) == 0
    assert managed.load(home) == [files["two"]]
    assert files["one"].exists()


# --- adding a folder ---------------------------------------------------------

@pytest.fixture()
def folder(tmp_path):
    """Markdown at two depths, plus the things a folder walk must leave out."""
    root = tmp_path / "proj"
    write(root / "README.md")
    write(root / "docs" / "design.md")
    write(root / "docs" / "deep" / "notes.md")
    (root / "docs" / "todo.txt").write_text("not markdown")
    write(root / ".git" / "hidden.md")
    write(root / "docs" / ".draft.md")
    (root / "docs" / "dangling.md").symlink_to(root / "nowhere.md")
    return root


def test_a_folder_names_every_markdown_file_under_it(folder):
    assert managed.expand(folder) == [
        folder / "README.md",
        folder / "docs" / "design.md",
        folder / "docs" / "deep" / "notes.md",
    ]


def test_a_folder_with_no_markdown_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="no .md files"):
        managed.expand(tmp_path / "empty")


def test_adding_many_reports_only_the_new_ones(home, folder):
    paths = managed.expand(folder)
    managed.add(home, paths[0])
    assert managed.add_many(home, paths) == paths[1:]
    assert managed.load(home) == paths


def test_dropping_a_folder_adds_its_files_in_one_request(server, home, folder, files):
    base, workspace = server
    status, body = call(base, "/api/documents/link",
                        {"path": str(folder), "page": doc(files["one"])})

    assert status == 201
    assert body["added"] == [doc(p) for p in managed.expand(folder)]
    for path in managed.expand(folder):
        assert path in managed.load(home)
        assert (workspace.outputs / f"{doc(path)}.html").exists()


def test_serve_hands_a_folder_to_the_managed_server_already_up(
    server, home, folder, capsys
):
    base, _ = server
    port = int(base.rsplit(":", 1)[1])

    assert cli.main(["serve", str(folder), "--port", str(port)]) == 0

    out = capsys.readouterr().out.strip().splitlines()
    assert "managing 3 .md files" in out[0] and "(3 new)" in out[0]
    assert out[1:] == [
        f"http://127.0.0.1:{port}/{doc(p)}.html" for p in managed.expand(folder)
    ]


def test_serve_starts_on_a_folder_and_names_each_file(home, folder, monkeypatch):
    seen = {}

    def fake_serve(*args, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr("mdweave.serve.serve", fake_serve)
    assert cli.main(["serve", str(folder), "--port", "1"]) == 0

    assert managed.load(home) == managed.expand(folder)
    assert seen["focus"] == tuple(doc(p) for p in managed.expand(folder))
    assert seen["workspace"].managed


def test_removing_a_folder_takes_off_everything_under_it(home, folder, files, capsys):
    managed.add(home, files["one"])
    managed.add_many(home, managed.expand(folder))

    assert cli.main(["managed", "--remove", str(folder / "docs"), "--port", "1"]) == 0
    assert managed.load(home) == [files["one"], folder / "README.md"]
    assert (folder / "docs" / "design.md").exists()


# --- moving ------------------------------------------------------------------

def test_mv_moves_a_file_and_its_comments_and_the_record(home, files, capsys):
    managed.add(home, files["one"])
    files["one"].with_name("file1.ann.json").write_text('{"version": 1}')
    target = files["one"].parents[2] / "elsewhere" / "renamed.md"

    assert cli.main(["mv", str(files["one"]), str(target), "--port", "1"]) == 0

    assert target.exists() and not files["one"].exists()
    assert target.with_name("renamed.ann.json").exists()
    assert managed.load(home) == [target]
    assert f"{files['one']} -> {target}" in capsys.readouterr().out


def test_mv_into_an_existing_folder_keeps_the_name(home, files):
    managed.add(home, files["one"])
    folder = files["two"].parent

    assert cli.main(["mv", str(files["one"]), str(folder), "--port", "1"]) == 0
    assert managed.load(home) == [folder / "file1.md"]


def test_mv_a_folder_carries_every_listed_file_and_label(home, files):
    for key in ("one", "two", "three"):
        managed.add(home, files[key])
    workspace = workspace_for(home)
    a_folder = files["one"].parents[1]  # root/A
    workspace.set_label(f"{doc(files['one']).rsplit('/', 2)[0]}/a", "Alpha")
    renamed = a_folder.with_name("Z")

    assert cli.main(["mv", str(a_folder), str(renamed), "--port", "1"]) == 0

    assert managed.load(home) == [
        renamed / "a" / "file1.md", renamed / "b" / "file2.md", files["three"]
    ]
    assert (renamed / "a" / "file1.md").exists() and not a_folder.exists()
    labels = json.loads((home / ".mdweave-labels.json").read_text())
    assert labels == {f"{renamed.as_posix().lstrip('/')}/a": "Alpha"}


def test_mv_removes_the_pages_named_after_the_old_paths(home, files):
    managed.add(home, files["one"])
    workspace = workspace_for(home)
    workspace.rebuild_all()
    old_page = workspace.outputs / f"{doc(files['one'])}.html"
    assert old_page.exists()

    target = files["one"].with_name("moved.md")
    assert cli.main(["mv", str(files["one"]), str(target), "--port", "1"]) == 0
    assert not old_page.exists()


def test_mv_catches_up_with_a_move_made_by_hand(home, files, capsys):
    managed.add(home, files["one"])
    target = files["one"].with_name("by_hand.md")
    files["one"].rename(target)

    assert cli.main(["mv", str(files["one"]), str(target), "--port", "1"]) == 0
    assert managed.load(home) == [target]
    assert "already moved on disk" in capsys.readouterr().out


@pytest.mark.parametrize("case", ["unmanaged", "exists", "not-markdown", "into-itself"])
def test_mv_refuses_and_changes_nothing(home, files, tmp_path, capsys, case):
    managed.add(home, files["one"])
    managed.add(home, files["two"])
    source, target = {
        "unmanaged": (files["three"], tmp_path / "x.md"),
        "exists": (files["one"], files["two"]),
        "not-markdown": (files["one"], files["one"].with_name("notes.txt")),
        "into-itself": (files["one"].parents[1], files["one"].parent / "deeper"),
    }[case]

    assert cli.main(["mv", str(source), str(target), "--port", "1"]) == 1
    assert capsys.readouterr().err.startswith("error:")
    assert managed.load(home) == [files["one"], files["two"]]
    assert files["one"].exists() and files["two"].exists() and files["three"].exists()


def test_mv_tells_a_running_server_to_re_render(server, home, files, capsys):
    base, workspace = server
    port = int(base.rsplit(":", 1)[1])
    target = files["one"].parents[2] / "C" / "file1.md"

    assert cli.main(["mv", str(files["one"]), str(target), "--port", str(port)]) == 0

    assert (workspace.outputs / f"{doc(target)}.html").exists()
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == f"http://127.0.0.1:{port}/{doc(target)}.html"
    # Every page's sidebar was rebuilt: file2's now shows the new folder.
    other = (workspace.outputs / f"{doc(files['two'])}.html").read_text()
    assert 'data-folder="C"' in other
