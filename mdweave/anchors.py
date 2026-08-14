"""Resolve text-quote anchors against rendered HTML.

The markdown is rendered to HTML first, then annotations are located by
searching the *visible text* of that HTML. This keeps the anchor model
independent of markdown syntax: a quote still resolves after the surrounding
prose is reflowed, re-wrapped, or moved to a different block.

Whitespace is collapsed before matching, so a quote written on one line still
matches text that the renderer split across several.
"""

from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup, NavigableString

# Text inside these elements is never annotatable.
SKIP_TAGS = {"script", "style", "head", "title"}


@dataclass
class Segment:
    """A slice of one text node covered by a resolved anchor."""

    node: NavigableString
    start: int
    end: int


class TextIndex:
    """Flattened, whitespace-normalised view of a document's visible text.

    Keeps a per-character map back to the originating text node so a match in
    the flattened string can be spliced into the real DOM.
    """

    def __init__(self, soup: BeautifulSoup) -> None:
        chars: list[str] = []
        origins: list[tuple[NavigableString, int]] = []

        for node in soup.find_all(string=True):
            if node.parent is not None and node.parent.name in SKIP_TAGS:
                continue
            if isinstance(node, NavigableString) and type(node) is not NavigableString:
                continue  # Comment, Doctype, CData, ...
            for offset, ch in enumerate(node):
                if ch.isspace():
                    # Collapse whitespace runs, including across node boundaries.
                    if chars and chars[-1] == " ":
                        continue
                    chars.append(" ")
                else:
                    chars.append(ch)
                origins.append((node, offset))

        self.text = "".join(chars)
        self._origins = origins

    def find(self, target) -> tuple[int, int] | None:
        """Locate a TextTarget in the flattened text. Returns (start, end)."""
        quote = normalize(target.quote)
        if not quote:
            return None

        spans = _all_occurrences(self.text, quote)
        if not spans:
            return None

        prefix = normalize(target.prefix)
        suffix = normalize(target.suffix)

        # Narrow by context, but never narrow all the way to nothing: a stale
        # prefix/suffix should degrade to a quote-only match, not a lost
        # annotation.
        # Context is compared against a trimmed window: `normalize` strips the
        # boundary space off the stored prefix/suffix, so an exact endswith /
        # startswith against the raw text would never match.
        candidates = spans
        if prefix:
            window = len(prefix) + 4
            narrowed = [
                s
                for s in candidates
                if self.text[max(0, s[0] - window) : s[0]].rstrip().endswith(prefix)
            ]
            candidates = narrowed or candidates
        if suffix:
            window = len(suffix) + 4
            narrowed = [
                s
                for s in candidates
                if self.text[s[1] : s[1] + window].lstrip().startswith(suffix)
            ]
            candidates = narrowed or candidates

        # `occurrence` indexes the *unfiltered* match list, which is what
        # `selector_for` records. Honour it only when it agrees with the
        # context filter; otherwise the context is the better evidence.
        idx = target.occurrence
        if 0 <= idx < len(spans) and spans[idx] in candidates:
            return spans[idx]
        return candidates[0]

    def segments(self, start: int, end: int) -> list[Segment]:
        """Map a flattened-text span onto slices of the underlying text nodes."""
        segments: list[Segment] = []
        for i in range(start, end):
            node, offset = self._origins[i]
            if segments and segments[-1].node is node:
                # Extend through any collapsed whitespace inside this node.
                segments[-1].end = offset + 1
            else:
                segments.append(Segment(node=node, start=offset, end=offset + 1))
        return segments


def normalize(text: str) -> str:
    """Collapse whitespace runs to single spaces and strip the ends."""
    return " ".join(text.split())


# How much surrounding text a new annotation records to disambiguate its quote.
CONTEXT_CHARS = 48


def occurrence_index(text: str, quote: str, start: int) -> int:
    """How many matches of `quote` begin before `start`.

    Counts overlapping matches, matching `_all_occurrences`, so the result is
    a valid index into the list `find` searches.
    """
    return sum(1 for s, _ in _all_occurrences(text, quote) if s < start)


def selector_for(text: str, start: int, end: int, context: int = CONTEXT_CHARS):
    """Build a TextTarget describing `text[start:end]`.

    The inverse of `TextIndex.find`: given a span the reader selected, record
    enough about it to find that same span again in a freshly rendered
    document. `assets/annotate.js` reimplements this in the browser, so the two
    must stay in step -- `test_selector_round_trip*` guards that.
    """
    from .model import TextTarget

    quote = text[start:end]
    return TextTarget(
        quote=normalize(quote),
        prefix=normalize(text[max(0, start - context) : start]),
        suffix=normalize(text[end : end + context]),
        occurrence=occurrence_index(text, normalize(quote), start),
    )


def _all_occurrences(haystack: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = haystack.find(needle)
    while start != -1:
        spans.append((start, start + len(needle)))
        start = haystack.find(needle, start + 1)
    return spans
