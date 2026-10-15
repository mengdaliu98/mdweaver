"""mdweave command line interface.

The two commands for everyday use:

    mdweave start                        render everything, serve it, open it
    mdweave stop                         shut the server down
    mdweave serve <file.md> [--monolithic]
                                         add one file to the managed list, serve
                                         the list, and print the file's link

The rest are the pieces those are built from, useful on their own:

    mdweave extract  <doc.md>            pull Obsidian inline comments -> sidecar JSON
    mdweave build    <doc.md> -o <dir>   render annotated HTML + CSS
    mdweave build    <dir>    -o <dir>   render every .md in a directory
    mdweave serve    [--port N]          serve in the foreground, no browser
    mdweave managed  [--remove <file>]   list the managed files, or take one off
    mdweave fingerprint                  hash of the installed source, for staleness checks
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import scheme as schemes
from .render import render_document, write_assets
from .sources import obsidian_inline, sidecar
from .tree import (
    broken_links,
    build_tree,
    document_ids,
    folder_paths,
    humanize,
    load_order,
)


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
        "path",
        nargs="?",
        type=Path,
        default=None,
        help="a .md file to add to the managed list; serves that list instead "
        "of the knowledge base, and prints the file's link",
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
    p_serve.add_argument(
        "--monolithic",
        action="store_true",
        help="also run the agent runner in this process, against this server",
    )
    p_serve.add_argument(
        "--model", default=None, help="model for the sessions (with --monolithic)"
    )
    p_serve.add_argument(
        "--permission-mode",
        default="bypassPermissions",
        help="what the sessions may do (with --monolithic)",
    )

    p_managed = sub.add_parser(
        "managed", help="list the files `mdweave serve <path>` has added"
    )
    p_managed.add_argument(
        "--remove",
        type=Path,
        default=None,
        metavar="PATH",
        help="take a file off the list; the file itself is untouched",
    )
    p_managed.add_argument("--port", type=int, default=8765)
    p_managed.add_argument("--host", default="127.0.0.1")

    p_rec = sub.add_parser(
        "reconcile",
        help="merge what the remote has, when a push says the branch has moved on",
    )
    p_rec.add_argument(
        "-i", "--indir", type=Path, default=default_indir(), help="markdown directory"
    )
    p_rec.add_argument(
        "-o", "--outdir", type=Path, default=default_outdir(), help="output directory"
    )

    p_agent = sub.add_parser(
        "agent",
        help="run Claude sessions here, driven by buttons on the deployed site",
    )
    p_agent.add_argument(
        "-i", "--indir", type=Path, default=default_indir(), help="markdown directory"
    )
    p_agent.add_argument(
        "-o", "--outdir", type=Path, default=default_outdir(), help="output directory"
    )
    p_agent.add_argument(
        "--remote", default="", help="the deployed site, e.g. https://x.up.railway.app"
    )
    p_agent.add_argument("--name", default="", help="how to identify this machine")
    p_agent.add_argument("--model", default=None, help="model for the sessions")
    p_agent.add_argument(
        "--permission-mode",
        default="bypassPermissions",
        help="what the sessions may do (default: bypassPermissions)",
    )
    p_agent.add_argument(
        "--add-dir",
        action="append",
        default=[],
        help="another directory the sessions may touch; repeatable",
    )
    p_agent.add_argument(
        "--once", action="store_true", help="take at most one job, then exit"
    )
    p_agent.add_argument("--verbose", action="store_true")

    p_actions = sub.add_parser(
        "actions", help="list the agent buttons this knowledge base offers"
    )
    p_actions.add_argument(
        "-i", "--indir", type=Path, default=default_indir(), help="markdown directory"
    )
    p_actions.add_argument(
        "--write-default",
        action="store_true",
        help="write the shipped set into the knowledge base, to be edited",
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
    if args.command == "managed":
        return cmd_managed(args)
    if args.command == "reconcile":
        return cmd_reconcile(args)
    if args.command == "agent":
        return cmd_agent(args)
    if args.command == "actions":
        return cmd_actions(args)
    return cmd_build(args)


def cmd_agent(args) -> int:
    """Take jobs from the deployed site and run them here.

    The site cannot call this machine -- there is no address to call -- so the
    traffic all goes the other way: this asks for work, and the request is held
    open until there is some.
    """
    from .agent.daemon import Runner, from_env

    config = from_env(args)
    if config is None:
        return 1
    try:
        return Runner(config).loop()
    except KeyboardInterrupt:
        print("\nmdweave-agent: stopped")
        return 0


def cmd_actions(args) -> int:
    from .agent import actions as registry

    if args.write_default:
        path = registry.write_default(args.indir)
        print(f"mdweave: wrote {path}")
        return 0

    for action in registry.load(args.indir):
        asks = "asks for an instruction" if action.needs_instruction else "one press"
        print(f"{action.name:12} {action.label:24} ({asks})")
    return 0


def cmd_reconcile(args) -> int:
    """Bring the checkout in line with the remote, keeping both sides.

    The recovery for "two machines wrote to one branch". The container runs it
    at boot, where the alternative -- the `pull --ff-only` this replaced -- was
    to abort and then serve an old commit for the rest of the deployment.
    Exits 0 when there was nothing to take, so a boot script can just call it.
    """
    from . import checkpoint as git

    try:
        repo = git.repo_root(args.indir)
        merged = git.reconcile(repo, args.outdir)
    except git.GitError as exc:
        print(f"mdweave: {exc}", file=sys.stderr)
        return 1

    print("mdweave: merged the remote" if merged else "mdweave: already up to date")
    return 0


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
    # From beside the documents, not the shipped default: this runs on every
    # `mdweave start` and every container boot, and writing the default here
    # silently reverted the reader's colors.
    write_assets(outdir, schemes.load(root).current())

    for missing in broken_links(root):
        print(
            f"warning: {missing}.md is a symlink to nothing here -- skipped",
            file=sys.stderr,
        )

    failures = 0
    for doc_id, path in selected.items():
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError as exc:
            # One unreadable file must not cost the other thirty-five. The
            # loop already counts failures; this one was simply outside it.
            print(f"error: {doc_id}: {exc}", file=sys.stderr)
            failures += 1
            continue
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

    if args.path is not None:
        return cmd_serve_managed(args)

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

    return serve(
        args.indir,
        args.outdir,
        host=args.host,
        port=args.port,
        monolithic=args.monolithic,
        model=args.model,
        permission_mode=args.permission_mode,
    )



def _managed_server(host: str, port: int) -> tuple[dict | None, str]:
    """The managed server already on this port, or why that port is no good."""
    from . import daemon

    running = daemon.health(host, port)
    if running is None:
        return None, ""
    if not running.get("managed"):
        # Adding to it would make a symlink in the knowledge base, which is
        # a different thing entirely from what was asked.
        return None, (
            f"port {port} is serving the knowledge base, not managed documents\n"
            "  pass --port to serve them somewhere else"
        )
    return running, ""


def cmd_serve_managed(args) -> int:
    """Add one file to the managed list, and say where to read it.

    If a managed server is already up on this port it is handed the file and
    this returns at once: the list is one per machine, so a second server
    would only be a second view of it on a port nobody is looking at. If
    nothing is listening, this becomes the server, in the foreground, the way
    `serve` always has.
    """
    from . import daemon, managed
    from .serve import ManagedWorkspace, document_url, refuse_insecure_bind, serve

    refusal = refuse_insecure_bind(args.host)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 1

    try:
        path = managed.check(args.path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    doc = managed.doc_id(path)

    running, refused = _managed_server(args.host, args.port)
    if refused:
        print(f"error: {refused}", file=sys.stderr)
        return 1

    if running:
        try:
            daemon.post(args.host, args.port, "/api/documents/link", {"path": str(path)})
        except RuntimeError as exc:
            print(f"error: the server on port {args.port} refused {path}: {exc}",
                  file=sys.stderr)
            return 1
        if daemon.is_stale(running):
            print("mdweave: note -- the running server is older than the code on "
                  "disk; restart it to pick the change up", file=sys.stderr)
        print(f"mdweave: managing {path} (already serving on port {args.port})")
        print(document_url(args.host, args.port, doc))
        return 0

    home = managed.home()
    _, added = managed.add(home, path)
    print(f"mdweave: {'now managing' if added else 'already managing'} {path}")

    workspace = ManagedWorkspace(
        inputs=Path("/"), outputs=managed.outputs(home), home=home
    )
    for gone in workspace.missing():
        print(f"warning: {gone} is on the list but not on disk -- skipped",
              file=sys.stderr)
    if not args.no_build:
        workspace.rebuild_all()

    return serve(
        workspace.inputs,
        workspace.outputs,
        host=args.host,
        port=args.port,
        monolithic=args.monolithic,
        model=args.model,
        permission_mode=args.permission_mode,
        workspace=workspace,
        focus=doc,
    )


def cmd_managed(args) -> int:
    """List the managed files, or take one off the list."""
    from . import daemon, managed

    home = managed.home()
    if args.remove is None:
        paths = managed.load(home)
        if not paths:
            print("mdweave: nothing managed yet -- add a file with "
                  "`mdweave serve <path>`")
        for path in paths:
            print(path if path.is_file() else f"{path}  (missing)")
        return 0

    path = managed.absolute(args.remove)
    if path not in managed.load(home):
        print(f"error: {path} is not on the list", file=sys.stderr)
        return 1

    # Through the server when one is up, so every page's sidebar loses the
    # row now rather than at the next restart.
    running, _ = _managed_server(args.host, args.port)
    if running and path.is_file():
        try:
            daemon.post(args.host, args.port, "/api/documents/delete",
                        {"document": managed.doc_id(path)})
        except RuntimeError as exc:
            print(f"error: the server on port {args.port} refused: {exc}",
                  file=sys.stderr)
            return 1
    else:
        managed.remove(home, path)
    print(f"mdweave: no longer managing {path} (the file is untouched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
