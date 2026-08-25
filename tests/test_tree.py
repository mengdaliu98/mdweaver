"""Tests for the sidebar's document tree."""

from __future__ import annotations

import pytest

from mdweave.cli import main
from mdweave.tree import build_tree, document_ids, humanize, relative_prefix


# --- labels ---------------------------------------------------------------

@pytest.mark.parametrize(
    "name, expected",
    [
        ("system_for_bio_literature_research", "System for bio literature research"),
        ("mdweave_style_reference", "Mdweave style reference"),
        ("notes", "Notes"),
        ("a", "A"),
        ("", ""),
        ("_leading_underscore", "Leading underscore"),
        ("trailing_underscore_", "Trailing underscore"),
        ("double__underscore", "Double underscore"),
        ("2026_review", "2026 review"),
        # Only the first letter is touched; existing capitals survive.
        ("OME_Zarr_notes", "OME Zarr notes"),
        ("iPhone_notes", "IPhone notes"),
        # Hyphens are left alone -- they are not underscores.
        ("ome-zarr-layout-planner", "Ome-zarr-layout-planner"),
    ],
)
def test_humanize(name, expected):
    assert humanize(name) == expected


def test_humanize_does_not_lowercase_the_rest_of_the_label():
    assert humanize("notes_about_CRAM_and_BAM") == "Notes about CRAM and BAM"


# --- discovery ------------------------------------------------------------

def test_document_ids_are_paths_without_the_suffix(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "top.md").write_text("x")
    (tmp_path / "notes" / "nested.md").write_text("x")

    assert sorted(document_ids(tmp_path)) == ["notes/nested", "top"]


def test_document_ids_skips_dotted_directories(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "config.md").write_text("x")
    (tmp_path / "real.md").write_text("x")

    assert list(document_ids(tmp_path)) == ["real"]


def test_document_ids_ignores_non_markdown(tmp_path):
    (tmp_path / "doc.md").write_text("x")
    (tmp_path / "doc.ann.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("x")

    assert list(document_ids(tmp_path)) == ["doc"]


def test_document_ids_on_a_missing_directory_is_empty(tmp_path):
    assert document_ids(tmp_path / "absent") == {}


# --- tree shape -----------------------------------------------------------

def test_a_flat_list_becomes_flat_nodes():
    tree = build_tree(["beta", "alpha"])
    assert [n.label for n in tree] == ["Alpha", "Beta"]
    assert all(not n.is_dir for n in tree)
    assert [n.doc_id for n in tree] == ["alpha", "beta"]


def test_nested_ids_become_folders():
    tree = build_tree(["notes/weekly", "notes/daily", "top"])

    assert [n.label for n in tree] == ["Notes", "Top"]
    folder = tree[0]
    assert folder.is_dir and folder.doc_id is None
    assert [c.label for c in folder.children] == ["Daily", "Weekly"]
    assert [c.doc_id for c in folder.children] == ["notes/daily", "notes/weekly"]


def test_folders_sort_before_files():
    tree = build_tree(["aaa_file", "zzz_folder/doc"])
    assert [n.is_dir for n in tree] == [True, False]
    assert [n.label for n in tree] == ["Zzz folder", "Aaa file"]


def test_deeply_nested_paths_share_intermediate_folders():
    tree = build_tree(["a/b/one", "a/b/two", "a/three"])

    (a,) = tree
    assert [c.label for c in a.children] == ["B", "Three"]
    assert [c.doc_id for c in a.children[0].children] == ["a/b/one", "a/b/two"]


def test_a_folder_and_a_document_may_share_a_name():
    tree = build_tree(["notes", "notes/inner"])
    assert sorted((n.label, n.is_dir) for n in tree) == [("Notes", False), ("Notes", True)]


def test_empty_input_gives_an_empty_tree():
    assert build_tree([]) == []


@pytest.mark.parametrize(
    "doc_id, expected", [("top", ""), ("a/b", "../"), ("a/b/c", "../../")]
)
def test_relative_prefix(doc_id, expected):
    assert relative_prefix(doc_id) == expected


# --- end to end through the CLI -------------------------------------------

@pytest.fixture()
def workspace(tmp_path):
    src = tmp_path / "markdown_inputs"
    (src / "notes").mkdir(parents=True)
    (src / "system_for_bio_research.md").write_text("# Bio\n\nSome text.\n")
    (src / "notes" / "weekly_update.md").write_text("# Weekly\n\nMore text.\n")
    return src, tmp_path / "html_outputs"


def test_build_mirrors_the_directory_structure(workspace):
    src, out = workspace
    assert main(["build", str(src), "-o", str(out)]) == 0

    assert (out / "system_for_bio_research.html").exists()
    assert (out / "notes" / "weekly_update.html").exists()


def test_a_nested_page_reaches_its_assets_and_siblings(workspace):
    src, out = workspace
    main(["build", str(src), "-o", str(out)])

    html = (out / "notes" / "weekly_update.html").read_text()
    assert 'href="../assets/mdweave.css"' in html
    assert 'src="../assets/notes.js"' in html
    assert 'href="../system_for_bio_research.html"' in html

    top = (out / "system_for_bio_research.html").read_text()
    assert 'href="assets/mdweave.css"' in top
    assert 'href="notes/weekly_update.html"' in top


def test_every_page_lists_every_document(workspace):
    src, out = workspace
    main(["build", str(src), "-o", str(out)])

    for page in [out / "system_for_bio_research.html", out / "notes" / "weekly_update.html"]:
        html = page.read_text()
        assert "System for bio research" in html
        assert "Weekly update" in html


def test_the_current_document_is_marked_active(workspace):
    src, out = workspace
    main(["build", str(src), "-o", str(out)])

    html = (out / "system_for_bio_research.html").read_text()
    assert 'aria-current="page"' in html
    active = html.split('tree__row--active')[1]
    assert "System for bio research" in active.split("</a>")[0]


def test_the_page_title_is_humanized(workspace):
    src, out = workspace
    main(["build", str(src), "-o", str(out)])

    assert "<title>System for bio research</title>" in (
        out / "system_for_bio_research.html"
    ).read_text()


def test_building_one_file_still_renders_the_whole_tree(workspace):
    """A single-file rebuild must not produce a sidebar with one entry."""
    src, out = workspace
    assert main(["build", str(src / "system_for_bio_research.md"), "-o", str(out)]) == 0

    html = (out / "system_for_bio_research.html").read_text()
    assert "Weekly update" in html
    assert not (out / "notes" / "weekly_update.html").exists()


def test_no_markdown_anywhere_is_an_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["build", str(empty), "-o", str(tmp_path / "out")]) == 1
