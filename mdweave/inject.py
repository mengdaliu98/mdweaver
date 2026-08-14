"""Splice highlight elements into rendered HTML.

Works on the parsed HTML rather than on the markdown source, so a highlight may
span inline formatting (`**bold**`, links) without producing invalid markup: the
range is cut at element boundaries and each piece is wrapped separately, all
pieces sharing one annotation id.
"""

from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup, NavigableString

from .anchors import Segment, TextIndex
from .model import Annotation, safe_id


@dataclass
class Placement:
    """Outcome of trying to anchor one annotation."""

    annotation: Annotation
    resolved: bool
    reason: str = ""


def inject(soup: BeautifulSoup, annotations: list[Annotation]) -> list[Placement]:
    """Wrap each annotation's target text in `<mark>`. Mutates `soup`.

    Returns one Placement per annotation, in input order, so the caller can
    report anchors that no longer match the document.
    """
    index = TextIndex(soup)
    placements: list[Placement] = []

    # (node id) -> list of (start, end, annotation, is_first_piece)
    edits: dict[int, list[tuple[int, int, Annotation, bool]]] = {}
    nodes: dict[int, NavigableString] = {}
    claimed: dict[int, list[tuple[int, int]]] = {}

    for ann in annotations:
        span = index.find(ann.target)
        if span is None:
            placements.append(
                Placement(ann, False, f"no text matching {ann.target.quote!r}")
            )
            continue

        segments = index.segments(*span)
        if _overlaps(segments, claimed):
            placements.append(
                Placement(ann, False, "overlaps an already-placed annotation")
            )
            continue

        for position, seg in enumerate(segments):
            key = id(seg.node)
            nodes[key] = seg.node
            edits.setdefault(key, []).append((seg.start, seg.end, ann, position == 0))
            claimed.setdefault(key, []).append((seg.start, seg.end))

        placements.append(Placement(ann, True))

    for key, node_edits in edits.items():
        _apply(nodes[key], sorted(node_edits))

    return placements


def _overlaps(
    segments: list[Segment], claimed: dict[int, list[tuple[int, int]]]
) -> bool:
    for seg in segments:
        for start, end in claimed.get(id(seg.node), []):
            if seg.start < end and start < seg.end:
                return True
    return False


def _apply(
    node: NavigableString, node_edits: list[tuple[int, int, Annotation, bool]]
) -> None:
    """Replace one text node with alternating plain text and `<mark>` pieces."""
    text = str(node)
    parent = node.parent
    if parent is None:
        return

    pieces: list = []
    cursor = 0
    soup = BeautifulSoup("", "html.parser")

    for start, end, ann, is_first in node_edits:
        if start > cursor:
            pieces.append(NavigableString(text[cursor:start]))
        pieces.append(_mark(soup, ann, text[start:end], is_first))
        cursor = end

    if cursor < len(text):
        pieces.append(NavigableString(text[cursor:]))

    at = parent.index(node)
    node.extract()
    for offset, piece in enumerate(pieces):
        parent.insert(at + offset, piece)


def _mark(soup: BeautifulSoup, ann: Annotation, text: str, is_first: bool):
    mark = soup.new_tag("mark")
    classes = ["hl", f"hl--{ann.color_token}"]
    if ann.status == "resolved":
        classes.append("hl--resolved")
    if ann.has_card:
        classes.append("hl--has-note")
    mark["class"] = classes
    mark["data-ann"] = ann.id

    # Only the first piece carries the id, so JS aligning a note to its
    # highlight always targets the start of the range.
    if is_first:
        mark["id"] = f"hl-{safe_id(ann.id)}"

    if custom := ann.custom_color:
        mark["style"] = f"--hl-custom: {custom};"

    mark.string = text
    return mark
