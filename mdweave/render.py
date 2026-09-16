"""Markdown -> annotated HTML."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from .inject import Placement, inject
from .model import Annotation, safe_id
from .tree import Node, humanize, relative_prefix

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
THEME = HERE / "theme"
ASSETS = HERE / "assets"

PYGMENTS_STYLE = "friendly"

# Every top-level block carries the source lines it came from, as a half-open
# [start, end) range. This is what makes editing in place possible: the browser
# can say "the paragraph at lines 14-18" and the server knows exactly which
# slice of the markdown to hand back, and where to put the edit.
SRC_START = "data-src-start"
SRC_END = "data-src-end"

_FIRST_TAG = re.compile(r"<(\w+)")


def _tag_first_tag(html: str, span: list[int]) -> str:
    """Add the source range to the opening tag of an already-rendered block.

    `fence` and friends build their own markup instead of going through
    renderToken, so their attrs never reach the output; splice them in.
    """
    match = _FIRST_TAG.match(html.lstrip())
    if not match:
        return html
    attrs = f' {SRC_START}="{span[0]}" {SRC_END}="{span[1]}"'
    at = html.index(match.group(0)) + len(match.group(0))
    return html[:at] + attrs + html[at:]


class SourceMappedRenderer(RendererHTML):
    """RendererHTML that records where each top-level block came from."""

    def renderToken(self, tokens, idx, options, env):  # noqa: N802 -- upstream name
        token = tokens[idx]
        if token.level == 0 and token.nesting >= 0 and token.map:
            token.attrSet(SRC_START, str(token.map[0]))
            token.attrSet(SRC_END, str(token.map[1]))
        return super().renderToken(tokens, idx, options, env)

    def fence(self, tokens, idx, options, env):
        html = super().fence(tokens, idx, options, env)
        token = tokens[idx]
        return _tag_first_tag(html, token.map) if token.level == 0 and token.map else html

    def code_block(self, tokens, idx, options, env):
        html = super().code_block(tokens, idx, options, env)
        token = tokens[idx]
        return _tag_first_tag(html, token.map) if token.level == 0 and token.map else html


# The sidebar is written between these, so it can be replaced later without
# touching anything else on the page. The macro emits them itself, so the
# fragment and the region it replaces are the same bytes.
SIDEBAR_OPEN = "<!--mdweave:sidebar-->"
SIDEBAR_CLOSE = "<!--/mdweave:sidebar-->"


def _environment() -> Environment:
    # autoescape=True, not select_autoescape(): the latter keys off the file
    # extension, and ".html.j2" reads as ".j2" -- which would leave comment
    # bodies unescaped. The rendered markdown is the one trusted value, and it
    # is marked `| safe` in the template.
    return Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_sidebar(tree: list[Node], doc_id: str) -> str:
    """Just the navigation panel, for splicing into a page already written.

    Rebuilding the tree used to mean re-rendering every document: 26 markdown
    parses and 26 runs of Pygments to move one row. Nothing about the prose
    changes when the tree does, so this renders the one part that did.
    """
    module = _environment().get_template("sidebar.html.j2").module
    return str(module.sidebar(tree, relative_prefix(doc_id), doc_id))


def splice_sidebar(html: str, sidebar: str) -> str:
    """Replace a page's navigation, leaving every other byte alone.

    A page without the markers is left untouched rather than guessed at -- it
    was written by an older version, and a full render will replace it.
    """
    start = html.find(SIDEBAR_OPEN)
    end = html.find(SIDEBAR_CLOSE)
    if start == -1 or end == -1 or end < start:
        return html
    return html[:start] + sidebar.strip() + html[end + len(SIDEBAR_CLOSE) :]


@dataclass
class RenderResult:
    html: str
    placements: list[Placement]

    @property
    def unresolved(self) -> list[Placement]:
        return [p for p in self.placements if not p.resolved]


def build_parser() -> MarkdownIt:
    md = (
        MarkdownIt("commonmark",
                   {"html": True, "highlight": _highlight, "linkify": True},
                   renderer_cls=SourceMappedRenderer)
        # `linkify` turns a bare URL into a link. Notes are full of pasted
        # addresses, and wrapping each one in <> or [](…) by hand is work the
        # parser can do -- markdown-it only does it inside text, so a URL in a
        # code span or a fenced block is left exactly as written.
        #
        # Both halves are needed: the rule has to be enabled *and* the option
        # set, because the rule reads the option before doing anything.
        .enable(["table", "strikethrough", "linkify"])
        .use(footnote_plugin)
        .use(tasklists_plugin, enabled=True)
    )
    # A scheme is required. linkify's fuzzy matching turns anything ending in
    # a known TLD into a link, and `.md` is Moldova -- so "see README.md" came
    # out as a link to a domain, in a knowledge base whose prose is largely
    # about .md files. Bare emails go the same way for the same reason.
    #
    # The cost is that `www.example.org` is left alone too; there is no switch
    # for www on its own. Writing `https://` is the reader saying they meant
    # an address, which is a small thing to ask and never guesses wrong.
    md.linkify.set({"fuzzy_link": False, "fuzzy_email": False})
    return md


def _highlight(code: str, lang: str, _attrs: str) -> str:
    """Pygments highlighter wired into markdown-it.

    Returning "" tells markdown-it to fall back to its own escaping, which is
    what we want when the language is unknown.
    """
    try:
        lexer = get_lexer_by_name(lang, stripall=False) if lang else guess_lexer(code)
    except (ClassNotFound, ValueError):
        return ""
    formatter = HtmlFormatter(nowrap=False, cssclass="codehilite")
    return pygments_highlight(code, lexer, formatter)


def render_markdown(markdown: str) -> str:
    return build_parser().render(markdown)


def render_document(
    markdown: str,
    annotations: list[Annotation],
    *,
    title: str | None = None,
    doc_id: str | None = None,
    tree: list[Node] | None = None,
    css_hrefs: tuple[str, ...] = ("assets/mdweave.css", "assets/pygments.css"),
    js_srcs: tuple[str, ...] = (
        "assets/ui.js",
        "assets/sidebar.js",
        "assets/filetree.js",
        "assets/notes.js",
        "assets/annotate.js",
        "assets/edit.js",
        "assets/copy.js",
        "assets/refresh.js",
        "assets/checkpoint.js",
        "assets/agent.js",
        "assets/settings.js",
    ),
) -> RenderResult:
    """Render markdown to a full HTML page with highlights and note cards."""
    body_html = render_markdown(markdown)
    soup = BeautifulSoup(body_html, "html.parser")

    placements = inject(soup, annotations)
    _add_heading_ids(soup)

    placed = [p.annotation for p in placements if p.resolved]
    notes = [_note_context(a) for a in placed if a.has_card]

    env = _environment()
    # A page nested under a folder has to climb back out to reach the shared
    # assets and its siblings' pages.
    prefix = relative_prefix(doc_id or "")

    # A document with nothing but its heading gets somewhere to start. The
    # only other way in is to open the heading itself and type past it, which
    # nobody guesses -- and now that editing needs a double click, even less
    # so. The line number is the end of the file, which `replace_block` reads
    # as an append rather than a replacement.
    blocks = soup.find_all(attrs={"data-src-start": True}, recursive=False)
    template = env.get_template("document.html.j2")
    html = template.render(
        title=title or _infer_title(soup) or "Document",
        doc_id=doc_id or "",
        body=str(soup),
        start_at=len(markdown.splitlines()) if len(blocks) <= 1 else None,
        notes=notes,
        tree=tree or [],
        prefix=prefix,
        css_hrefs=[prefix + href for href in css_hrefs],
        js_srcs=[prefix + src for src in js_srcs],
    )
    return RenderResult(html=html, placements=placements)


def _note_context(ann: Annotation) -> dict:
    return {
        "id": ann.id,
        "dom_id": safe_id(ann.id),
        "color_token": ann.color_token,
        "custom_color": ann.custom_color,
        "status": ann.status,
        "tags": ann.tags,
        "offset": ann.offset.to_dict() if ann.offset else None,
        "quote": ann.target.quote,
        "author": ann.thread[0].author if ann.thread else "",
        "initial": (ann.thread[0].author[:1].upper() if ann.thread else "•"),
        "thread": [
            {"author": c.author, "at": _short_date(c.at), "body": c.body}
            for c in ann.thread
        ],
    }


def _short_date(value: str | None) -> str:
    if not value:
        return ""
    return value[:10]  # ISO timestamp -> YYYY-MM-DD


_SLUG_STRIP = re.compile(r"[^a-z0-9\s-]")


def _add_heading_ids(soup: BeautifulSoup) -> None:
    """Give headings stable ids so sections are directly linkable."""
    used: set[str] = set()
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if heading.get("id"):
            used.add(heading["id"])
            continue
        base = _SLUG_STRIP.sub("", heading.get_text().lower()).strip()
        base = re.sub(r"[\s-]+", "-", base) or "section"
        slug, n = base, 2
        while slug in used:
            slug, n = f"{base}-{n}", n + 1
        heading["id"] = slug
        used.add(slug)


def _infer_title(soup: BeautifulSoup) -> str | None:
    h1 = soup.find("h1")
    return h1.get_text().strip() if h1 else None


def write_assets(outdir: Path, scheme) -> None:
    """Copy the stylesheet, syntax theme, and note script next to the HTML.

    The active scheme is generated onto the end of the stylesheet rather than
    linked as a second file: last wins, so the static themes keep every rule
    about structure and own none of the colour, and switching schemes rewrites
    one file instead of every page.

    `scheme` is required, and used to be optional with the shipped palette as
    its default. That made forgetting it silent: `mdweave build` did, so every
    boot wrote the default colours over the reader's scheme and the page stayed
    that way until the server happened to rebuild for some other reason. Two
    tabs on one URL came out different colours. Pass `scheme.load(inputs).
    current()` -- there is no sensible default, because the answer lives beside
    the documents and this function is not given them.
    """
    from . import scheme as schemes

    assets = outdir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    css = "\n".join(
        (THEME / name).read_text(encoding="utf-8")
        for name in ("base.css", "sidebar.css", "annotations.css", "editor.css")
    )
    css += "\n\n" + schemes.css(scheme)
    (assets / "mdweave.css").write_text(css, encoding="utf-8")

    pygments_css = HtmlFormatter(style=PYGMENTS_STYLE).get_style_defs(".codehilite")
    (assets / "pygments.css").write_text(
        f"/* Generated by Pygments, style: {PYGMENTS_STYLE} */\n{pygments_css}\n",
        encoding="utf-8",
    )

    scripts = (
        "ui.js", "sidebar.js", "filetree.js", "notes.js", "annotate.js",
        "edit.js", "copy.js", "refresh.js", "checkpoint.js", "agent.js",
        "settings.js",
    )
    for script in scripts:
        (assets / script).write_text(
            (ASSETS / script).read_text(encoding="utf-8"), encoding="utf-8"
        )
