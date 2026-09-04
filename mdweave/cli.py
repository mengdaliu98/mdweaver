"""mdweave command line interface.

The two commands for everyday use:

    mdweave start                        render everything, serve it, open it
    mdweave stop                         shut the server down

The rest are the pieces those are built from, useful on their own:

    mdweave extract  <doc.md>            pull Obsidian inline comments -> sidecar JSON
    mdweave build    <doc.md> -o <dir>   render annotated HTML + CSS
    mdweave build    <dir>    -o <dir>   render every .md in a directory
    mdweave serve    [--port N]          serve in the foreground, no browser
    mdweave fingerprint                  hash of the installed source, for staleness checks
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .render import render_document, write_assets
from .sources import obsidian_inline, sidecar
from .tree import build_tree, document_ids, folder_paths, humanize, load_order


def contents_root() -> Path:
    """Where the documents live.

    The tool and the contents are two independent checkouts sitting side by
    side, so the default is a sibling of this repo. Resolving it from the
    installed package rather than the working directory is what lets
    `mdweave start` be typed from anywhere.
    """
    override = os.environ.get("MDWEAVE_CONTENTS")
    if override:
        return Path(override).expanduser()

    sibling = Path(__file__).resolve().parents[2] / "knowledge_base"
    if sibling.is_dir():
        return sibling
    return Path("knowledge_base")


def default_indir() -> Path:
    return contents_root() / "markdown_inputs"


def default_outdir() -> Path:
    return contents_root() / "html_outputs"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mdweave",
        description="Render annotated markdown to HTML + CSS.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser(
        "start",
        help="render every document, serve them, and open the knowledge base",
    )
    p_start.add_argument(
        "-i", "--indir", type=Path, default=default_indir(), help="markdown directory"
    )
    p_start.add_argument(
        "-o", "--outdir", type=Path, default=default_outdir(), help="output directory"
    )
    p_start.add_argument("--port", type=int, default=8765)
    p_start.add_argument("--host", default="127.0.0.1")
    p_start.add_argument(
        "--no-build",
        action="store_true",
        help="serve what is already on disk instead of rendering first",
    )
    p_start.add_argument(
        "--no-open", action="store_true", help="print the URL without opening a browser"
    )

    p_stop = sub.add_parser("stop", help="shut the background server down")
    p_stop.add_argument("--port", type=int, default=8765)
    p_stop.add_argument("--host", default="127.0.0.1")

    p_extract = sub.add_parser(
        "extract",
        help="move Obsidian inline comments out of a .md into its .ann.json sidecar",
    )
    p_extract.add_argument("input", type=Path, help="markdown file")
    p_extract.add_argument(
        "--in-place",
        action="store_true",
        help="rewrite the markdown without the comment markup (default: keep it)",
    )
    p_extract.add_argument(
        "--merge",
        action="store_true",
        help="keep existing sidecar annotations instead of replacing them",
    )

    p_build = sub.add_parser("build", help="render markdown to HTML")
    p_build.add_argument("input", type=Path, help="markdown file or directory")
    p_build.add_argument(
        "-o", "--outdir", type=Path, default=None, help="output directory"
    )
    p_build.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any annotation fails to anchor",
    )

    p_serve = sub.add_parser(
        "serve", help="serve the rendered HTML and accept comments from the browser"
    )
    p_serve.add_argument(
        "-i", "--indir", type=Path, default=default_indir(), help="markdown directory"
    )
    p_serve.add_argument(
        "-o", "--outdir", type=Path, default=default_outdir(), help="output directory"
    )
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument(
        "--no-build",
        action="store_true",
        help="serve what is already on disk instead of rebuilding first",
    )

    sub.add_parser(
        "fingerprint",
        help="print a hash of the installed source (used to spot a stale server)",
    )

    args = parser.parse_args(argv)

    if args.command == "fingerprint":
        from .serve import source_fingerprint

        print(source_fingerprint())
        return 0
    if args.command == "start":
        return cmd_start(args)
    if args.command == "stop":
        return cmd_stop(args)
    if args.command == "extract":
        return cmd_extract(args)
    if args.command == "serve":
        return cmd_serve(args)
    return cmd_build(args)


def cmd_extract(args) -> int:
    path: Path = args.input
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        return 1

    markdown = path.read_text(encoding="utf-8")
    cleaned, found = obsidian_inline.extract(markdown)

    if not found:
        print(f"{path.name}: no inline comment markup found")
        return 0

    out = sidecar.sidecar_path(path)
    annotations = sidecar.load(out) if args.merge else []
    existing = {a.id for a in annotations}
    annotations.extend(a for a in found if a.id not in existing)

    sidecar.save(out, annotations)
    print(f"{path.name}: extracted {len(found)} annotation(s) -> {out.name}")

    if args.in_place and cleaned != markdown:
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(markdown, encoding="utf-8")
        path.write_text(cleaned, encoding="utf-8")
        print(f"{path.name}: markup removed (original saved as {backup.name})")

    return 0


def cmd_build(args) -> int:
    target: Path = args.input
    if not target.exists():
        print(f"error: no markdown found at {target}", file=sys.stderr)
        return 1

    # The markdown root defines every document id and the sidebar tree, even
    # when only one file is being rebuilt.
    root = target if target.is_dir() else target.parent
    documents = document_ids(root)
    if not documents:
        print(f"error: no markdown found under {root}", file=sys.stderr)
        return 1

    # Folders explicitly, so an empty one is still a row -- the server builds
    # the same tree, and a page built here must not disagree with one it writes.
    tree = build_tree(list(documents), load_order(root), folder_paths(root))
    selected = (
        documents
        if target.is_dir()
        else {k: v for k, v in documents.items() if v.resolve() == target.resolve()}
    )

    outdir: Path = args.outdir or default_outdir()
    outdir.mkdir(parents=True, exist_ok=True)
    write_assets(outdir)

    failures = 0
    for doc_id, path in selected.items():
        markdown = path.read_text(encoding="utf-8")
        annotations = sidecar.load(sidecar.sidecar_path(path))

        # A document that still carries inline markup renders correctly without
        # needing `extract` first; the sidecar just takes precedence.
        cleaned, inline = obsidian_inline.extract(markdown)
        known = {a.id for a in annotations}
        annotations.extend(a for a in inline if a.id not in known)

        result = render_document(
            cleaned,
            annotations,
            title=humanize(path.stem),
            doc_id=doc_id,
            tree=tree,
        )
        out = outdir / f"{doc_id}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.html, encoding="utf-8")

        placed = len(annotations) - len(result.unresolved)
        print(f"{doc_id} -> {out}  ({placed}/{len(annotations)} annotations)")

        for miss in result.unresolved:
            failures += 1
            print(f"  ! {miss.annotation.id}: {miss.reason}", file=sys.stderr)

    return 1 if (failures and args.strict) else 0


def cmd_start(args) -> int:
    from . import daemon
    from .serve import refuse_insecure_bind

    # The child would refuse anyway; catching it here gives the reason rather
    # than "the server did not come up".
    refusal = refuse_insecure_bind(args.host)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 1

    if not args.indir.is_dir():
        print(f"error: no markdown directory at {args.indir}", file=sys.stderr)
        print("  pass -i, or set MDWEAVE_CONTENTS to the knowledge_base checkout",
              file=sys.stderr)
        return 1

    # Render before touching the server. The pages are plain files on disk, so
    # this picks up anything added by hand since the last run whether or not a
    # server is already up.
    if not args.no_build:
        build = argparse.Namespace(input=args.indir, outdir=args.outdir, strict=False)
        if cmd_build(build) != 0:
            return 1

    running = daemon.health(args.host, args.port)
    if running and daemon.is_stale(running):
        print("mdweave: the running server is older than the code on disk, restarting")
        daemon.stop(args.port, host=args.host)
        running = None

    if running:
        print(f"mdweave: already serving on port {args.port}")
    else:
        process = daemon.spawn(args.indir, args.outdir, args.host, args.port)
        if daemon.wait_until_up(process, args.host, args.port) is None:
            print(f"error: the server did not come up on port {args.port}",
                  file=sys.stderr)
            print(f"  log: {daemon.logfile(args.port)}", file=sys.stderr)
            return 1

    url = f"http://{args.host}:{args.port}/"
    print(url)
    if args.no_open or not daemon.open_page(url):
        print(f"  no browser here -- forward port {args.port} and open that URL")
    return 0


def cmd_stop(args) -> int:
    from . import daemon

    pid = daemon.stop(args.port, host=args.host)
    if pid is None:
        print(f"mdweave: nothing running on port {args.port}")
    else:
        print(f"mdweave: stopped the server on port {args.port} (pid {pid})")
    return 0


def cmd_serve(args) -> int:
    from .serve import refuse_insecure_bind, serve

    # Before anything else, including the build: if this bind is not going to
    # be allowed, say so now rather than after rendering the whole set.
    refusal = refuse_insecure_bind(args.host)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 1

    if not args.indir.is_dir():
        print(f"error: {args.indir} is not a directory", file=sys.stderr)
        return 1

    if not args.no_build:
        build = argparse.Namespace(input=args.indir, outdir=args.outdir, strict=False)
        if cmd_build(build) != 0:
            return 1

    return serve(args.indir, args.outdir, host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
