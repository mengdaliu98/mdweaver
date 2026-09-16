"""Local editing server.

Serves the generated HTML and accepts new annotations from the browser. A
static page opened over `file://` stays perfectly readable -- it just has no
API to write to, and `annotate.js` degrades to read-only.

Bound to loopback only. There is no authentication, so do not expose it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import random
import shutil
import string
import sys
import time
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import checkpoint as git_checkpoint
from . import scheme as schemes
from . import edits
from .agent import protocol
from .autosave import DEFAULT_DELAY, AutoCommit
from .model import COLOR_TOKENS, DEFAULT_COLOR, Annotation, Comment, Offset, TextTarget
from .render import (
    SIDEBAR_OPEN,
    render_document,
    render_sidebar,
    splice_sidebar,
    write_assets,
)
from .sources import obsidian_inline, session as session_store, sidecar
from .tree import (
    build_tree,
    document_ids,
    folder_paths,
    humanize,
    load_labels,
    save_labels,
    load_order,
    safe_document_name,
    safe_folder_name,
    save_order,
)

ID_ALPHABET = string.ascii_lowercase + string.digits
ID_LENGTH = 5
MAX_BODY_BYTES = 256 * 1024
# An imported document is a whole file, so it needs far more headroom than an
# annotation payload.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
# How much of an over-sized body is worth reading just to answer politely.
DRAIN_LIMIT = 64 * 1024 * 1024

# POSTs that only read. Everything else that succeeds has changed something.
READ_ONLY_POSTS = {"/api/extract"}


def is_runner_route(path: str) -> bool:
    """Is this one of the endpoints the devserver's runner calls?

    These take no credential, which makes this list the one place that decides
    what is reachable without a password. It is defined once and used twice --
    by `do_POST` to skip the gate and by `_api_agent` to route -- because two
    lists meant to agree are exactly how a route ends up gated by neither.
    """
    if path in ("/api/agent/claim", "/api/agent/reconcile"):
        return True
    return path.startswith("/api/agent/jobs/") and path.rsplit("/", 1)[-1] in (
        "events",
        "done",
    )


def _writes_something(path: str) -> bool:
    """Should this request arm the auto-commit timer?

    The bridge's own traffic must not. A runner polls for work every twenty-odd
    seconds forever, and its progress reports are chatter about a job, not
    edits to a document -- counting either as a write would leave the container
    committing on a timer for as long as the daemon was connected, whether or
    not anybody had written a word.
    """
    if path in READ_ONLY_POSTS:
        return False
    if path.startswith("/api/agent/"):
        # `reconcile` is the exception: it pulls in real prose and re-renders
        # over it, and the rebuilt pages are worth committing.
        return path == "/api/agent/reconcile"
    return True

# A dragged note may not be flung arbitrarily far from its anchor.
OFFSET_LIMIT = 4000

# Files whose contents define "the version of mdweave this process is running".
SOURCE_SUFFIXES = {".py", ".js", ".css", ".j2"}

# How long a browser's event stream is held before it is closed on purpose.
# Railway caps any request at fifteen minutes; ending it ourselves a little
# short of that turns a severed connection into an ordinary reconnect.
STREAM_LIFETIME = 12 * 60


def source_fingerprint() -> str:
    """A hash of the package source.

    A long-lived server keeps serving whatever code it imported at startup, so
    editing a source file has no effect until it is restarted -- which is easy
    to forget and produces baffling errors (a new endpoint answering 501). The
    launcher compares this against a freshly computed one and restarts on a
    mismatch.
    """
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in SOURCE_SUFFIXES:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


# Computed once, at import: it describes the code this process actually loaded,
# not whatever is on disk now.
RUNNING_FINGERPRINT = source_fingerprint()


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Workspace:
    """The pair of directories the server reads and writes."""

    inputs: Path
    outputs: Path

    def documents(self) -> dict[str, Path]:
        """Every document under the markdown root, keyed by id."""
        return document_ids(self.inputs)

    # --- colour schemes ----------------------------------------------------

    def theme(self) -> schemes.Theme:
        """The reader's schemes, read fresh: another machine may have pushed."""
        return schemes.load(self.inputs)

    def write_theme(self, theme: schemes.Theme) -> None:
        schemes.save(self.inputs, theme)

    def label_of(self, key: str, fallback_name: str) -> str:
        """What a row is called: the reader's word for it, or the heuristic."""
        return load_labels(self.inputs).get(key) or humanize(fallback_name)

    def set_label(self, key: str, label: str) -> str:
        """Record what a row should be called, or forget a previous answer.

        Stored literally. The whole reason this exists is that `humanize` has
        no way to know when punctuation mattered -- Ome-Zarr, a date in a
        filename -- so whatever is typed is kept exactly, including the case
        and the hyphens the heuristic would have eaten.

        A label equal to what the heuristic would produce is removed rather
        than written, so the file stays a list of exceptions and a renamed
        file goes back to following the rule.
        """
        label = " ".join(label.split())
        if not label:
            raise ApiError(HTTPStatus.BAD_REQUEST, "a name cannot be empty")

        leaf = key.rpartition("/")[2]
        labels = load_labels(self.inputs)
        if label == humanize(leaf):
            labels.pop(key, None)
        else:
            labels[key] = label
        save_labels(self.inputs, labels)
        return label

    def move_labels(self, old: str, new: str) -> None:
        """Carry labels across a move, for the row and everything under it."""
        labels = load_labels(self.inputs)
        moved = {}
        for key, value in labels.items():
            if key == old:
                moved[new] = value
            elif key.startswith(f"{old}/"):
                moved[new + key[len(old):]] = value
            else:
                moved[key] = value
        if moved != labels:
            save_labels(self.inputs, moved)

    def forget_label(self, key: str) -> None:
        labels = load_labels(self.inputs)
        gone = {k: v for k, v in labels.items()
                if k != key and not k.startswith(f"{key}/")}
        if gone != labels:
            save_labels(self.inputs, gone)

    def remap_slots(self, moved: list[int]) -> int:
        """Renumber every annotation so a reordered scheme looks unchanged.

        `moved[i]` is the slot whose colour now sits at position i+1. Moving
        the colours without moving the annotations would repaint the whole
        knowledge base, which is the opposite of what dragging a swatch means:
        the reader is arranging the palette, not restyling their notes.

        Only ever called for the scheme in effect. Reordering one that is not
        active must leave annotations alone -- they are expressed in the active
        scheme's positions, and renumbering them would be visible immediately.
        """
        onto = {old: new for new, old in enumerate(moved, start=1)}
        touched = 0
        for doc_id, markdown in self.documents().items():
            path = sidecar.sidecar_path(markdown)
            annotations = sidecar.load(path)
            changed = False
            for ann in annotations:
                slot = ann.slot
                if slot is not None and onto.get(slot, slot) != slot:
                    ann.color = onto[slot]
                    changed = True
            if changed:
                sidecar.save(path, annotations)
                touched += 1
        return touched

    def markdown_for(self, doc_id: str) -> Path:
        """Resolve a document id, rejecting anything not actually present.

        Membership in this dict is the path-traversal guard: `doc_id` is never
        joined onto a path, only looked up.
        """
        try:
            return self.documents()[doc_id]
        except KeyError:
            raise ApiError(HTTPStatus.NOT_FOUND, f"unknown document: {doc_id!r}")

    def folders(self) -> dict[str, Path]:
        """Every folder under the markdown root, keyed by path; `""` is the root.

        The counterpart of `documents()` for the other half of a move. Same
        guard, for the same reason: a client names a folder that is already
        there, rather than handing over a string to be joined onto a path.
        """
        found: dict[str, Path] = {"": self.inputs}
        # One discovery rule, shared with the tree: a folder that is drawn must
        # be one a document can be dropped into, and the other way round.
        for folder in folder_paths(self.inputs):
            found[folder] = self.inputs / folder
        return found

    def folder_for(self, folder: str) -> Path:
        try:
            return self.folders()[folder.replace("\\", "/").strip("/")]
        except KeyError:
            raise ApiError(HTTPStatus.NOT_FOUND, f"unknown folder: {folder!r}")

    def children_of(self, folder: str) -> set[str]:
        """The names the sidebar draws directly inside one folder.

        Folders as well as documents, and the folders have to be asked for
        separately: an empty one holds no document, so deriving the list from
        document ids alone leaves it out. `set_order` then drops it as a name
        the sidebar does not draw -- and dragging an empty folder up or down
        did nothing that survived a reload, while a folder with a document in
        it moved perfectly well. `build_tree` takes `folders` for the same
        reason; this is the other half of it.
        """
        prefix = f"{folder}/" if folder else ""
        names = {
            doc_id[len(prefix) :].split("/", 1)[0]
            for doc_id in self.documents()
            if doc_id.startswith(prefix)
        }
        names.update(
            path[len(prefix) :].split("/", 1)[0]
            for path in self.folders()
            if path and path.startswith(prefix)
        )
        return names

    def annotations_for(self, doc_id: str) -> list[Annotation]:
        md = self.markdown_for(doc_id)
        annotations = sidecar.load(sidecar.sidecar_path(md))
        known = {a.id for a in annotations}
        _, inline = obsidian_inline.extract(md.read_text(encoding="utf-8"))
        annotations.extend(a for a in inline if a.id not in known)
        return annotations

    def render(self, doc_id: str, annotations: list[Annotation], tree=None):
        """Render one document exactly as `mdweave build` would."""
        md = self.markdown_for(doc_id)
        cleaned, _ = obsidian_inline.extract(md.read_text(encoding="utf-8"))
        return render_document(
            cleaned,
            annotations,
            title=humanize(md.stem),
            doc_id=doc_id,
            tree=tree if tree is not None else self.tree(),
        )

    def tree(self):
        """The sidebar, arranged the way the reader last dragged it.

        Folders are passed explicitly, not inferred from the document ids: an
        empty one has no documents to infer it from, and a folder you cannot
        see is a folder you cannot drop anything into.
        """
        return build_tree(
            list(self.documents()),
            load_order(self.inputs),
            [f for f in self.folders() if f],
            load_labels(self.inputs),
        )

    def write_html(self, doc_id: str, annotations: list[Annotation], tree=None):
        result = self.render(doc_id, annotations, tree=tree)
        out = self.outputs / f"{doc_id}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.html, encoding="utf-8")
        return result

    def save(self, doc_id: str, annotations: list[Annotation]) -> None:
        """Persist the sidecar and regenerate the HTML so a reload matches."""
        sidecar.save(sidecar.sidecar_path(self.markdown_for(doc_id)), annotations)
        self.write_html(doc_id, annotations)

    def rebuild_all(self) -> None:
        """Re-render every document.

        Each page bakes its own copy of the sidebar, so adding a document makes
        every other page's navigation stale. Cheap at this scale, and it is what
        lets an import appear without restarting the server.
        """
        write_assets(self.outputs, self.theme().current())
        documents = self.documents()
        tree = self.tree()
        for doc_id in documents:
            self.write_html(doc_id, self.annotations_for(doc_id), tree=tree)

    def sidebar_for(self, doc_id: str) -> str | None:
        """The freshly rendered navigation for one page, or None if it is gone.

        Handed back after a tree change so the browser can swap the panel in
        place. A full reload would re-fetch the page, re-run every script and
        re-lay out the notes to move one row.
        """
        if doc_id not in self.documents():
            return None
        return render_sidebar(self.tree(), doc_id)

    def rebuild_tree(self, render_ids: set[str] = frozenset()) -> None:
        """Refresh the navigation everywhere, re-rendering as little as possible.

        A change to the tree -- a new document, a rename, a drag -- alters the
        sidebar on every page and none of their prose. `rebuild_all` re-parsed
        all of it anyway, which cost two seconds to move one row. Only the
        documents named in `render_ids` are rendered in full; the rest have the
        one region that changed spliced in.

        A page that is missing, or was written before the markers existed, falls
        back to a full render rather than being left stale.
        """
        tree = self.tree()
        for doc_id in self.documents():
            page = self.outputs / f"{doc_id}.html"
            html = page.read_text(encoding="utf-8") if page.exists() else ""

            # Look for the markers rather than comparing the result: a splice
            # that changes nothing is the common case -- most pages' navigation
            # is identical after a reorder -- and treating that as "no markers"
            # sent every one of them through a full render.
            if doc_id not in render_ids and SIDEBAR_OPEN in html:
                spliced = splice_sidebar(html, render_sidebar(tree, doc_id))
                if spliced != html:
                    page.write_text(spliced, encoding="utf-8")
                continue

            self.write_html(doc_id, self.annotations_for(doc_id), tree=tree)

    def add_document(
        self, raw_name: str, content: str, replace: bool, folder: str = ""
    ) -> str:
        """Write an imported markdown file and return its document id."""
        directory = self.folder_for(folder)
        try:
            name = safe_document_name(raw_name)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc))

        target = directory / name
        if target.exists() and not replace:
            raise ApiError(
                HTTPStatus.CONFLICT, f"a document called {name!r} already exists"
            )

        directory.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self._id_of(target)

    # --- rearranging the tree ---------------------------------------------

    def _id_of(self, path: Path) -> str:
        return path.relative_to(self.inputs).with_suffix("").as_posix()

    def destination(self, target: str) -> tuple[str, Path]:
        """Split a proposed document id into a folder that exists and a safe name.

        Both halves are guarded, and differently. The folder is looked up in
        `folders()` and never joined, so `../escape` asks for a folder called
        `..` and gets a 404. The leaf goes through `safe_document_name`, which
        throws away any path it is given. Neither half can name a file outside
        the markdown root, which is the property every write here depends on.
        """
        cleaned = target.replace("\\", "/").strip("/")
        folder, _, leaf = cleaned.rpartition("/")
        directory = self.folder_for(folder)

        # The client sends an id, which has no suffix -- but tolerate one, so
        # that a rename typed as "weekly.md" does not become "weekly.md.md".
        if not leaf.lower().endswith(".md"):
            leaf += ".md"
        try:
            name = safe_document_name(leaf)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc))

        stem = name[: -len(".md")]
        return (f"{folder}/{stem}" if folder else stem), directory / name

    def create_document(self, target: str) -> str:
        """Make a new document, and return its id."""
        doc_id, path = self.destination(target)
        if path.exists():
            raise ApiError(
                HTTPStatus.CONFLICT, f"a document called {doc_id!r} already exists"
            )

        # Not literally empty: a file with no blocks in it renders a page with
        # nothing to click, and clicking a block is the only way to edit one.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {humanize(path.stem)}\n", encoding="utf-8")
        return doc_id

    def move_document(self, doc_id: str, target: str) -> str:
        """Move or rename a document, and return the id it now has.

        A rename is a move to the same folder -- the id is the path, so there
        is no second operation to write.
        """
        source = self.markdown_for(doc_id)
        new_id, path = self.destination(target)
        if new_id == doc_id:
            return doc_id
        if path.exists():
            raise ApiError(
                HTTPStatus.CONFLICT, f"a document called {new_id!r} already exists"
            )

        # Both sidecars' names are derived from the markdown's, so leaving
        # either behind would silently orphan it -- the annotations on the
        # document, and the Claude session that has been co-writing it.
        beside = sidecar.sidecar_path(source)
        owning = session_store.session_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        source.rename(path)
        if beside.exists():
            beside.rename(sidecar.sidecar_path(path))
        if owning.exists():
            owning.rename(session_store.session_path(path))

        # The generated page is named after the old id, which no document
        # claims any more; `rebuild_all` writes the new one but has no reason
        # to go looking for the file it replaced.
        (self.outputs / f"{doc_id}.html").unlink(missing_ok=True)

        self._reindex(doc_id, new_id)
        self.move_labels(doc_id, new_id)
        return new_id

    def delete_document(self, doc_id: str) -> None:
        """Remove a document, its annotations, and the page built from them."""
        markdown = self.markdown_for(doc_id)
        beside = sidecar.sidecar_path(markdown)
        owning = session_store.session_path(markdown)

        markdown.unlink()
        beside.unlink(missing_ok=True)
        owning.unlink(missing_ok=True)
        (self.outputs / f"{doc_id}.html").unlink(missing_ok=True)

        self._reindex(doc_id, None)
        self.forget_label(doc_id)

    # --- folders ----------------------------------------------------------

    def folder_destination(self, target: str) -> tuple[str, Path]:
        """Split a proposed folder path into a parent that exists and a safe leaf.

        Guarded exactly as `destination` is, and for the same reason: the
        parent is looked up in `folders()` and never joined, the leaf goes
        through `safe_folder_name`, and so neither half can name a directory
        outside the markdown root.
        """
        cleaned = target.replace("\\", "/").strip("/")
        parent, _, leaf = cleaned.rpartition("/")
        directory = self.folder_for(parent)
        try:
            name = safe_folder_name(leaf)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc))
        return (f"{parent}/{name}" if parent else name), directory / name

    def create_folder(self, target: str) -> str:
        """Make an empty folder, and return its path.

        The one operation the tree could not do without. Everything else here
        moves documents *into* folders, which is unreachable while there is no
        way to get a first one.
        """
        path_id, path = self.folder_destination(target)
        if path.exists():
            raise ApiError(HTTPStatus.CONFLICT, f"{path_id!r} already exists")
        path.mkdir(parents=True)
        return path_id

    def rename_folder(self, folder: str, target: str) -> str:
        """Rename a folder, carrying every document under it to the new path."""
        source = self.folder_for(folder)
        if not folder:
            raise ApiError(HTTPStatus.BAD_REQUEST, "the root cannot be renamed")

        new_id, path = self.folder_destination(target)
        if new_id == folder:
            return folder
        if path.exists():
            raise ApiError(HTTPStatus.CONFLICT, f"{new_id!r} already exists")
        if f"{new_id}/".startswith(f"{folder}/"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "a folder cannot move inside itself")

        # Every id beneath this folder changes, so every page built from one is
        # named after something that no longer exists.
        moving = [d for d in self.documents() if d.startswith(f"{folder}/")]
        path.parent.mkdir(parents=True, exist_ok=True)
        source.rename(path)
        for doc_id in moving:
            (self.outputs / f"{doc_id}.html").unlink(missing_ok=True)

        self._reindex_folder(folder, new_id)
        self.move_labels(folder, new_id)
        return new_id

    def delete_folder(self, folder: str, recursive: bool) -> int:
        """Remove a folder. Returns how many documents went with it.

        A non-empty folder needs `recursive`, so a mis-aimed drop or a stray
        request cannot take a subtree with it.
        """
        directory = self.folder_for(folder)
        if not folder:
            raise ApiError(HTTPStatus.BAD_REQUEST, "the root cannot be deleted")

        inside = [d for d in self.documents() if d.startswith(f"{folder}/")]
        if inside and not recursive:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"{folder!r} still holds {len(inside)} document(s); "
                "pass recursive to remove them too",
            )

        for doc_id in inside:
            (self.outputs / f"{doc_id}.html").unlink(missing_ok=True)
        shutil.rmtree(directory)

        self._reindex_folder(folder, None)
        return len(inside)

    def _reindex_folder(self, old: str, new: str | None) -> None:
        """Follow a folder move or delete through the hand-arranged order.

        Two things move: the folder's own name in its parent's list, and every
        order key recorded for it or anything beneath it.
        """
        order = load_order(self.inputs)

        parent, _, name = old.rpartition("/")
        listed = order.get(parent)
        if listed and name in listed:
            at = listed.index(name)
            listed.pop(at)
            if new is not None:
                listed.insert(at, new.rpartition("/")[2])
            order[parent] = listed

        for key in [k for k in order if k == old or k.startswith(f"{old}/")]:
            names = order.pop(key)
            if new is not None:
                order[new + key[len(old) :]] = names

        save_order(self.inputs, order)

    def set_order(self, folder: str, names: list[str]) -> list[str]:
        """Remember the order of one folder's children."""
        self.folder_for(folder)  # 404s a folder that is not there
        children = self.children_of(folder)

        # Only names the sidebar actually draws. A stale entry is harmless --
        # `_sort` ignores what it cannot find -- but the file is read by people
        # too, and letting it accumulate ghosts makes it unreadable.
        kept = [name for name in names if name in children]

        order = load_order(self.inputs)
        if kept:
            order[folder] = kept
        else:
            order.pop(folder, None)
        save_order(self.inputs, order)
        return kept

    def _reindex(self, old_id: str, new_id: str | None) -> None:
        """Follow a move or a delete through the hand-arranged order."""
        old_folder, _, old_name = old_id.rpartition("/")
        order = load_order(self.inputs)
        listed = order.get(old_folder)
        if not listed or old_name not in listed:
            return

        at = listed.index(old_name)
        listed.pop(at)
        if new_id is not None:
            new_folder, _, new_name = new_id.rpartition("/")
            # A rename keeps its place. Dropping it back among the unranked
            # would send a row to the bottom of the folder for the crime of
            # being given a better name.
            if new_folder == old_folder:
                listed.insert(at, new_name)

        order[old_folder] = listed
        save_order(self.inputs, order)

    # --- editing the prose ------------------------------------------------

    def source_of(self, doc_id: str) -> str:
        return self.markdown_for(doc_id).read_text(encoding="utf-8")

    def block_of(self, doc_id: str, line: int, end: int) -> str:
        """The markdown behind one rendered block."""
        try:
            return edits.block_source(self.source_of(doc_id), line, end)
        except IndexError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc))

    def write_source(self, doc_id: str, markdown: str) -> None:
        """Replace a document's markdown and re-render its page.

        Only this page. A sidebar label comes from the *filename*, so no edit
        to the prose can change any other page -- the whole set used to be
        rebuilt on the belief that a heading edit moved the label, which it
        never did.
        """
        path = self.markdown_for(doc_id)
        path.write_text(markdown, encoding="utf-8")
        self.write_html(doc_id, self.annotations_for(doc_id))

    def body_of(self, doc_id: str) -> tuple[str, str]:
        """The rendered document and its notes, without the page around them.

        What the browser swaps into `<article class="doc">` after an edit --
        cheaper and far less jarring than reloading the page.
        """
        from bs4 import BeautifulSoup

        result = self.render(doc_id, self.annotations_for(doc_id))
        soup = BeautifulSoup(result.html, "html.parser")
        article = soup.find("article", class_="doc")
        layer = soup.find("aside", class_="notes-layer")
        return (
            article.decode_contents() if article else "",
            layer.decode_contents() if layer else "",
        )

    def files_of(self, doc_id: str) -> list[Path]:
        """Everything that belongs to one document.

        The prose, the annotations beside it, the Claude session co-writing it,
        and the page built from them -- but not `assets/`, which is shared and
        would drag every other document's rebuild into a commit meant for this
        one.
        """
        markdown = self.markdown_for(doc_id)
        return [
            markdown,
            sidecar.sidecar_path(markdown),
            session_store.session_path(markdown),
            self.outputs / f"{doc_id}.html",
        ]

    def session_of(self, doc_id: str) -> session_store.Session:
        return session_store.load(
            session_store.session_path(self.markdown_for(doc_id))
        )

    # --- highlighting over what is already there --------------------------

    def spans_in(self, doc_id: str):
        """Every annotation that anchors, with where it lands, and the index.

        Flattened coordinates, the same ones the anchor machinery uses, so two
        annotations can be compared for overlap without going near the DOM.
        The index comes back too: the caller needs it to place the selection
        on the same ruler.
        """
        from bs4 import BeautifulSoup

        from .anchors import TextIndex
        from .render import render_markdown

        markdown = self.markdown_for(doc_id).read_text(encoding="utf-8")
        cleaned, _ = obsidian_inline.extract(markdown)
        index = TextIndex(BeautifulSoup(render_markdown(cleaned), "html.parser"))

        found = []
        for annotation in self.annotations_for(doc_id):
            where = index.find(annotation.target)
            if where is not None:
                found.append((annotation, where))
        return found, index

    def apply_highlight(self, doc_id: str, target: TextTarget, color: int) -> dict:
        """Paint a selection. One rule, no exceptions.

        Picking a colour means "make the whole selection this colour",
        whatever is underneath -- so re-colouring part of a highlight, or a
        patch of mixed colours, both end up as one clean highlight.

        It used to also *clear*, when the selection was already entirely and
        only the colour pressed. That made a press ambiguous: the same gesture
        painted or erased depending on state the reader could not reliably
        see, and pressing a colour by accident on already-coloured text took
        the highlight off. Erasing is its own button now -- `erase_highlight`.
        """
        placed, index = self.spans_in(doc_id)
        span = index.find(target)
        if span is None:
            raise ApiError(
                HTTPStatus.CONFLICT, f"could not anchor the selection: {target.quote!r}"
            )

        touching = [
            (a, where) for a, where in placed
            if where[0] < span[1] and span[0] < where[1]
        ]

        # A comment's highlight is the handle for a thread. Absorbing it would
        # delete the conversation, which no colour press should ever mean.
        commented = [a for a, _ in touching if a.has_card]
        if commented:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "that text carries a comment — change its colour from its own card",
            )

        absorbed = {a.id for a, _ in touching}
        keep = [a for a in self.annotations_for(doc_id) if a.id not in absorbed]

        fresh = Annotation(
            id=self.new_id({a.id for a in self.annotations_for(doc_id)} | absorbed),
            target=target,
            kind="highlight",
            color=color,
        )
        candidate = keep + [fresh]
        failures = self.check_anchors(doc_id, candidate)
        if fresh.id in failures:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"could not anchor the selection: {failures[fresh.id]}",
            )
        self.save(doc_id, candidate)
        return {
            "annotation": fresh.to_dict(),
            "replaced": [a.id for a, _ in touching],
        }

    def erase_highlight(self, doc_id: str, target: TextTarget) -> dict:
        """Take the highlighting off a selection, whatever colours it had.

        The counterpart to a colour press, and the reason a press can be
        unconditional. Comments are left where they are: their highlight is
        the handle for a thread, and an eraser aimed at colour should not
        delete a conversation. Removing one is what its own card is for.
        """
        placed, index = self.spans_in(doc_id)
        span = index.find(target)
        if span is None:
            raise ApiError(
                HTTPStatus.CONFLICT, f"could not anchor the selection: {target.quote!r}"
            )

        touching = [
            a for a, where in placed
            if where[0] < span[1] and span[0] < where[1] and not a.has_card
        ]
        if not touching:
            return {"cleared": []}

        gone = {a.id for a in touching}
        self.save(doc_id, [a for a in self.annotations_for(doc_id) if a.id not in gone])
        return {"cleared": sorted(gone)}

    def check_anchors(self, doc_id: str, annotations: list[Annotation]) -> dict[str, str]:
        """Render without saving; report which annotations fail to anchor."""
        result = self.render(doc_id, annotations)
        return {p.annotation.id: p.reason for p in result.unresolved}

    def new_id(self, taken: set[str]) -> str:
        while True:
            candidate = "".join(random.choices(ID_ALPHABET, k=ID_LENGTH))
            if candidate not in taken:
                return candidate


def credentials() -> tuple[str, str] | None:
    """The username and password every request must present, if any.

    Unset means no authentication, which is right for the loopback server on
    your own machine and wrong anywhere else -- see `refuse_insecure_bind`.
    """
    password = os.environ.get("MDWEAVE_PASSWORD")
    if not password:
        return None
    return os.environ.get("MDWEAVE_USER") or "mdweave", password


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def refuse_insecure_bind(host: str) -> str | None:
    """Why this bind should not happen, or None if it is fine.

    Reachable from another machine and unauthenticated is the one combination
    that must never happen by accident: every write endpoint is open, and
    `/api/checkpoint` will push to a git remote with whatever credential the
    process has. Set MDWEAVE_ALLOW_INSECURE=1 to say you meant it.
    """
    if host in LOOPBACK or credentials() or os.environ.get("MDWEAVE_ALLOW_INSECURE"):
        return None
    return (
        f"refusing to serve on {host} without a password.\n"
        "  Anyone who can reach this port could edit the documents and push to git.\n"
        "  Set MDWEAVE_PASSWORD, or MDWEAVE_ALLOW_INSECURE=1 if you really mean it."
    )


class Handler(SimpleHTTPRequestHandler):
    """Static file server for html_outputs, plus a small JSON API."""

    workspace: Workspace

    # Deliberately left at the inherited HTTP/1.0. It was briefly raised to
    # 1.1, on the theory that a streaming response wants chunked encoding --
    # it does not: the event stream already sends `Connection: close`, and
    # "the body ends when the socket does" is exactly what EventSource
    # expects. What 1.1 did bring was keep-alive, and with it a hang. A
    # `fetch()` that inspects `response.ok` and returns without reading the
    # body leaves that body undrained; under 1.0 the close ended the request
    # anyway, while under 1.1 the browser counted it in flight forever and no
    # page ever reached `networkidle`. The whole browser suite stopped.
    #
    # Keep-alive is worth having one day. It is worth having deliberately,
    # with every error path audited, and not as a side effect of adding SSE.

    def __init__(
        self, *args, workspace: Workspace, autosave=None, jobs=None, **kwargs
    ) -> None:
        self.workspace = workspace
        self.autosave = autosave or AutoCommit(workspace.inputs, workspace.outputs)
        self.jobs = jobs
        super().__init__(*args, directory=str(workspace.outputs), **kwargs)

    # --- authentication ----------------------------------------------------

    def _allowed(self) -> bool:
        """Check HTTP Basic credentials, if any are configured.

        Basic rather than a login page: the browser prompts, remembers, and
        re-sends it on the page's own fetch() calls without a line of code
        here. It is only as private as the transport, which on Railway is TLS.
        """
        wanted = credentials()
        if wanted is None:
            return True

        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                offered = base64.b64decode(header[6:], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                offered = ""
            user, _, password = offered.partition(":")
            # Compare both, always, rather than short-circuiting on the user.
            ok_user = hmac.compare_digest(user, wanted[0])
            ok_password = hmac.compare_digest(password, wanted[1])
            if ok_user and ok_password:
                return True

        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="mdweave", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _broker(self):
        if self.jobs is None:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE, "this server has no job broker"
            )
        return self.jobs

    # --- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 -- name fixed by BaseHTTPRequestHandler
        # The one unauthenticated route. A platform health check cannot present
        # a password, and answering it with /api/health would publish the list
        # of document names to anyone who asked.
        if urlparse(self.path).path == "/api/ping":
            self._respond(HTTPStatus.OK, {"ok": True})
            return
        if not self._allowed():
            return
        route = urlparse(self.path).path
        # Answered by streaming rather than by returning a payload, so it
        # cannot go through `_dispatch`.
        if route.startswith("/api/agent/jobs/") and route.endswith("/events"):
            self._stream_job_events(route[len("/api/agent/jobs/") : -len("/events")])
        elif route.startswith("/api/"):
            self._dispatch(self._api_get)
        elif route in ("", "/"):
            self._serve_root()
        else:
            super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        super().do_HEAD()

    def _serve_root(self) -> None:
        """Send the bare URL to a document rather than a directory listing.

        `mdweave start` has no document to open -- the sidebar on every page is
        how you get to the rest -- so the root just needs to reach one of them.
        """
        documents = sorted(self.workspace.documents())
        if not documents:
            self._respond(
                HTTPStatus.NOT_FOUND,
                {"error": f"no documents under {self.workspace.inputs}"},
            )
            return
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", f"/{quote(documents[0])}.html")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        # The runner's endpoints take no credential. Deliberately: what they
        # can do is collect a job and report on it, and neither leads anywhere
        # -- a job can only be *queued* with the operator key, and a job can
        # only run on a machine someone has started a runner on. The cost of
        # leaving them open is that a stranger who finds the URL could claim
        # your jobs, reading the instruction text and stopping your own runner
        # from getting them. A traded risk, taken knowingly: see the bridge
        # section of the README.
        if not is_runner_route(urlparse(self.path).path):
            if not self._allowed():
                return
        self._dispatch(self._api_post)

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        self._dispatch(self._api_patch)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        self._dispatch(self._api_delete)

    def _dispatch(self, handler) -> None:
        try:
            status, payload = handler()
        except ApiError as exc:
            status, payload = exc.status, {"error": exc.message}
        except Exception as exc:  # keep the server alive on an unexpected fault
            self.log_error("unhandled: %r", exc)
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        self._respond(status, payload)

        # One choke point rather than a call in each of thirteen handlers, so a
        # new endpoint cannot forget to arm it. After the response, so arming
        # never costs the request anything.
        if self.command != "GET" and 200 <= int(status) < 300:
            if _writes_something(urlparse(self.path).path):
                self.autosave.touch()

    # --- endpoints -------------------------------------------------------

    def _api_get(self):
        route = urlparse(self.path)
        if route.path == "/api/health":
            return HTTPStatus.OK, {
                "ok": True,
                "pid": os.getpid(),  # how `mdweave stop` finds this process
                "fingerprint": RUNNING_FINGERPRINT,
                "autosave": self.autosave.status(),
                "documents": sorted(self.workspace.documents()),
            }

        if route.path == "/api/schemes":
            theme = self.workspace.theme()
            return HTTPStatus.OK, {
                "active": theme.active,
                "schemes": [s.to_dict() for s in theme.schemes],
                "slots": schemes.SLOTS,
            }

        if route.path == "/api/annotations":
            doc_id = self._query(route, "document")
            annotations = self.workspace.annotations_for(doc_id)
            return HTTPStatus.OK, {"annotations": [a.to_dict() for a in annotations]}

        if route.path == "/api/block":
            doc_id = self._query(route, "document")
            line, end = self._span(route)
            return HTTPStatus.OK, {
                "markdown": self.workspace.block_of(doc_id, line, end)
            }

        if route.path == "/api/agent/status":
            return HTTPStatus.OK, self._agent_status(route)

        if route.path.startswith("/api/agent/jobs/"):
            job = self._broker().get(route.path[len("/api/agent/jobs/") :])
            if job is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "no such job")
            return HTTPStatus.OK, {"job": job.summary()}

        raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {route.path}")

    def _agent_status(self, route) -> dict:
        """Whether there is a runner, and what it could be asked to do.

        The page hides the button when nothing is listening, the same way every
        other control here hides itself when there is no server -- a button
        that queues work nobody will ever collect is worse than no button.
        """
        from .agent import actions as agent_actions

        broker = self._broker()
        status = broker.status()
        status["actions"] = [a.to_dict() for a in agent_actions.load(self.workspace.inputs)]

        document = parse_qs(route.query).get("document", [""])[0]
        if document:
            status["recent"] = [j.summary() for j in broker.recent(document, limit=5)]
            try:
                status["session"] = self.workspace.session_of(document).to_dict()
            except ApiError:
                status["session"] = {}
        return status

    # --- the agent bridge --------------------------------------------------

    def _stream_job_events(self, job_id: str) -> None:
        """One job's progress, as Server-Sent Events.

        The browser's `EventSource` reconnects on its own and tells us where it
        got to via `Last-Event-ID`; the replay buffer that makes that mean
        something lives in the broker, because the SSE standard carries the id
        around and stores nothing.

        `Connection: close` and no Content-Length: under HTTP/1.1 that is the
        way to say "this body ends when the socket does", which is the only
        thing that can be said about a stream whose length nobody knows.

        The operator key is *not* asked for here, and the asymmetry is
        deliberate. `EventSource` cannot set a request header, so the only ways
        to present one would be a query parameter -- a secret in a URL, which
        lands in every log that touches it -- or a cookie, which is a session
        mechanism this server does not otherwise have. What that key protects
        is the power to run something on the devserver, and that is the POST.
        Watching a job you already queued report its progress is reading, and
        reading is already behind the page's own password.
        """
        try:
            broker = self._broker()
        except ApiError as exc:
            self._respond(exc.status, {"error": exc.message})
            return

        if broker.get(job_id) is None:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "no such job"})
            return

        after = 0
        resumed = self.headers.get("Last-Event-ID", "")
        if resumed.isdigit():
            after = int(resumed)
        else:
            asked = parse_qs(urlparse(self.path).query).get("after", ["0"])[0]
            if asked.isdigit():
                after = int(asked)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # For any intermediary that would otherwise buffer the whole response
        # and deliver a stream as one lump at the end.
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        deadline = time.monotonic() + STREAM_LIFETIME
        try:
            while True:
                events, state = broker.wait_for_events(
                    job_id, after, timeout=protocol.STREAM_HEARTBEAT
                )
                for event in events:
                    after = event.seq
                    self._sse(event.seq, event.kind, json.dumps(event.to_dict()))

                if state in protocol.TERMINAL and not events:
                    self._sse(after, "closed", json.dumps({"state": state}))
                    return
                if not events:
                    # A comment: it keeps the connection out of an idle
                    # timeout without being an event the page has to ignore.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                if time.monotonic() > deadline:
                    # Railway caps a request at fifteen minutes regardless, so
                    # end it deliberately and let EventSource come back.
                    self._sse(after, "reconnect", json.dumps({"state": state}))
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # the reader closed the tab; nothing to report

    def _sse(self, seq: int, event: str, data: str) -> None:
        chunk = f"id: {seq}\nevent: {event}\ndata: {data}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _api_post(self):
        route = urlparse(self.path)
        if route.path == "/api/documents":
            return self._api_import()
        if route.path == "/api/folders/create":
            return self._api_folder_create()
        if route.path == "/api/folders/rename":
            return self._api_folder_rename()
        if route.path == "/api/folders/delete":
            return self._api_folder_delete()
        if route.path == "/api/documents/create":
            return self._api_create()
        if route.path == "/api/documents/move":
            return self._api_move()
        if route.path == "/api/documents/delete":
            return self._api_delete_document()
        if route.path == "/api/tree/order":
            return self._api_order()
        if route.path == "/api/tree/label":
            return self._api_label()
        if route.path == "/api/block":
            return self._api_block()
        if route.path == "/api/cut":
            return self._api_cut()
        if route.path == "/api/extract":
            return self._api_extract()
        if route.path == "/api/refresh":
            return self._api_refresh()
        if route.path == "/api/checkpoint":
            return self._api_checkpoint()
        if route.path == "/api/schemes":
            return self._api_schemes()
        if route.path.startswith("/api/agent/"):
            return self._api_agent(route.path)
        if route.path == "/api/annotations/erase":
            payload = self._json_body()
            doc_id = _require(payload, "document")
            self.workspace.markdown_for(doc_id)  # 404s an unknown document
            return HTTPStatus.OK, self.workspace.erase_highlight(
                doc_id,
                TextTarget(
                    quote=_require(payload, "quote"),
                    prefix=str(payload.get("prefix", "")),
                    suffix=str(payload.get("suffix", "")),
                    occurrence=int(payload.get("occurrence", 0)),
                ),
            )

        if route.path != "/api/annotations":
            raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {route.path}")

        payload = self._json_body()
        doc_id = _require(payload, "document")
        quote = _require(payload, "quote")

        # A highlight is a comment with nothing to say: the same anchor and the
        # same colour, no thread, and so no card. Its body is not merely
        # optional -- it must be absent, or the card would come back.
        kind = str(payload.get("kind", "comment"))
        if kind not in ("comment", "highlight"):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"unknown kind: {kind!r}")

        target = TextTarget(
            quote=quote,
            prefix=str(payload.get("prefix", "")),
            suffix=str(payload.get("suffix", "")),
            occurrence=int(payload.get("occurrence", 0)),
        )

        # A highlight goes through the painting rule rather than being
        # appended: a colour pressed over an existing highlight recolours it,
        # unconditionally. Taking one off is /api/annotations/erase.
        if kind == "highlight":
            return HTTPStatus.OK, self.workspace.apply_highlight(
                doc_id, target, _parse_color(payload.get("color", DEFAULT_COLOR))
            )

        body = _require(payload, "body")

        annotations = self.workspace.annotations_for(doc_id)
        annotation = Annotation(
            id=self.workspace.new_id({a.id for a in annotations}),
            target=target,
            kind="comment",
            color=_parse_color(payload.get("color", DEFAULT_COLOR)),
            thread=[
                Comment(
                    body=body,
                    author=str(payload.get("author", "me")),
                    at=payload.get("at"),
                )
            ],
        )

        # Round-trip check before writing: confirm the anchor the browser
        # computed still resolves once Python re-renders the document. This is
        # what stops a subtly wrong selector from being persisted.
        candidate = annotations + [annotation]
        failures = self.workspace.check_anchors(doc_id, candidate)
        if annotation.id in failures:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"could not anchor the selection: {failures[annotation.id]}",
            )

        self.workspace.save(doc_id, candidate)
        return HTTPStatus.CREATED, {"annotation": annotation.to_dict()}

    def _api_import(self):
        """Accept a markdown file from the browser and publish it."""
        payload = self._json_body(limit=MAX_UPLOAD_BYTES)
        name = _require(payload, "name")

        content = payload.get("content")
        if not isinstance(content, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "'content' must be a string")

        doc_id = self.workspace.add_document(
            name,
            content,
            bool(payload.get("replace")),
            folder=_optional(payload, "folder"),
        )

        # Every page carries its own sidebar, so they all need regenerating for
        # the new document to be reachable -- no restart involved.
        self.workspace.rebuild_tree({doc_id})

        return HTTPStatus.CREATED, _with_panel(self, payload, _described(doc_id))

    def _api_folder_create(self):
        """Make an empty folder. Without this the tree can never be nested."""
        payload = self._json_body()
        folder = self.workspace.create_folder(_require(payload, "path"))
        # An empty folder holds no documents, so no page's content changes --
        # but every page draws the tree, and the tree just gained a row.
        self.workspace.rebuild_tree()
        return HTTPStatus.CREATED, _with_panel(self, payload, {"folder": {
            "path": folder, "label": humanize(folder.rpartition("/")[2])
        }})

    def _api_folder_rename(self):
        """Rename a folder, carrying everything under it."""
        payload = self._json_body()
        folder = self.workspace.rename_folder(
            _require(payload, "from"), _require(payload, "to")
        )
        self.workspace.rebuild_tree()
        return HTTPStatus.OK, _with_panel(self, payload, {"folder": {
            "path": folder, "label": humanize(folder.rpartition("/")[2])
        }})

    def _api_folder_delete(self):
        """Remove a folder, and say how many documents went with it."""
        payload = self._json_body()
        removed = self.workspace.delete_folder(
            _require(payload, "folder"), bool(payload.get("recursive"))
        )
        self.workspace.rebuild_tree()
        return HTTPStatus.OK, _with_panel(self, payload, {"removed": removed})

    def _api_create(self):
        """Make a new, empty document where the reader asked for one."""
        payload = self._json_body()
        doc_id = self.workspace.create_document(_require(payload, "path"))
        self.workspace.rebuild_tree({doc_id})
        return HTTPStatus.CREATED, _with_panel(self, payload, _described(doc_id))

    def _api_move(self):
        """Move a document into another folder, or rename it -- one operation.

        A document's id is its path, so both are the same write: `notes/weekly`
        to `archive/weekly` is a drag, and to `notes/summary` is a rename.
        """
        payload = self._json_body()
        doc_id = self.workspace.move_document(
            _require(payload, "from"), _require(payload, "to")
        )
        self.workspace.rebuild_tree({doc_id})
        return HTTPStatus.OK, _with_panel(self, payload, _described(doc_id))

    def _api_label(self):
        """Name a row. The file on disk keeps the name it has.

        Renaming used to move the file, which made the caption and the id one
        thing -- so a label the heuristic could not produce was unreachable
        without a filename nobody wanted. They are separate now: the id stays
        stable (links, pages and annotations all hang off it) and the caption
        is whatever was typed. Moving a document is still a move.
        """
        payload = self._json_body()
        key = _require(payload, "key")
        kind = payload.get("kind", "document")

        # Looked up, never joined -- the same guard every other write uses.
        if kind == "folder":
            self.workspace.folder_for(key)
        else:
            self.workspace.markdown_for(key)

        label = self.workspace.set_label(key, _require(payload, "label"))
        self.workspace.rebuild_tree()
        return HTTPStatus.OK, _with_panel(self, payload, {"key": key, "label": label})

    def _api_delete_document(self):
        """Throw a document away -- the prose, its annotations, and its page."""
        payload = self._json_body()
        doc_id = _require(payload, "document")

        self.workspace.delete_document(doc_id)
        self.workspace.rebuild_tree()
        return HTTPStatus.OK, _with_panel(self, payload, {"deleted": doc_id})

    def _api_order(self):
        """Persist the order the reader dragged one folder's children into."""
        payload = self._json_body()
        folder = payload.get("folder", "")
        if not isinstance(folder, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "'folder' must be a string")

        names = payload.get("order")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ApiError(HTTPStatus.BAD_REQUEST, "'order' must be a list of names")

        kept = self.workspace.set_order(folder, names)
        # The arrangement is baked into every page's sidebar, same as the set
        # of documents is.
        self.workspace.rebuild_tree()
        return HTTPStatus.OK, _with_panel(self, payload, {"folder": folder, "order": kept})

    def _api_block(self):
        """Replace one block's markdown -- clicking a paragraph and typing."""
        payload = self._json_body(limit=MAX_UPLOAD_BYTES)
        doc_id = _require(payload, "document")

        text = payload.get("text")
        if not isinstance(text, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "'text' must be a string")

        line, end = _line_range(payload)
        source = self.workspace.source_of(doc_id)
        try:
            updated = edits.replace_block(source, line, end, text)
        except IndexError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc))

        return self._rewrite(doc_id, source, updated)

    def _api_cut(self):
        """Remove a selection that may run across several blocks."""
        payload = self._json_body(limit=MAX_UPLOAD_BYTES)
        doc_id = _require(payload, "document")
        cuts = _spans(payload, "cuts", edits.Cut)

        source = self.workspace.source_of(doc_id)
        return self._rewrite(doc_id, source, edits.apply_cuts(source, cuts))

    def _api_extract(self):
        """The markdown under a selection -- what Cmd-C puts on the clipboard.

        The only endpoint here that reads without writing: nothing is saved,
        nothing is re-rendered, and a failure costs the reader nothing but the
        formatting, since the browser has already copied the rendered text.
        """
        payload = self._json_body(limit=MAX_UPLOAD_BYTES)
        doc_id = _require(payload, "document")
        spans = _spans(payload, "spans", edits.Span)

        source = self.workspace.source_of(doc_id)
        return HTTPStatus.OK, {"markdown": edits.extract_spans(source, spans)}

    def _api_refresh(self):
        """Re-render from what is on disk now.

        The markdown is a file, and this server is not the only thing that
        writes to it. Everything is rebuilt, not just this document: an editor
        or a `git pull` may have touched several, and a new file changes the
        sidebar on every page.
        """
        payload = self._json_body()
        doc_id = _require(payload, "document")

        self.workspace.markdown_for(doc_id)  # 404s a document that has gone
        self.workspace.rebuild_all()
        body, notes = self.workspace.body_of(doc_id)

        return HTTPStatus.OK, {
            "body": body,
            "notes": notes,
            "documents": sorted(self.workspace.documents()),
        }

    def _api_schemes(self):
        """Save the schemes, and switch to one.

        Three things can change in one request and the order matters. The
        renumbering goes first, because it is expressed in the slots as they
        are *now*; then the file; then one rebuild, which repaints every page
        from whichever scheme ended up active. Rebuilding before the
        renumbering would publish pages in colours that are about to move.
        """
        payload = self._json_body()

        raw = payload.get("schemes")
        if not isinstance(raw, list) or not raw:
            raise ApiError(HTTPStatus.BAD_REQUEST, "at least one scheme is needed")
        try:
            parsed = [schemes.Scheme.from_dict(entry) for entry in raw]
        except (ValueError, AttributeError, TypeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc))

        names = [s.name for s in parsed]
        if len(set(names)) != len(names):
            raise ApiError(HTTPStatus.BAD_REQUEST, "two schemes share a name")

        active = payload.get("active")
        if not isinstance(active, str) or active not in names:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"no scheme called {active!r}")

        renumbered = 0
        moved = payload.get("remap")
        if moved is not None:
            if (
                not isinstance(moved, list)
                or sorted(moved) != list(range(1, schemes.SLOTS + 1))
            ):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    f"remap must be a permutation of 1..{schemes.SLOTS}",
                )
            renumbered = self.workspace.remap_slots(moved)

        self.workspace.write_theme(schemes.Theme(schemes=parsed, active=active))
        self.workspace.rebuild_all()

        current = self.workspace.theme().current()
        return HTTPStatus.OK, {
            "active": active,
            "schemes": [s.to_dict() for s in parsed],
            "renumbered": renumbered,
            "warnings": schemes.unreadable(current),
        }

    def _api_agent(self, path: str):
        """Everything the bridge does, split by who is calling.

        The browser's routes are gated by the operator key. The runner's are
        not gated at all -- collecting a job and reporting on it lead nowhere,
        and the endpoint that *starts* something is on the other side of this
        branch. `is_runner_route` is the whole of that boundary.
        """
        broker = self._broker()

        # --- the runner's side ---------------------------------------------
        if is_runner_route(path):
            if path == "/api/agent/claim":
                payload = self._json_body() if self._has_body() else {}
                job = broker.claim(
                    timeout=protocol.CLAIM_SECONDS,
                    runner=str(payload.get("runner", ""))[:80],
                )
                if job is None:
                    return HTTPStatus.NO_CONTENT, {}
                return HTTPStatus.OK, protocol.dispatch(job)

            if path == "/api/agent/reconcile":
                return HTTPStatus.OK, self._api_agent_reconcile()

            job_id, _, verb = path[len("/api/agent/jobs/") :].rpartition("/")
            if verb == "events":
                payload = self._json_body()
                event = broker.note(
                    job_id,
                    str(payload.get("kind", "status"))[:20],
                    str(payload.get("text", ""))[:2000],
                )
                if event is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "no such job, or it has finished")
                return HTTPStatus.OK, {"seq": event.seq}

            payload = self._json_body()
            job = broker.finish(
                job_id,
                bool(payload.get("ok")),
                str(payload.get("detail", ""))[:2000],
                str(payload.get("revision", ""))[:80],
                str(payload.get("session", ""))[:80] or None,
            )
            if job is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "no such job")
            return HTTPStatus.OK, {"job": job.summary()}

        # --- the browser's side ---------------------------------------------
        #
        # Gated by the page password alone, which `do_POST` has already
        # checked. That makes the page password an execute-code-here
        # credential whenever a runner is connected -- see the bridge section
        # of DEPLOY.md, where that is stated rather than left to be noticed.
        if path == "/api/agent/jobs":
            from .agent import actions as agent_actions

            payload = self._json_body()
            doc_id = _require(payload, "document")
            self.workspace.markdown_for(doc_id)  # 404s a document that is gone

            name = _require(payload, "action")
            action = agent_actions.find(self.workspace.inputs, name)
            if action is None:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"no action called {name!r}")

            instruction = _optional(payload, "instruction").strip()
            if action.needs_instruction and not instruction:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, f"{action.label} needs something to be asked"
                )

            if not broker.status()["connected"]:
                raise ApiError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "no runner is connected — start `mdweave agent` on the devserver",
                )

            job = broker.submit(doc_id, action.name, instruction[:8000])
            return HTTPStatus.ACCEPTED, {"job": job.summary()}

        raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")

    def _api_agent_reconcile(self) -> dict:
        """Take in what the runner just pushed, and rebuild over it.

        Without this the loop is silently broken in the least obvious way: the
        edit is committed, the push succeeds, everything reports success, and
        the page keeps serving the prose from boot. The container only ever
        pulled at startup.
        """
        self._json_body() if self._has_body() else {}
        try:
            repo = git_checkpoint.repo_root(self.workspace.inputs)
            # The pages this container rendered for itself are about to be
            # rebuilt anyway, and leaving them in place is enough on its own to
            # make the merge refuse to start.
            git_checkpoint.discard_generated(repo, self.workspace.outputs)
            merged = git_checkpoint.reconcile(repo, self.workspace.outputs)
        except git_checkpoint.GitError as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, str(exc))

        self.workspace.rebuild_all()
        return {"merged": merged, "documents": sorted(self.workspace.documents())}

    def _has_body(self) -> bool:
        try:
            return int(self.headers.get("Content-Length") or 0) > 0
        except ValueError:
            return False

    def _api_checkpoint(self):
        """Commit and push one document, with the message the reader wrote."""
        payload = self._json_body()
        doc_id = _require(payload, "document")
        message = _require(payload, "message")

        paths = self.workspace.files_of(doc_id)  # 404s an unknown document
        try:
            repo = git_checkpoint.repo_root(self.workspace.inputs)
            result = git_checkpoint.checkpoint(
                repo,
                paths,
                message,
                generated=self.workspace.outputs,
                on_merge=self.workspace.rebuild_all,
            )
        except git_checkpoint.GitError as exc:
            # The reader can act on what git said, so pass it through rather
            # than flattening it into "checkpoint failed".
            raise ApiError(HTTPStatus.BAD_GATEWAY, str(exc))

        return HTTPStatus.OK, {
            "committed": result.committed,
            "pushed": result.pushed,
            "sent": result.sent,
            "revision": result.revision,
            "detail": result.detail,
        }

    def _rewrite(self, doc_id: str, before: str, after: str):
        """Persist a new version of a document and hand back the new page body.

        Nothing is written when the text is unchanged: a click that opens a
        block and closes it again should not touch the file or rebuild.
        """
        if after == before:
            body, notes = self.workspace.body_of(doc_id)
            return HTTPStatus.OK, {"changed": False, "body": body, "notes": notes}

        self.workspace.write_source(doc_id, after)
        body, notes = self.workspace.body_of(doc_id)
        return HTTPStatus.OK, {"changed": True, "body": body, "notes": notes}

    def _api_patch(self):
        route = urlparse(self.path)
        prefix = "/api/annotations/"
        if not route.path.startswith(prefix):
            raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {route.path}")

        ann_id = route.path[len(prefix) :]
        payload = self._json_body()
        doc_id = _require(payload, "document")

        annotations = self.workspace.annotations_for(doc_id)
        target = next((a for a in annotations if a.id == ann_id), None)
        if target is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no annotation {ann_id!r}")

        if "offset" in payload:
            target.offset = _parse_offset(payload["offset"])
        if "color" in payload:
            target.color = _parse_color(payload["color"])

        self.workspace.save(doc_id, annotations)
        return HTTPStatus.OK, {"annotation": target.to_dict()}

    def _api_delete(self):
        route = urlparse(self.path)
        prefix = "/api/annotations/"
        if not route.path.startswith(prefix):
            raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {route.path}")

        ann_id = route.path[len(prefix) :]
        doc_id = self._query(route, "document")

        annotations = self.workspace.annotations_for(doc_id)
        remaining = [a for a in annotations if a.id != ann_id]
        if len(remaining) == len(annotations):
            raise ApiError(HTTPStatus.NOT_FOUND, f"no annotation {ann_id!r}")

        self.workspace.save(doc_id, remaining)
        return HTTPStatus.OK, {"deleted": ann_id}

    # --- plumbing --------------------------------------------------------

    def _query(self, route, key: str) -> str:
        values = parse_qs(route.query).get(key)
        if not values:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"missing ?{key}=")
        return values[0]

    def _span(self, route) -> tuple[int, int]:
        """The ?start=&end= source-line range a block carries in its markup."""
        try:
            line = int(self._query(route, "start"))
            end = int(self._query(route, "end"))
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "start and end must be integers")
        # `end == line` is allowed: an empty range at the end of the file is
        # the append point the "start writing" box names, and the read and the
        # write both understand it.
        if line < 0 or end < line:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"bad line range {line}-{end}")
        return line, end

    def _require_json_type(self) -> None:
        """Refuse a body that does not announce itself as JSON.

        This looks like pedantry and is not. A browser attaches HTTP Basic
        credentials to *any* request to an origin it has them for, including
        one started by somebody else's website -- there is no SameSite for
        Basic auth the way there is for cookies. And a plain HTML form can be
        submitted cross-origin without the browser asking permission first,
        provided it uses one of three content types, of which `text/plain` is
        one.

        This server used to parse whatever arrived, whatever it claimed to be.
        So a form with `enctype="text/plain"` and a JSON-shaped field name was
        a working request, which meant that merely visiting a hostile page
        while logged in here could queue a job -- and a job runs commands on
        the devserver. Checked and reproduced before this was written.

        `application/json` is not on the list a form may send, and setting it
        from script forces a preflight this server never answers. So requiring
        it ends that whole class of attack at one line, for every endpoint at
        once, without depending on the operator key being set.
        """
        declared = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        if declared.strip().lower() != "application/json":
            raise ApiError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "this endpoint takes application/json",
            )

    def _json_body(self, limit: int = MAX_BODY_BYTES) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "empty request body")
        self._require_json_type()
        if length > limit:
            # Read the body before answering. Replying to a request whose body
            # is still in flight makes the client see a connection reset rather
            # than the 413, so the reason never reaches the user.
            self._drain(length)
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}")
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected a JSON object")
        return payload

    def _drain(self, length: int) -> None:
        """Discard an unwanted request body so the response can be delivered."""
        remaining = min(length, DRAIN_LIMIT)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        if length > DRAIN_LIMIT:
            self.close_connection = True  # too big to bother draining

    def _respond(self, status: HTTPStatus, payload: dict) -> None:
        # 204 is defined as having no body at all, and under HTTP/1.1 a client
        # that is told otherwise waits for bytes that never come. It is the
        # honest answer to "is there any work?", so it has to be sent properly.
        if status == HTTPStatus.NO_CONTENT:
            self.send_response(status)
            self.end_headers()
            return

        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def end_headers(self) -> None:
        # The page and its assets are rewritten underneath the browser on every
        # save, so caching them only ever serves stale content.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        if self.command != "GET" or "/api/" in self.path:
            super().log_message(fmt, *args)


def _parse_color(raw) -> int:
    """Validate a client-supplied colour: one of the six slots, nothing else.

    A raw CSS colour is legitimate in a hand-edited sidecar, but it reaches the
    page as an inline custom property -- so taking one from the browser would
    be writing a client string into a style attribute. A slot is all the picker
    can produce anyway.

    Stored as the number. The class name `c3` is what the picker sends and what
    the page wears, but writing that into a sidecar would put a presentation
    detail in a data file; the file says 3. `slot_of` also takes the number and
    the hue names the palette used to have, which costs nothing -- all three
    name a slot, and none of them can name a colour.
    """
    slot = schemes.slot_of(raw)
    if slot is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unknown colour {raw!r}")
    return slot


def _parse_offset(raw) -> Offset | None:
    """Validate a client-supplied {dx, dy}. Zero or null means "reset"."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "offset must be an object")
    try:
        dx = float(raw.get("dx", 0))
        dy = float(raw.get("dy", 0))
    except (TypeError, ValueError):
        raise ApiError(HTTPStatus.BAD_REQUEST, "offset dx and dy must be numbers")
    if not (math.isfinite(dx) and math.isfinite(dy)):
        raise ApiError(HTTPStatus.BAD_REQUEST, "offset dx and dy must be finite")

    dx = max(-OFFSET_LIMIT, min(OFFSET_LIMIT, dx))
    dy = max(-OFFSET_LIMIT, min(OFFSET_LIMIT, dy))
    offset = Offset(dx=dx, dy=dy)
    return offset if offset else None


def _covers(span: tuple[int, int], others: list[tuple[int, int]], text: str) -> bool:
    """Is every character of `span` that a reader could see inside one of `others`?

    Whitespace is skipped. Two highlights sitting either side of a space do
    cover the phrase they spell out, and treating that space as a hole would
    make pressing their colour repaint rather than clear -- a distinction with
    nothing behind it, since a space carries no highlight to speak of.
    """
    inside = set()
    for start, end in others:
        inside.update(range(max(start, span[0]), min(end, span[1])))
    return all(
        at in inside or text[at].isspace() for at in range(span[0], span[1])
    )


def _int_field(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key!r} must be a non-negative integer")
    return value


def _spans(payload: dict, key: str, make) -> list:
    """Parse a list of per-block visible-text ranges.

    One shape, two endpoints: cutting a selection and copying it both describe
    it as "characters `from`..`to` of the block at lines `start`..`end`", which
    is all the browser can say without knowing any markdown.
    """
    raw = payload.get(key)
    if not isinstance(raw, list) or not raw:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key!r} must be a non-empty list")

    spans = []
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"each entry in {key!r} must be an object")
        line, end = _line_range(item)
        spans.append(
            make(
                line=line,
                end=end,
                start=_int_field(item, "from"),
                stop=_int_field(item, "to"),
            )
        )
    return spans


def _line_range(payload: dict) -> tuple[int, int]:
    # An empty range inserts rather than replaces -- see `block_source`. It is
    # how the "start writing" box on an empty document writes its first block.
    line, end = _int_field(payload, "start"), _int_field(payload, "end")
    if end < line:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"bad line range {line}-{end}")
    return line, end


def _with_panel(handler, payload: dict, body: dict) -> dict:
    """Add the caller's freshly rendered sidebar to a response.

    The page the reader is on is theirs to name -- the server has no idea which
    of the documents it just re-indexed is on screen.
    """
    page = payload.get("page")
    if isinstance(page, str) and page:
        body["sidebar"] = handler.workspace.sidebar_for(page)
    return body


def _described(doc_id: str) -> dict:
    """What the browser needs to open a document it has just caused to exist."""
    return {
        "document": {
            "id": doc_id,
            "href": f"{doc_id}.html",
            "label": humanize(doc_id.rpartition("/")[2]),
        }
    }


def _optional(payload: dict, key: str) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key!r} must be a string")
    return value


def _require(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, f"missing or empty {key!r}")
    return value


def autocommit_for(workspace: Workspace) -> AutoCommit:
    """Read the auto-commit setting from the environment.

    Off unless MDWEAVE_AUTOCOMMIT names a number of seconds -- opt-in, because
    committing on somebody's behalf is not a default worth assuming. The
    deployed container sets it; a checkout on your own machine need not.
    """
    raw = os.environ.get("MDWEAVE_AUTOCOMMIT", "").strip()
    try:
        delay = float(raw)
    except ValueError:
        delay = 0.0
    return AutoCommit(
        inputs=workspace.inputs,
        outputs=workspace.outputs,
        delay=delay or DEFAULT_DELAY,
        enabled=delay > 0,
        rebuild=workspace.rebuild_all,
    )


def broker_for(workspace: Workspace):
    """The job queue, kept beside the checkout rather than inside it.

    On Railway the volume is mounted at /data and the clone sits under it, so
    the queue survives a redeploy without ever becoming something git would
    try to commit.
    """
    from .agent.broker import Broker

    store = protocol.setting(protocol.JOBS_STORE)
    path = Path(store) if store else workspace.inputs.parent.parent / "agent-jobs.json"
    return Broker(store=path)


def make_server(
    workspace: Workspace,
    host: str,
    port: int,
    autosave: AutoCommit | None = None,
    jobs=None,
) -> ThreadingHTTPServer:
    workspace.outputs.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer(
        (host, port),
        partial(Handler, workspace=workspace, autosave=autosave, jobs=jobs),
    )


def serve(inputs: Path, outputs: Path, host: str = "127.0.0.1", port: int = 8765) -> int:
    refusal = refuse_insecure_bind(host)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 1

    workspace = Workspace(inputs=inputs, outputs=outputs)
    autosave = autocommit_for(workspace)
    jobs = broker_for(workspace)
    httpd = make_server(workspace, host, port, autosave, jobs)

    guarded = " [password required]" if credentials() else ""
    saving = f"  [auto-commit after {autosave.delay:g}s]" if autosave.enabled else ""
    bridge = "  [agent bridge open]" if jobs is not None else ""
    print(
        f"mdweave serving http://{host}:{httpd.server_port}/  "
        f"[{RUNNING_FINGERPRINT}]{guarded}{saving}{bridge}  (Ctrl-C to stop)"
    )
    for name in workspace.documents():
        print(f"  http://{host}:{httpd.server_port}/{name}.html")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        # Anything written in the last few seconds has an armed timer that will
        # never fire now. Commit it rather than leaving it on the disk only.
        autosave.cancel()
        if autosave.enabled:
            autosave.run_now()
        httpd.server_close()
    return 0
