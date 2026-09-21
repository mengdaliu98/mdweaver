"""Importing a Google Doc as a document in the knowledge base.

A snapshot, not a link. The doc is fetched once, converted to markdown, and
from then on it is an ordinary document here: it renders to a static page, it
takes highlights and comments, and its prose can be edited. Nothing goes back
to Google, and the Google Doc carries on without knowing about any of it.

Markdown rather than the HTML export, even though the export is higher
fidelity. Everything mdweave does to a document it does through the markdown
-- a block edit rewrites a source range, a cut maps visible characters back to
what produced them -- so an imported document stored as HTML would render and
annotate but could not be edited. Fidelity that costs editing is the wrong
trade for a knowledge base you write in.

The transport is Meta's `meta google.docs` CLI, which already holds the
reader's credentials. That makes this the one feature here that does not work
everywhere: a container has no `meta`, so `available()` says so and the
browser hides the option rather than offering something that will fail.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

# Reading a long document goes through several Google APIs; 90s is slow enough
# to be safe and short enough that a wedged call does not hold a request open.
TIMEOUT = 90

SOURCES_FILE = ".mdweave-sources.json"

# Both the /d/<id>/ form and a bare id, because people paste either.
_DOC_URL = re.compile(r"/document/(?:u/\d+/)?d/([A-Za-z0-9_-]{16,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{16,}$")


class GDocError(RuntimeError):
    """Fetching the document failed; the message is meant for the reader."""


@dataclass(frozen=True)
class Snapshot:
    """One import: the markdown, and what it was taken from."""

    doc_id: str          # the Google one
    title: str
    markdown: str
    revision: str        # modifiedTime -- see `_revision`
    url: str


def available() -> bool:
    """Whether this machine can reach Google Docs at all."""
    return shutil.which("meta") is not None


def doc_id_from(url: str) -> str:
    """The document id inside a Google Docs URL, or the id itself."""
    text = (url or "").strip()
    if not text:
        raise GDocError("paste a Google Doc URL")

    found = _DOC_URL.search(text)
    if found:
        return found.group(1)
    if _BARE_ID.match(text):
        return text
    raise GDocError(f"{text!r} does not look like a Google Doc URL")


def _run(*args: str) -> str:
    if not available():
        raise GDocError(
            "the `meta` CLI is not on this machine, so Google Docs cannot be read here"
        )
    try:
        done = subprocess.run(
            ["meta", "google.docs", *args],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GDocError(f"Google Docs did not answer within {TIMEOUT}s")

    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise GDocError(detail[-1] if detail else "the meta CLI failed")
    return done.stdout


def _revision(meta: dict) -> str:
    """What "which revision" means here.

    `google.docs revisions` is the obvious answer and is not a reliable one --
    it returns nothing at all for plenty of documents, including the first one
    this was tried against. `modifiedTime` is always present, changes whenever
    the document does, and is what Drive itself compares. It identifies the
    state that was imported, which is the question being asked.
    """
    return str(meta.get("modifiedTime") or "").strip() or "unknown"


def fetch(url: str) -> Snapshot:
    """Read a Google Doc as markdown, with enough to say which state it was."""
    doc_id = doc_id_from(url)

    try:
        meta = json.loads(_run("describe", f"--id={doc_id}", "--output=json"))
    except json.JSONDecodeError:
        raise GDocError("could not read the document's details")
    if not isinstance(meta, dict) or meta.get("status") == "error":
        raise GDocError(str((meta or {}).get("message", "no such document")))

    # Images are stripped: they arrive as data URIs, which would put megabytes
    # of base64 into a file meant to be read and edited as text.
    markdown = _run(
        "get", f"--id={doc_id}", "--output=markdown", "--images=strip"
    ).strip()
    if not markdown:
        raise GDocError("the document is empty")

    title = str(meta.get("name") or "Untitled").strip()
    return Snapshot(
        doc_id=doc_id,
        title=title,
        markdown=_with_heading(markdown, title),
        revision=_revision(meta),
        url=str(meta.get("url") or url),
    )


def _with_heading(markdown: str, title: str) -> str:
    """Make sure the document starts with its own title.

    The export leads with the title as a bare line rather than a heading, so
    without this every imported document renders with an unstyled first line
    and no `<h1>` -- which is also what the page's `<title>` is inferred from.
    """
    lines = markdown.splitlines()
    first = next((l for l in lines if l.strip()), "")
    if first.startswith("#"):
        return markdown
    if first.strip() == title:
        at = lines.index(first)
        lines[at] = f"# {title}"
        return "\n".join(lines)
    return f"# {title}\n\n{markdown}"


# A document title is prose and can be a sentence long; a filename is a URL
# and a path. The label carries the real title now, so the file does not have
# to -- it only has to be short, recognisable and easy to type.
FILENAME_CHARS = 60


def filename_for(title: str) -> str:
    """A short, plain name for the file. The label keeps the whole title."""
    # Punctuation a Google Doc title is full of and a filename should not be:
    # em dashes, smart quotes, colons. Not stripped as unsafe -- they are
    # legal in a filename -- just unwelcome in a URL you might type.
    plain = re.sub(r"[\u2010-\u2015\u2018\u2019\u201c\u201d:/|]+", " ", title)
    plain = " ".join(plain.split())
    if len(plain) <= FILENAME_CHARS:
        return plain or "Untitled"

    cut = plain[:FILENAME_CHARS]
    # At a word boundary, so the name reads rather than stopping mid-word.
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut or "Untitled"


def record(snapshot: Snapshot, doc_id: str) -> dict:
    """The dictionary entry for an import, keyed by *this* document's id."""
    return {
        "kind": "gdoc",
        "google_id": snapshot.doc_id,
        "url": snapshot.url,
        "title": snapshot.title,
        "revision": snapshot.revision,
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
