"""mdweave command line interface.

    mdweave extract  <doc.md>            pull Obsidian inline comments -> sidecar JSON
    mdweave build    <doc.md> -o <dir>   render annotated HTML + CSS
    mdweave build    <dir>    -o <dir>   render every .md in a directory
    mdweave serve    [--port N]          serve the output and accept new comments
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .render import render_document, write_assets
from .sources import obsidian_inline, sidecar

DEFAULT_INDIR = Path("contents/markdown_inputs")
DEFAULT_OUTDIR = Path("contents/html_outputs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mdweave",
        description="Render annotated markdown to HTML + CSS.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

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
        "-i", "--indir", type=Path, default=DEFAULT_INDIR, help="markdown directory"
    )
    p_serve.add_argument(
        "-o", "--outdir", type=Path, default=DEFAULT_OUTDIR, help="output directory"
    )
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument(
        "--no-build",
        action="store_true",
        help="serve what is already on disk instead of rebuilding first",
    )

    args = parser.parse_args(argv)

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
    inputs = _collect(args.input)
    if not inputs:
        print(f"error: no markdown found at {args.input}", file=sys.stderr)
        return 1

    outdir: Path = args.outdir or DEFAULT_OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)
    write_assets(outdir)

    failures = 0
    for path in inputs:
        markdown = path.read_text(encoding="utf-8")
        annotations = sidecar.load(sidecar.sidecar_path(path))

        # A document that still carries inline markup renders correctly without
        # needing `extract` first; the sidecar just takes precedence.
        cleaned, inline = obsidian_inline.extract(markdown)
        known = {a.id for a in annotations}
        annotations.extend(a for a in inline if a.id not in known)

        result = render_document(
            cleaned, annotations, title=path.stem, doc_id=path.stem
        )
        target = outdir / f"{path.stem}.html"
        target.write_text(result.html, encoding="utf-8")

        placed = len(annotations) - len(result.unresolved)
        print(f"{path.name} -> {target}  ({placed}/{len(annotations)} annotations)")

        for miss in result.unresolved:
            failures += 1
            print(f"  ! {miss.annotation.id}: {miss.reason}", file=sys.stderr)

    return 1 if (failures and args.strict) else 0


def cmd_serve(args) -> int:
    from .serve import serve

    if not args.indir.is_dir():
        print(f"error: {args.indir} is not a directory", file=sys.stderr)
        return 1

    if not args.no_build:
        build = argparse.Namespace(input=args.indir, outdir=args.outdir, strict=False)
        if cmd_build(build) != 0:
            return 1

    return serve(args.indir, args.outdir, host=args.host, port=args.port)


def _collect(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.glob("*.md"))
    return [target] if target.exists() else []


if __name__ == "__main__":
    raise SystemExit(main())
