"""Editing the markdown behind a rendered page.

The browser only knows what it can see: "the block that came from lines 14-18",
and character offsets into that block's visible text. This module turns those
back into slices of the markdown file.

Three operations, and the last two are the interesting ones:

* replacing a block wholesale, which is what clicking a paragraph and typing
  amounts to -- the source range is already known, so it is a line splice.
* cutting a span of *visible* text, which has to cross back over the rendering.
  `**bold**` shows as four characters and is stored as eight, so a selection
  offset is not a source offset. `_source_positions` aligns the two.
* extracting a span of visible text, which is the same walk run backwards:
  instead of the markdown either side of the selection, the markdown *under*
  it, so that copying can put that on the clipboard rather than flat prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from bs4 import BeautifulSoup

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Span:
    """Visible characters [start, stop) of the block at [line, end).

    All the browser can say without knowing any markdown: a source-line range
    it read off the markup, and offsets into the text it can actually see.
    """

    line: int
    end: int
    start: int
    stop: int


@dataclass(frozen=True)
class Cut(Span):
    """A span to remove from the document."""


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
    return rendered_texts_in(markdown, [(line, end)])[(line, end)]


def rendered_texts_in(
    markdown: str, blocks: list[tuple[int, int]]
) -> dict[tuple[int, int], str | None]:
    """`rendered_text_in` for several blocks, off a single render.

    A selection touches one block per paragraph it crosses, and re-rendering
    the whole document once per block would make copying a long selection cost
    a dozen full renders.
    """
    from .render import render_markdown

    soup = BeautifulSoup(render_markdown(markdown), "html.parser")
    found: dict[tuple[int, int], str | None] = {}
    for line, end in blocks:
        element = soup.find(
            attrs={"data-src-start": str(line), "data-src-end": str(end)}
        )
        found[(line, end)] = element.get_text() if element else None
    return found


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


# Characters that are only ever inline markup, never text a reader could have
# selected -- so a slice may reach out over them looking for the rest of a
# construct. `!` and `[` only open one; `](url)` closes one and is a regex.
_MARKUP_BEFORE = "*_~`[!"
_MARKUP_AFTER = "*_~`"
_LINK_TAIL = re.compile(r"\]\([^)]*\)")


def _candidates(source: str, first: int, last: int) -> list[tuple[int, int]]:
    """Slices of the source worth offering for a selection, widest first.

    Selecting the word in `**word**` selects four characters, and the four
    characters of source beneath them are `word`: the emphasis sits outside the
    selection and would be lost. So the slice is also offered grown over the
    markup either side, and the caller keeps whichever candidate still renders
    as the text that was actually selected.
    """
    left = first
    while left > 0 and source[left - 1] in _MARKUP_BEFORE:
        left -= 1

    right = last
    tail = _LINK_TAIL.match(source, right)
    if tail:  # `](url)` -- the selection was the whole of a link's text
        right = tail.end()
    while right < len(source) and source[right] in _MARKUP_AFTER:
        right += 1

    grown = []
    for pair in ((left, right), (first, right), (left, last), (first, last)):
        if pair not in grown:
            grown.append(pair)
    return grown


def extract_block(
    block_md: str, start: int, stop: int, rendered: str | None = None
) -> str:
    """The markdown behind the visible span [start, stop) of one block.

    The mirror of `cut_block`: same alignment, but it keeps what that one
    throws away. `rendered` is the block's visible text; pass the one resolved
    against the whole document, since that is what the browser measured.
    """
    if rendered is None:
        rendered = rendered_text(block_md)
    start = max(0, min(start, len(rendered)))
    stop = max(start, min(stop, len(rendered)))

    wanted = rendered[start:stop]
    if not wanted.strip():
        return ""

    # Everything a reader could have reached is selected, so this is the whole
    # block and the whole block comes back verbatim -- hashes, bullets, fences
    # and all. Not `start == 0 and stop == len(rendered)`: a list's visible text
    # opens with the newline before its first `<li>`, which no selection can
    # start at, and a fence's ends with the one after the last line of code.
    if not rendered[:start].strip() and not rendered[stop:].strip():
        return block_md

    positions = _source_positions(rendered, block_md)
    first = positions[start]
    last = max(positions[stop - 1] + 1, first)

    # The widest slice that still renders as exactly what was selected. A wider
    # one would be markup the selection cut through -- `bold** wo`, or a link's
    # URL spilled out as literal text -- and pasting that is worse than pasting
    # prose with no formatting at all, which is what the last line does.
    # Compared stripped, because a slice that starts mid-line loses its leading
    # space to the renderer while the clipboard should keep it.
    for lo, hi in _candidates(block_md, first, last):
        candidate = block_md[lo:hi]
        if rendered_text(candidate).strip() == wanted.strip():
            return candidate
    return wanted


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
    visible = rendered_texts_in(markdown, [(cut.line, cut.end) for cut in cuts])

    for cut in sorted(cuts, key=lambda c: c.line, reverse=True):
        if not 0 <= cut.line < len(lines) or cut.end <= cut.line:
            continue
        block = "\n".join(lines[cut.line : cut.end]).rstrip("\n")
        kept = cut_block(
            block, cut.start, cut.stop, rendered=visible.get((cut.line, cut.end))
        )
        lines[cut.line : cut.end] = kept.splitlines() if kept.strip() else []

    return normalise("\n".join(lines))


def extract_spans(markdown: str, spans: list[Span]) -> str:
    """The markdown under a selection: `apply_cuts` read the other way round.

    Blocks come back in document order, separated by the blank line that keeps
    them separate blocks, so what lands on the clipboard is a small markdown
    document rather than a run-on paragraph.
    """
    lines = markdown.splitlines()
    visible = rendered_texts_in(markdown, [(span.line, span.end) for span in spans])

    taken = []
    for span in sorted(spans, key=lambda s: s.line):
        if not 0 <= span.line < len(lines) or span.end <= span.line:
            continue
        block = "\n".join(lines[span.line : span.end]).rstrip("\n")
        part = extract_block(
            block, span.start, span.stop, rendered=visible.get((span.line, span.end))
        )
        if part.strip():
            taken.append(part.strip("\n"))

    return "\n\n".join(taken)


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
