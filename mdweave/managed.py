"""Documents adopted one at a time, from anywhere on this machine.

The knowledge base is a directory, and its layout is the navigation. This is
the other arrangement: a list of absolute paths, each added by hand with
`mdweave serve /path/to/file.md`, and a sidebar showing the least of the
filesystem needed to hold them all.

A document's id is its absolute path without the leading slash and the `.md`,
so `/home/me/proj/docs/design.md` is `home/me/proj/docs/design`. That is
longer than an id relative to the folder the files share, and the length is
the point: the shared folder moves every time a file from somewhere else is
added, and an id that moved with it would rename every page, every label and
every link that hung off the old one. The sidebar strips the shared part for
display instead -- see `common_folder` -- so the reader still sees the
minimal tree while nothing underneath it ever changes name.

Nothing is written beside the documents except their own `.ann.json`. The
list, the rendered pages and the settings the knowledge base keeps as
dotfiles in `markdown_inputs/` all live in one directory of mdweave's own,
`~/.mdweave` unless `MDWEAVE_HOME` says otherwise: a file adopted from a
codebase should not grow a `.mdweave-theme.json` next to it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME_ENV = "MDWEAVE_HOME"
REGISTRY = "managed.json"


def home() -> Path:
    """Where the list, the pages and the settings live."""
    override = os.environ.get(HOME_ENV)
    return Path(override).expanduser() if override else Path.home() / ".mdweave"


def outputs(root: Path) -> Path:
    return root / "html_outputs"


def absolute(raw: str | Path) -> Path:
    """An absolute, normalised path -- without following links.

    `resolve` would follow them, and a document adopted through a link should
    be listed where the reader said it was, not where the link happens to
    point today.
    """
    return Path(os.path.abspath(os.path.expanduser(str(raw))))


def check(raw: str | Path) -> Path:
    """The path to adopt, or ValueError saying why it cannot be.

    The same checks `link_document` makes before writing a link, and for the
    same reason: everything is tested *through* any link, because a path that
    resolves to nothing is the shape that gets accepted once and breaks
    something else later.
    """
    path = absolute(raw)
    if not path.exists():
        raise ValueError(f"there is nothing at {str(path)!r}")
    if not path.is_file():
        raise ValueError(f"{str(path)!r} is not a regular file")
    if not path.name.lower().endswith(".md"):
        raise ValueError("only .md files can be managed")
    if not os.access(path, os.R_OK):
        raise ValueError(f"{str(path)!r} cannot be read")
    return path


def load(root: Path) -> list[Path]:
    """Every path on the list, in the order they were added.

    Missing or mangled means an empty list rather than an error, the policy
    every dotfile here follows: a broken file must cost the list, never the
    server.
    """
    try:
        data = json.loads((root / REGISTRY).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    found: list[Path] = []
    for entry in raw:
        if isinstance(entry, str) and entry.startswith("/"):
            path = absolute(entry)
            if path not in found:
                found.append(path)
    return found


def save(root: Path, paths: list[Path]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / REGISTRY).write_text(
        json.dumps({"documents": [str(p) for p in paths]}, indent=2) + "\n",
        encoding="utf-8",
    )


def add(root: Path, raw: str | Path) -> tuple[Path, bool]:
    """Put a file on the list. (path, whether it was new)."""
    path = check(raw)
    paths = load(root)
    if path in paths:
        return path, False
    save(root, [*paths, path])
    return path, True


def remove(root: Path, raw: str | Path) -> bool:
    """Take a file off the list. The file itself is never touched."""
    path = absolute(raw)
    paths = load(root)
    if path not in paths:
        return False
    save(root, [p for p in paths if p != path])
    return True


def doc_id(path: Path) -> str:
    """The id of a managed file: its absolute path, less the slash and suffix."""
    return path.with_suffix("").as_posix().lstrip("/")


def common_folder(doc_ids) -> str:
    """The deepest folder holding every document, as a sidebar path.

    This is what the sidebar leaves out. `root/A/a/one` and `root/A/b/two`
    share `root/A`, so the tree starts at `a` and `b`; add `root/B/b/three`
    and they share only `root`, so `A` and `B` appear above them. A single
    document shares its own folder with itself, and the tree is that one row.
    """
    common: list[str] | None = None
    for doc in doc_ids:
        parts = doc.split("/")[:-1]
        if common is None:
            common = parts
            continue
        at = 0
        while at < min(len(common), len(parts)) and common[at] == parts[at]:
            at += 1
        common = common[:at]
    return "/".join(common or [])
