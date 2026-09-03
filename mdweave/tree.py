"""The document tree behind the sidebar.

A document is identified by its path under the markdown root, without the `.md`
suffix -- `notes/weekly.md` becomes the id `notes/weekly`. That id is the key
everywhere: the sidebar link, the output filename, and the `document` field the
API takes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


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


def build_tree(doc_ids: list[str]) -> list[Node]:
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

    _sort(root)
    return root.children


def _sort(node: Node) -> None:
    """Folders first, then documents, each alphabetical -- as VS Code shows it."""
    node.children.sort(key=lambda c: (not c.is_dir, c.label.lower()))
    for child in node.children:
        _sort(child)


def relative_prefix(doc_id: str) -> str:
    """How far a page at `doc_id` has to climb to reach the output root."""
    return "../" * doc_id.count("/")
