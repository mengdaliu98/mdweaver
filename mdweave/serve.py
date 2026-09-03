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
import string
import sys
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import checkpoint as git_checkpoint
from . import edits
from .model import COLOR_TOKENS, DEFAULT_COLOR, Annotation, Comment, Offset, TextTarget
from .render import render_document, write_assets
from .sources import obsidian_inline, sidecar
from .tree import build_tree, document_ids, humanize, safe_document_name

ID_ALPHABET = string.ascii_lowercase + string.digits
ID_LENGTH = 5
MAX_BODY_BYTES = 256 * 1024
# An imported document is a whole file, so it needs far more headroom than an
# annotation payload.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
# How much of an over-sized body is worth reading just to answer politely.
DRAIN_LIMIT = 64 * 1024 * 1024

# A dragged note may not be flung arbitrarily far from its anchor.
OFFSET_LIMIT = 4000

# Files whose contents define "the version of mdweave this process is running".
SOURCE_SUFFIXES = {".py", ".js", ".css", ".j2"}


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

    def markdown_for(self, doc_id: str) -> Path:
        """Resolve a document id, rejecting anything not actually present.

        Membership in this dict is the path-traversal guard: `doc_id` is never
        joined onto a path, only looked up.
        """
        try:
            return self.documents()[doc_id]
        except KeyError:
            raise ApiError(HTTPStatus.NOT_FOUND, f"unknown document: {doc_id!r}")

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
            tree=tree if tree is not None else build_tree(list(self.documents())),
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
        write_assets(self.outputs)
        documents = self.documents()
        tree = build_tree(list(documents))
        for doc_id in documents:
            self.write_html(doc_id, self.annotations_for(doc_id), tree=tree)

    def add_document(self, raw_name: str, content: str, replace: bool) -> str:
        """Write an imported markdown file and return its document id."""
        try:
            name = safe_document_name(raw_name)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc))

        target = self.inputs / name
        if target.exists() and not replace:
            raise ApiError(
                HTTPStatus.CONFLICT, f"a document called {name!r} already exists"
            )

        self.inputs.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target.stem

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
        """Replace a document's markdown and re-render everything.

        The whole set is rebuilt rather than just this page because a heading
        edit changes the sidebar label, which every other page has baked in.
        """
        self.markdown_for(doc_id).write_text(markdown, encoding="utf-8")
        self.rebuild_all()

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

        The prose, the annotations beside it, and the page built from them --
        but not `assets/`, which is shared and would drag every other document's
        rebuild into a commit meant for this one.
        """
        markdown = self.markdown_for(doc_id)
        return [
            markdown,
            sidecar.sidecar_path(markdown),
            self.outputs / f"{doc_id}.html",
        ]

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

    def __init__(self, *args, workspace: Workspace, **kwargs) -> None:
        self.workspace = workspace
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
        if route.startswith("/api/"):
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

    # --- endpoints -------------------------------------------------------

    def _api_get(self):
        route = urlparse(self.path)
        if route.path == "/api/health":
            return HTTPStatus.OK, {
                "ok": True,
                "pid": os.getpid(),  # how `mdweave stop` finds this process
                "fingerprint": RUNNING_FINGERPRINT,
                "documents": sorted(self.workspace.documents()),
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

        raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {route.path}")

    def _api_post(self):
        route = urlparse(self.path)
        if route.path == "/api/documents":
            return self._api_import()
        if route.path == "/api/block":
            return self._api_block()
        if route.path == "/api/cut":
            return self._api_cut()
        if route.path == "/api/refresh":
            return self._api_refresh()
        if route.path == "/api/checkpoint":
            return self._api_checkpoint()
        if route.path != "/api/annotations":
            raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {route.path}")

        payload = self._json_body()
        doc_id = _require(payload, "document")
        quote = _require(payload, "quote")
        body = _require(payload, "body")

        annotations = self.workspace.annotations_for(doc_id)
        annotation = Annotation(
            id=self.workspace.new_id({a.id for a in annotations}),
            target=TextTarget(
                quote=quote,
                prefix=str(payload.get("prefix", "")),
                suffix=str(payload.get("suffix", "")),
                occurrence=int(payload.get("occurrence", 0)),
            ),
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

        doc_id = self.workspace.add_document(name, content, bool(payload.get("replace")))

        # Every page carries its own sidebar, so they all need regenerating for
        # the new document to be reachable -- no restart involved.
        self.workspace.rebuild_all()

        return HTTPStatus.CREATED, {
            "document": {
                "id": doc_id,
                "href": f"{doc_id}.html",
                "label": humanize(doc_id),
            }
        }

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

        raw = payload.get("cuts")
        if not isinstance(raw, list) or not raw:
            raise ApiError(HTTPStatus.BAD_REQUEST, "'cuts' must be a non-empty list")

        cuts = []
        for item in raw:
            if not isinstance(item, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "each cut must be an object")
            line, end = _line_range(item)
            cuts.append(
                edits.Cut(
                    line=line,
                    end=end,
                    start=_int_field(item, "from"),
                    stop=_int_field(item, "to"),
                )
            )

        source = self.workspace.source_of(doc_id)
        return self._rewrite(doc_id, source, edits.apply_cuts(source, cuts))

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

    def _api_checkpoint(self):
        """Commit and push one document, with the message the reader wrote."""
        payload = self._json_body()
        doc_id = _require(payload, "document")
        message = _require(payload, "message")

        paths = self.workspace.files_of(doc_id)  # 404s an unknown document
        try:
            repo = git_checkpoint.repo_root(self.workspace.inputs)
            result = git_checkpoint.checkpoint(repo, paths, message)
        except git_checkpoint.GitError as exc:
            # The reader can act on what git said, so pass it through rather
            # than flattening it into "checkpoint failed".
            raise ApiError(HTTPStatus.BAD_GATEWAY, str(exc))

        return HTTPStatus.OK, {
            "committed": result.committed,
            "pushed": result.pushed,
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
        if line < 0 or end <= line:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"bad line range {line}-{end}")
        return line, end

    def _json_body(self, limit: int = MAX_BODY_BYTES) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "empty request body")
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


def _parse_color(raw) -> str:
    """Validate a client-supplied colour: a palette token, nothing else.

    A raw CSS colour is legitimate in a hand-edited sidecar, but it reaches the
    page as an inline custom property -- so taking one from the browser would
    be writing a client string into a style attribute. The five tokens are all
    the picker can produce anyway.
    """
    if raw not in COLOR_TOKENS:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unknown colour {raw!r}")
    return raw


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


def _int_field(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key!r} must be a non-negative integer")
    return value


def _line_range(payload: dict) -> tuple[int, int]:
    line, end = _int_field(payload, "start"), _int_field(payload, "end")
    if end <= line:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"bad line range {line}-{end}")
    return line, end


def _require(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, f"missing or empty {key!r}")
    return value


def make_server(workspace: Workspace, host: str, port: int) -> ThreadingHTTPServer:
    workspace.outputs.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer((host, port), partial(Handler, workspace=workspace))


def serve(inputs: Path, outputs: Path, host: str = "127.0.0.1", port: int = 8765) -> int:
    refusal = refuse_insecure_bind(host)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 1

    workspace = Workspace(inputs=inputs, outputs=outputs)
    httpd = make_server(workspace, host, port)
    guarded = " [password required]" if credentials() else ""
    print(
        f"mdweave serving http://{host}:{httpd.server_port}/  "
        f"[{RUNNING_FINGERPRINT}]{guarded}  (Ctrl-C to stop)"
    )
    for name in workspace.documents():
        print(f"  http://{host}:{httpd.server_port}/{name}.html")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
