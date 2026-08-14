"""Sidecar JSON annotation store -- the canonical format.

Lives next to the markdown as `<doc>.ann.json`:

    {
      "version": 1,
      "annotations": [
        {
          "id": "kxejp",
          "kind": "comment",
          "color": "amber",
          "status": "open",
          "target": {"quote": "CRAM/BAM", "prefix": "...", "suffix": "..."},
          "thread": [{"author": "me", "at": "...", "body": "..."}]
        }
      ]
    }
"""

from __future__ import annotations

import json
from pathlib import Path

from ..model import Annotation

SCHEMA_VERSION = 1


def sidecar_path(markdown_path: Path) -> Path:
    """`notes.md` -> `notes.ann.json`"""
    return markdown_path.with_suffix(".ann.json")


def load(path: Path) -> list[Annotation]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Annotation.from_dict(a) for a in data.get("annotations", [])]


def save(path: Path, annotations: list[Annotation]) -> None:
    payload = {
        "version": SCHEMA_VERSION,
        "annotations": [a.to_dict() for a in annotations],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
