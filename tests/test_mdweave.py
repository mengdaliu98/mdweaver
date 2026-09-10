from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup

from mdweave.anchors import TextIndex, normalize
from mdweave.inject import inject
from mdweave.model import Annotation, Comment, TextTarget
from mdweave.render import render_document, render_markdown
from mdweave.sources import obsidian_inline, sidecar


def soup_of(markdown: str) -> BeautifulSoup:
    return BeautifulSoup(render_markdown(markdown), "html.parser")


def ann(quote: str, **kw) -> Annotation:
    target = TextTarget(
        quote=quote,
        prefix=kw.pop("prefix", ""),
        suffix=kw.pop("suffix", ""),
        occurrence=kw.pop("occurrence", 0),
    )
    kw.setdefault("thread", [Comment(body="note body")])
    return Annotation(id=kw.pop("id", "a1"), target=target, **kw)


# --- Obsidian importer ----------------------------------------------------

OBSIDIAN_DOC = """\
Serve byte ranges over <!--c:kxejp-->CRAM/BAM<!--/c:kxejp--> + index.

<!--co:kxejp by:me at:2026-08-14T00:42:44.308Z status:open quote:"CRAM/BAM"
me (2026-08-14T00:42:44.308Z): BAM and CRAM are genomic formats.
ana (2026-08-15T09:00:00.000Z): Worth linking the spec.
-->

Trailing text.
"""


def test_extract_recovers_annotation_and_cleans_markdown():
    cleaned, found = obsidian_inline.extract(OBSIDIAN_DOC)

    assert "<!--c:" not in cleaned and "<!--co:" not in cleaned
    assert "Serve byte ranges over CRAM/BAM + index." in cleaned

    (annotation,) = found
    assert annotation.id == "kxejp"
    assert annotation.target.quote == "CRAM/BAM"
    assert annotation.kind == "comment"
    assert annotation.status == "open"  # regression: findall() blanked this
    assert annotation.target.prefix.endswith("byte ranges over")
    assert annotation.target.suffix.startswith("+ index")


def test_extract_parses_a_multi_reply_thread():
    _, (annotation,) = obsidian_inline.extract(OBSIDIAN_DOC)

    assert [c.author for c in annotation.thread] == ["me", "ana"]
    assert annotation.thread[1].body == "Worth linking the spec."
    assert annotation.thread[0].at == "2026-08-14T00:42:44.308Z"


def test_extract_keeps_a_block_whose_inline_marker_was_deleted():
    orphaned = OBSIDIAN_DOC.replace("<!--c:kxejp-->", "").replace("<!--/c:kxejp-->", "")
    _, found = obsidian_inline.extract(orphaned)

    assert len(found) == 1
    assert found[0].target.quote == "CRAM/BAM"


def test_extract_is_a_noop_on_plain_markdown():
    cleaned, found = obsidian_inline.extract("# Title\n\nNo annotations here.\n")
    assert found == []
    assert cleaned.strip() == "# Title\n\nNo annotations here."


# --- anchoring ------------------------------------------------------------

def test_normalize_collapses_whitespace():
    assert normalize("  a \n\t b  ") == "a b"


def test_quote_matches_across_a_line_break():
    index = TextIndex(soup_of("one two\nthree four"))
    assert index.find(TextTarget(quote="two three")) is not None


def test_prefix_disambiguates_a_repeated_quote():
    index = TextIndex(soup_of("alpha TARGET omega. beta TARGET omega."))

    first = index.find(TextTarget(quote="TARGET", prefix="alpha"))
    second = index.find(TextTarget(quote="TARGET", prefix="beta"))

    assert first is not None and second is not None
    assert first[0] < second[0]


def test_occurrence_disambiguates_when_context_is_absent():
    index = TextIndex(soup_of("TARGET and TARGET and TARGET"))

    spans = [index.find(TextTarget(quote="TARGET", occurrence=n)) for n in range(3)]
    assert [s[0] for s in spans] == sorted(s[0] for s in spans)
    assert len(set(spans)) == 3


def test_stale_context_degrades_to_a_quote_match_rather_than_vanishing():
    index = TextIndex(soup_of("the quick brown fox"))
    span = index.find(TextTarget(quote="brown", prefix="text that no longer exists"))
    assert span is not None


def test_missing_quote_returns_none():
    index = TextIndex(soup_of("nothing to see"))
    assert index.find(TextTarget(quote="absent")) is None


# --- injection ------------------------------------------------------------

def test_highlight_is_wrapped_in_a_mark():
    soup = soup_of("Serve ranges over CRAM/BAM + index.")
    (placement,) = inject(soup, [ann("CRAM/BAM", id="kxejp")])

    assert placement.resolved
    mark = soup.find("mark")
    assert mark.get_text() == "CRAM/BAM"
    assert mark["data-ann"] == "kxejp"
    assert mark["id"] == "hl-kxejp"
    assert "hl--has-note" in mark["class"]


def test_highlight_spanning_inline_markup_produces_valid_html():
    soup = soup_of("Serve **bold text** here now")
    (placement,) = inject(soup, [ann("bold text here", id="x")])

    assert placement.resolved
    marks = soup.find_all("mark", attrs={"data-ann": "x"})
    assert len(marks) == 2, "range should be cut at the </strong> boundary"
    assert "".join(m.get_text() for m in marks) == "bold text here"
    # The <strong> is still well-formed -- the mark nests inside it.
    assert soup.find("strong").find("mark") is not None
    # Only the first piece is the scroll/positioning anchor.
    assert sum("id" in m.attrs for m in marks) == 1


def test_overlapping_annotations_are_reported_not_silently_merged():
    soup = soup_of("the quick brown fox jumps")
    placements = inject(soup, [ann("quick brown", id="a"), ann("brown fox", id="b")])

    assert [p.resolved for p in placements] == [True, False]
    assert "overlaps" in placements[1].reason
    assert soup.find_all("mark", attrs={"data-ann": "b"}) == []


def test_adjacent_annotations_in_one_text_node_both_land():
    soup = soup_of("alpha beta gamma delta")
    placements = inject(soup, [ann("alpha", id="a"), ann("gamma", id="b")])

    assert all(p.resolved for p in placements)
    assert [m.get_text() for m in soup.find_all("mark")] == ["alpha", "gamma"]


def test_unresolved_annotation_is_reported_with_a_reason():
    soup = soup_of("some text")
    (placement,) = inject(soup, [ann("not present", id="ghost")])

    assert not placement.resolved
    assert "not present" in placement.reason


# --- rendering ------------------------------------------------------------

def test_render_emits_highlight_and_matching_note_card():
    result = render_document("Serve over CRAM/BAM now.", [ann("CRAM/BAM", id="kxejp")])

    assert 'data-ann="kxejp"' in result.html
    assert 'id="note-kxejp"' in result.html
    # c5 is where the default sits: the slot yellow occupied when the six
    # colours were hues rather than positions.
    assert "note--c5" in result.html
    assert result.unresolved == []


def test_status_does_not_collide_with_the_open_ui_state():
    """Regression: `note--{{ status }}` rendered every note pre-expanded."""
    result = render_document("Serve over CRAM/BAM.", [ann("CRAM/BAM", status="open")])

    assert 'data-status="open"' in result.html
    assert "note--open" not in result.html


def test_resolved_status_is_marked_on_both_the_mark_and_the_note():
    result = render_document("Serve over CRAM/BAM.", [ann("CRAM/BAM", status="resolved")])

    assert "hl--resolved" in result.html
    assert 'data-status="resolved"' in result.html


def test_a_raw_css_colour_is_emitted_as_an_inline_custom_property():
    result = render_document(
        "Serve over CRAM/BAM.", [ann("CRAM/BAM", color="rgb(255 0 128)")]
    )

    assert "hl--custom" in result.html
    assert "--hl-custom: rgb(255 0 128)" in result.html
    assert "--note-custom: rgb(255 0 128)" in result.html


def test_a_highlight_without_a_thread_gets_no_card():
    result = render_document(
        "Serve over CRAM/BAM.", [ann("CRAM/BAM", kind="highlight", thread=[])]
    )

    assert "<mark" in result.html
    assert "note__body" not in result.html


def test_comment_bodies_are_escaped():
    hostile = Annotation(
        id="x",
        target=TextTarget(quote="text"),
        thread=[Comment(body="<script>alert(1)</script>")],
    )
    result = render_document("some text here", [hostile])

    assert "<script>alert(1)</script>" not in result.html
    assert "&lt;script&gt;" in result.html


@pytest.mark.parametrize(
    "markdown, expected",
    [
        # "<table" not "<table>": every block now carries its source range.
        ("| a | b |\n| - | - |\n| 1 | 2 |", "<table"),
        ("- [x] done\n- [ ] todo", "task-list-item"),
        ("Text.[^1]\n\n[^1]: A footnote.", "footnotes"),
        ("```python\nx = 1\n```", "codehilite"),
    ],
)
def test_markdown_extensions_are_enabled(markdown, expected):
    assert expected in render_markdown(markdown)


def test_headings_get_stable_ids():
    result = render_document("## Storage & Access\n\n## Storage & Access\n", [])

    assert 'id="storage-access"' in result.html
    assert 'id="storage-access-2"' in result.html


# --- sidecar round trip ---------------------------------------------------

def test_sidecar_survives_a_save_load_round_trip(tmp_path):
    original = [
        Annotation(
            id="kxejp",
            target=TextTarget(quote="CRAM/BAM", prefix="over", occurrence=2),
            color="green",
            status="resolved",
            tags=["genomics"],
            thread=[Comment(body="hi", author="ana", at="2026-08-14T00:00:00Z")],
            extra={"pinned": True},
        )
    ]
    path = tmp_path / "doc.ann.json"
    sidecar.save(path, original)
    (restored,) = sidecar.load(path)

    assert restored.to_dict() == original[0].to_dict()
    assert restored.extra["pinned"] is True  # unknown fields survive
    assert restored.target.occurrence == 2


def test_sidecar_path_derivation():
    from pathlib import Path

    assert sidecar.sidecar_path(Path("a/notes.md")).name == "notes.ann.json"


def test_missing_sidecar_is_not_an_error(tmp_path):
    assert sidecar.load(tmp_path / "absent.ann.json") == []


def test_sidecar_is_valid_json_with_a_version(tmp_path):
    path = tmp_path / "d.ann.json"
    sidecar.save(path, [ann("q")])
    assert json.loads(path.read_text())["version"] == sidecar.SCHEMA_VERSION
