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


@dataclass
class RenderResult:
    html: str
    placements: list[Placement]

    @property
    def unresolved(self) -> list[Placement]:
        return [p for p in self.placements if not p.resolved]


def build_parser() -> MarkdownIt:
    md = (
        MarkdownIt("commonmark", {"html": True, "highlight": _highlight},
                   renderer_cls=SourceMappedRenderer)
        .enable(["table", "strikethrough"])
        .use(footnote_plugin)
        .use(tasklists_plugin, enabled=True)
    )
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
        "assets/notes.js",
        "assets/annotate.js",
        "assets/edit.js",
        "assets/copy.js",
        "assets/refresh.js",
        "assets/checkpoint.js",
    ),
) -> RenderResult:
    """Render markdown to a full HTML page with highlights and note cards."""
    body_html = render_markdown(markdown)
    soup = BeautifulSoup(body_html, "html.parser")

    placements = inject(soup, annotations)
    _add_heading_ids(soup)

    placed = [p.annotation for p in placements if p.resolved]
    notes = [_note_context(a) for a in placed if a.has_card]

    # autoescape=True, not select_autoescape(): the latter keys off the file
    # extension, and ".html.j2" reads as ".j2" -- which would leave comment
    # bodies unescaped. The rendered markdown is the one trusted value, and it
    # is marked `| safe` in the template.
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # A page nested under a folder has to climb back out to reach the shared
    # assets and its siblings' pages.
    prefix = relative_prefix(doc_id or "")

    template = env.get_template("document.html.j2")
    html = template.render(
        title=title or _infer_title(soup) or "Document",
        doc_id=doc_id or "",
        body=str(soup),
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


def write_assets(outdir: Path) -> None:
    """Copy the stylesheet, syntax theme, and note script next to the HTML."""
    assets = outdir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    css = "\n".join(
        (THEME / name).read_text(encoding="utf-8")
        for name in ("base.css", "sidebar.css", "annotations.css", "editor.css")
    )
    (assets / "mdweave.css").write_text(css, encoding="utf-8")

    pygments_css = HtmlFormatter(style=PYGMENTS_STYLE).get_style_defs(".codehilite")
    (assets / "pygments.css").write_text(
        f"/* Generated by Pygments, style: {PYGMENTS_STYLE} */\n{pygments_css}\n",
        encoding="utf-8",
    )

    scripts = (
        "ui.js", "sidebar.js", "notes.js", "annotate.js",
        "edit.js", "copy.js", "refresh.js", "checkpoint.js",
    )
    for script in scripts:
        (assets / script).write_text(
            (ASSETS / script).read_text(encoding="utf-8"), encoding="utf-8"
        )
