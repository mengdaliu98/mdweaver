"""Editing the markdown behind a rendered page.

The browser only knows what it can see: "the block that came from lines 14-18",
and character offsets into that block's visible text. This module turns those
back into slices of the markdown file.

Two operations, and the second is the interesting one:

* replacing a block wholesale, which is what clicking a paragraph and typing
  amounts to -- the source range is already known, so it is a line splice.
* cutting a span of *visible* text, which has to cross back over the rendering.
  `**bold**` shows as four characters and is stored as eight, so a selection
  offset is not a source offset. `_index_map` aligns the two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from bs4 import BeautifulSoup

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Cut:
    """Remove visible characters [start, stop) from the block at [line, end)."""

    line: int
    end: int
    start: int
    stop: int


def block_source(markdown: str, line: int, end: int) -> str:
    """The markdown for one block, as its data-src range names it.

    Trailing blank lines are trimmed: markdown-it hands a list the blank line
    that terminates it, and showing that in an editor is just noise.
    """
    lines = markdown.splitlines()
    if not 0 <= line < len(lines) or end <= line:
        raise IndexError(f"no block at lines {line}-{end}")
    return "\n".join(lines[line:end]).rstrip("\n")


def rendered_text(block_md: str) -> str:
    """The visible text of a block rendered on its own.

    Deliberately the text of the *element*, not of the fragment: markdown-it
    puts a newline after `</p>` which the browser never sees inside the block,
    and a one-character disagreement here skews every offset after it.

    Accurate for all but one case, which is why `rendered_text_in` exists.
    """
    from .render import render_markdown

    soup = BeautifulSoup(render_markdown(block_md), "html.parser")
    element = soup.find(True)
    return element.get_text() if element else soup.get_text()


def rendered_text_in(markdown: str, line: int, end: int) -> str | None:
    """The visible text of one block, taken from the whole-document render.

    A block is not always self-contained: `[^1]` needs its footnote definition,
    which lives further down the file, so in isolation it stays literal and in
    context it becomes `[1]`. One character of drift is enough to put every
    later offset in the wrong place, so cuts resolve against the same render
    the browser is looking at.
    """
    from .render import render_markdown

    soup = BeautifulSoup(render_markdown(markdown), "html.parser")
    element = soup.find(attrs={"data-src-start": str(line), "data-src-end": str(end)})
    return element.get_text() if element else None


def _source_positions(rendered: str, source: str) -> list[int]:
    """Where each visible character sits in the markdown that produced it.

    Visible text is very nearly a subsequence of its source -- rendering mostly
    *removes* characters (`**`, `[`, `](url)`) -- so a plain sequence alignment
    recovers the correspondence. A visible character with no counterpart (rare;
    entity expansion) is pinned to the start of the run it came out of.
    """
    positions = [0] * len(rendered)
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        None, rendered, source, autojunk=False
    ).get_opcodes():
        for k in range(i1, i2):
            positions[k] = j1 + (k - i1) if tag == "equal" else j1
    return positions


def cut_block(block_md: str, start: int, stop: int, rendered: str | None = None) -> str:
    """The block's markdown with the visible span [start, stop) removed.

    `rendered` is the block's visible text; pass the one resolved against the
    whole document when there is one, since that is what the browser measured.
    """
    if rendered is None:
        rendered = rendered_text(block_md)
    start = max(0, min(start, len(rendered)))
    stop = max(start, min(stop, len(rendered)))

    if start == stop:
        return block_md
    if start == 0 and stop == len(rendered):
        return ""  # the whole block goes, syntax and all

    # Both ends conservative: take exactly the source of the characters that
    # were selected, and no delimiters. Greedily swallowing the `**` around a
    # fully-selected word would be right for that case and wrong for a partial
    # one -- so instead let the emptied delimiters fall to _tidy_empty_inline.
    positions = _source_positions(rendered, block_md)
    first = positions[start]
    last = positions[stop - 1] + 1
    return _tidy_empty_inline(block_md[:first] + block_md[max(last, first) :])


# Emptying the text out of an inline construct leaves its delimiters behind:
# cut "bold" out of `**bold**` and `****` is what remains, which renders as
# four literal asterisks. These four forms can only be wreckage -- none of them
# means anything on its own -- so they are safe to sweep up.
_EMPTIED = (
    re.compile(r"\*\*\*\*"),
    re.compile(r"____"),
    re.compile(r"``"),
    re.compile(r"!?\[\]\([^)]*\)"),
)


def _tidy_empty_inline(source: str) -> str:
    for pattern in _EMPTIED:
        source = pattern.sub("", source)
    return source


def replace_block(markdown: str, line: int, end: int, text: str) -> str:
    """Put `text` in place of the block at [line, end)."""
    lines = markdown.splitlines()
    if not 0 <= line <= len(lines) or end < line:
        raise IndexError(f"no block at lines {line}-{end}")

    body = text.rstrip("\n").splitlines()
    # A blank line after, so the next block does not get absorbed into this one
    # when the edit shortens it. Doubles are cleaned up below.
    lines[line:end] = (body + [""]) if body else []
    return normalise("\n".join(lines))


def apply_cuts(markdown: str, cuts: list[Cut]) -> str:
    """Remove every cut span, bottom block first so line numbers stay valid."""
    lines = markdown.splitlines()

    # Measure every block against the original document before touching any of
    # it -- once the first cut lands, the line numbers in the rest are stale.
    visible = {
        (cut.line, cut.end): rendered_text_in(markdown, cut.line, cut.end)
        for cut in cuts
    }

    for cut in sorted(cuts, key=lambda c: c.line, reverse=True):
        if not 0 <= cut.line < len(lines) or cut.end <= cut.line:
            continue
        block = "\n".join(lines[cut.line : cut.end]).rstrip("\n")
        kept = cut_block(
            block, cut.start, cut.stop, rendered=visible.get((cut.line, cut.end))
        )
        lines[cut.line : cut.end] = kept.splitlines() if kept.strip() else []

    return normalise("\n".join(lines))


def normalise(markdown: str) -> str:
    """Collapse runs of blank lines, leaving code fences alone.

    Splicing blocks in and out leaves stray blank lines behind. Inside a fence
    they are content, not separation, so the collapse has to stop at one.
    """
    out: list[str] = []
    in_fence = False
    fence_marker = ""
    blanks = 0

    for raw in markdown.splitlines():
        marker = _FENCE.match(raw)
        if marker:
            token = marker.group(1)
            if not in_fence:
                in_fence, fence_marker = True, token[0]
            elif token[0] == fence_marker:
                in_fence = False

        if in_fence or raw.strip():
            blanks = 0
            out.append(raw)
            continue

        blanks += 1
        if blanks == 1:
            out.append("")

    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n" if out else ""
