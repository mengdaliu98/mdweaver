"""Local editing server.

Serves the generated HTML and accepts new annotations from the browser. A
static page opened over `file://` stays perfectly readable -- it just has no
API to write to, and `annotate.js` degrades to read-only.

Bound to loopback only. There is no authentication, so do not expose it.
"""

from __future__ import annotations

import json
import random
import string
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .model import Annotation, Comment, TextTarget
from .render import render_document
from .sources import obsidian_inline, sidecar

ID_ALPHABET = string.ascii_lowercase + string.digits
ID_LENGTH = 5
MAX_BODY_BYTES = 256 * 1024


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
        return {p.stem: p for p in sorted(self.inputs.glob("*.md"))}

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

    def save(self, doc_id: str, annotations: list[Annotation]) -> None:
        """Persist the sidecar and regenerate the HTML so a reload matches."""
        md = self.markdown_for(doc_id)
        sidecar.save(sidecar.sidecar_path(md), annotations)

        cleaned, _ = obsidian_inline.extract(md.read_text(encoding="utf-8"))
        result = render_document(cleaned, annotations, title=doc_id, doc_id=doc_id)
        (self.outputs / f"{doc_id}.html").write_text(result.html, encoding="utf-8")

    def check_anchors(self, doc_id: str, annotations: list[Annotation]) -> dict[str, str]:
        """Render without saving; report which annotations fail to anchor."""
        md = self.markdown_for(doc_id)
        cleaned, _ = obsidian_inline.extract(md.read_text(encoding="utf-8"))
        result = render_document(cleaned, annotations, title=doc_id, doc_id=doc_id)
        return {p.annotation.id: p.reason for p in result.unresolved}

    def new_id(self, taken: set[str]) -> str:
        while True:
            candidate = "".join(random.choices(ID_ALPHABET, k=ID_LENGTH))
            if candidate not in taken:
                return candidate


class Handler(SimpleHTTPRequestHandler):
    """Static file server for html_outputs, plus a small JSON API."""

    workspace: Workspace

    def __init__(self, *args, workspace: Workspace, **kwargs) -> None:
        self.workspace = workspace
        super().__init__(*args, directory=str(workspace.outputs), **kwargs)

    # --- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 -- name fixed by BaseHTTPRequestHandler
        if urlparse(self.path).path.startswith("/api/"):
            self._dispatch(self._api_get)
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch(self._api_post)

    def do_DELETE(self) -> None:  # noqa: N802
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
            return HTTPStatus.OK, {"ok": True, "documents": sorted(self.workspace.documents())}

        if route.path == "/api/annotations":
            doc_id = self._query(route, "document")
            annotations = self.workspace.annotations_for(doc_id)
            return HTTPStatus.OK, {"annotations": [a.to_dict() for a in annotations]}

        raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {route.path}")

    def _api_post(self):
        route = urlparse(self.path)
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
            color=str(payload.get("color", "amber")),
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

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "empty request body")
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}")
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "expected a JSON object")
        return payload

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


def _require(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, f"missing or empty {key!r}")
    return value


def make_server(workspace: Workspace, host: str, port: int) -> ThreadingHTTPServer:
    workspace.outputs.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer((host, port), partial(Handler, workspace=workspace))


def serve(inputs: Path, outputs: Path, host: str = "127.0.0.1", port: int = 8765) -> int:
    workspace = Workspace(inputs=inputs, outputs=outputs)
    httpd = make_server(workspace, host, port)
    print(f"mdweave serving http://{host}:{httpd.server_port}/  (Ctrl-C to stop)")
    for name in workspace.documents():
        print(f"  http://{host}:{httpd.server_port}/{name}.html")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
