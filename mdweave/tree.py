"""The document tree behind the sidebar.

A document is identified by its path under the markdown root, without the `.md`
suffix -- `notes/weekly.md` becomes the id `notes/weekly`. That id is the key
everywhere: the sidebar link, the output filename, and the `document` field the
API takes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Where a hand-arranged tree is remembered. A dotfile at the markdown root, so
# it travels with the documents in the contents repo -- and so `document_ids`
# never sees it: it only looks at `*.md`, and skips dotted paths on top of that.
ORDER_FILE = ".mdweave-order.json"


def humanize(name: str) -> str:
    """Turn a file or folder name into a readable label.

    Sentence case: both separators become spaces, the first letter is
    capitalised, and the rest is lowered.

        system_for_bio_literature_research -> System for bio literature research
        ome-zarr-layout-planner            -> Ome zarr layout planner

    Hyphens read as word separators here, not as part of a term, so `OME-Zarr`
    in a filename comes out as "Ome zarr". Acronyms lose their capitals with
    it; a label is a label, and the document keeps its own title.
    """
    text = " ".join(name.replace("_", " ").replace("-", " ").split())
    return text[:1].upper() + text[1:].lower() if text else name


# Characters that are unsafe in a filename, a URL, or both.
_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def safe_document_name(raw: str) -> str:
    """Reduce an uploaded filename to one safe to write and to link to.

    Everything before the last separator is discarded, so a client cannot steer
    the write out of the markdown root no matter what it sends. Whitespace
    becomes underscores to match the naming already in use.

    Raises ValueError if the name is not markdown, or has nothing usable left.
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name.lower().endswith(".md"):
        raise ValueError("only .md files can be imported")

    stem = "_".join(_UNSAFE.sub("", name[: -len(".md")]).split())
    stem = stem.strip("._")
    if not stem:
        raise ValueError(f"{raw!r} leaves no usable filename")
    return stem + ".md"


@dataclass
class Node:
    """One row in the sidebar: a folder, or a document."""

    name: str  # the raw path segment
    label: str  # what the sidebar shows
    is_dir: bool
    doc_id: str | None = None  # files only
    children: list[Node] = field(default_factory=list)


def document_ids(root: Path) -> dict[str, Path]:
    """Every markdown file under `root`, keyed by document id."""
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found

    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        # Skip dotted directories -- .obsidian and friends are not documents.
        if any(part.startswith(".") for part in relative.parts):
            continue
        found[relative.with_suffix("").as_posix()] = path
    return found


def load_order(root: Path) -> dict[str, list[str]]:
    """The hand-arranged order, keyed by folder path -- `""` being the root.

    Absent, unreadable, or hand-mangled all mean the same thing: fall back to
    the alphabetical sort. A broken dotfile must never take the sidebar with
    it, so anything that is not a list of strings is dropped on the floor.
    """
    path = root / ORDER_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(folder): [n for n in names if isinstance(n, str)]
        for folder, names in data.items()
        if isinstance(names, list)
    }


def save_order(root: Path, order: dict[str, list[str]]) -> None:
    """Write the hand-arranged order back, or remove it once nothing is left."""
    path = root / ORDER_FILE
    trimmed = {folder: names for folder, names in order.items() if names}
    if not trimmed:
        path.unlink(missing_ok=True)
        return
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(trimmed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build_tree(doc_ids: list[str], order: dict[str, list[str]] | None = None) -> list[Node]:
    """Assemble a nested tree from flat document ids."""
    root = Node(name="", label="", is_dir=True)

    for doc_id in sorted(doc_ids):
        parts = doc_id.split("/")
        cursor = root
        for depth, part in enumerate(parts):
            is_file = depth == len(parts) - 1
            # Match on name *and* kind: a folder and a document may share a name.
            match = next(
                (c for c in cursor.children if c.name == part and c.is_dir != is_file),
                None,
            )
            if match is None:
                match = Node(
                    name=part,
                    label=humanize(part),
                    is_dir=not is_file,
                    doc_id=doc_id if is_file else None,
                )
                cursor.children.append(match)
            cursor = match

    _sort(root, order or {}, "")
    return root.children


def _sort(node: Node, order: dict[str, list[str]], path: str) -> None:
    """Hand-arranged first, then folders before documents, then alphabetical.

    A name the reader has never dragged has no rank, so it takes one past the
    end of the list and falls back to the old sort among its own kind. That is
    what keeps a document someone else added from landing in the middle of an
    arrangement it was never part of.
    """
    listed = order.get(path) or []

    def rank(child: Node) -> tuple[int, bool, str]:
        at = listed.index(child.name) if child.name in listed else len(listed)
        return (at, not child.is_dir, child.label.lower())

    node.children.sort(key=rank)
    for child in node.children:
        _sort(child, order, f"{path}/{child.name}" if path else child.name)


def relative_prefix(doc_id: str) -> str:
    """How far a page at `doc_id` has to climb to reach the output root."""
    return "../" * doc_id.count("/")
