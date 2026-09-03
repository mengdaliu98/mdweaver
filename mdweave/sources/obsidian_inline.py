"""Importer for the Obsidian `document-comments` inline markup.

That plugin stores annotations inside the markdown as HTML comments:

    ... over <!--c:kxejp-->CRAM/BAM<!--/c:kxejp--> + index.

    <!--co:kxejp by:me at:2026-08-14T00:42:44.308Z status:open quote:"CRAM/BAM"
    me (2026-08-14T00:42:44.308Z): BAM and CRAM are standard genomic ...
    -->

This module is an *adapter*, not a dependency: it converts that markup into the
canonical `Annotation` model and hands back clean markdown. Once a document has
been extracted, the sidecar JSON is the source of truth and this module is no
longer involved.
"""

from __future__ import annotations

import re

from ..anchors import normalize, occurrence_index
from ..model import DEFAULT_COLOR, Annotation, Comment, TextTarget

# <!--c:ID-->highlighted text<!--/c:ID-->
INLINE_RE = re.compile(
    r"<!--c:(?P<id>[A-Za-z0-9_-]+)-->(?P<quote>.*?)<!--/c:(?P=id)-->", re.DOTALL
)

# <!--co:ID key:value ... \n body lines \n -->
BLOCK_RE = re.compile(
    r"<!--co:(?P<id>[A-Za-z0-9_-]+)(?P<attrs>[^\n]*)\n(?P<body>.*?)\n?-->",
    re.DOTALL,
)

# key:value, where value is either "quoted with spaces" or a bare token.
ATTR_RE = re.compile(r'(\w+):(?:"([^"]*)"|(\S+))')

# author (timestamp): body   -- one reply per line
REPLY_RE = re.compile(r"^(?P<author>[^(:]+?)\s*(?:\((?P<at>[^)]*)\))?\s*:\s*(?P<body>.*)$")

CONTEXT_CHARS = 48

# Markdown punctuation that appears in the source but not in rendered text.
_MD_NOISE = re.compile(r"[*_`~]|\[|\]\([^)]*\)|^[|>#\-\s]+", re.MULTILINE)


def extract(markdown: str) -> tuple[str, list[Annotation]]:
    """Strip Obsidian comment markup out of `markdown`.

    Returns the cleaned markdown and the annotations recovered from it.
    """
    threads = _extract_blocks(markdown)
    cleaned_of_blocks = BLOCK_RE.sub("", markdown)
    cleaned, spans = _extract_inline(cleaned_of_blocks)

    annotations: list[Annotation] = []
    seen: set[str] = set()

    for ann_id, quote, position in spans:
        meta = threads.get(ann_id, {})
        annotations.append(
            Annotation(
                id=ann_id,
                target=_target_from_context(cleaned, quote, position),
                kind="comment" if meta.get("thread") else "highlight",
                color=meta.get("color", DEFAULT_COLOR),
                status=meta.get("status", "open"),
                thread=meta.get("thread", []),
            )
        )
        seen.add(ann_id)

    # A comment block whose inline marker was deleted still holds the quote, so
    # it can be re-anchored by text search alone rather than silently dropped.
    for ann_id, meta in threads.items():
        if ann_id in seen or not meta.get("quote"):
            continue
        annotations.append(
            Annotation(
                id=ann_id,
                target=TextTarget(quote=meta["quote"]),
                kind="comment" if meta.get("thread") else "highlight",
                color=meta.get("color", DEFAULT_COLOR),
                status=meta.get("status", "open"),
                thread=meta.get("thread", []),
            )
        )

    return _tidy_blank_lines(cleaned), annotations


def _extract_blocks(markdown: str) -> dict[str, dict]:
    """Parse `<!--co:...-->` comment blocks into thread metadata keyed by id."""
    threads: dict[str, dict] = {}
    for match in BLOCK_RE.finditer(markdown):
        # finditer, not findall: findall yields "" for a non-participating
        # group, which is indistinguishable from a genuinely empty value.
        attrs = {
            m.group(1): (m.group(2) if m.group(2) is not None else m.group(3))
            for m in ATTR_RE.finditer(match.group("attrs"))
        }
        default_author = attrs.get("by", "me")
        default_at = attrs.get("at")

        thread: list[Comment] = []
        for line in match.group("body").splitlines():
            line = line.strip()
            if not line:
                continue
            reply = REPLY_RE.match(line)
            if reply:
                thread.append(
                    Comment(
                        body=reply.group("body").strip(),
                        author=reply.group("author").strip() or default_author,
                        at=reply.group("at") or default_at,
                    )
                )
            elif thread:
                thread[-1].body += "\n" + line  # continuation of the previous reply
            else:
                thread.append(Comment(body=line, author=default_author, at=default_at))

        threads[match.group("id")] = {
            "thread": thread,
            "quote": attrs.get("quote", ""),
            "status": attrs.get("status", "open"),
            "color": attrs.get("color", DEFAULT_COLOR),
        }
    return threads


def _extract_inline(markdown: str) -> tuple[str, list[tuple[str, str, int]]]:
    """Unwrap inline markers, returning clean text and (id, quote, offset)."""
    out: list[str] = []
    spans: list[tuple[str, str, int]] = []
    cursor = 0
    length = 0

    for match in INLINE_RE.finditer(markdown):
        head = markdown[cursor : match.start()]
        out.append(head)
        length += len(head)

        quote = match.group("quote")
        spans.append((match.group("id"), quote, length))
        out.append(quote)
        length += len(quote)
        cursor = match.end()

    out.append(markdown[cursor:])
    return "".join(out), spans


def _target_from_context(text: str, quote: str, position: int) -> TextTarget:
    """Build a quote selector with enough surrounding context to disambiguate."""
    prefix = _strip_markdown(text[max(0, position - CONTEXT_CHARS) : position])
    suffix = _strip_markdown(text[position + len(quote) : position + len(quote) + CONTEXT_CHARS])

    return TextTarget(
        quote=normalize(quote),
        prefix=prefix,
        suffix=suffix,
        # Positional fallback if the context goes stale. Approximate: counted
        # over markdown source rather than rendered text.
        occurrence=occurrence_index(text, quote, position),
    )


def _strip_markdown(fragment: str) -> str:
    """Approximate the rendered text of a short markdown fragment."""
    return normalize(_MD_NOISE.sub(" ", fragment))


def _tidy_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)
