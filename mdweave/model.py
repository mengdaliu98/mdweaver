"""Canonical annotation model.

Every annotation source (sidecar JSON, Obsidian inline markers, a future web UI)
is normalised into these dataclasses before rendering. Adding a new source means
writing one adapter that emits `Annotation` objects -- nothing downstream changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Colour tokens defined in theme/annotations.css. A colour that is not one of
# these is treated as a raw CSS colour and emitted as an inline custom property.
COLOR_TOKENS = ("amber", "rose", "mint", "sky", "violet", "slate")
DEFAULT_COLOR = "amber"


@dataclass
class Comment:
    """One message in an annotation's thread."""

    body: str
    author: str = "me"
    at: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Comment:
        return cls(body=d.get("body", ""), author=d.get("author", "me"), at=d.get("at"))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"author": self.author, "body": self.body}
        if self.at:
            d["at"] = self.at
        return d


@dataclass
class TextTarget:
    """A W3C-style TextQuoteSelector.

    `quote` is the highlighted text. `prefix`/`suffix` are the surrounding
    characters used to disambiguate when the quote appears more than once.
    `occurrence` picks the nth surviving match (0-based) as a last resort.
    """

    quote: str
    prefix: str = ""
    suffix: str = ""
    occurrence: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TextTarget:
        return cls(
            quote=d["quote"],
            prefix=d.get("prefix", ""),
            suffix=d.get("suffix", ""),
            occurrence=int(d.get("occurrence", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"quote": self.quote}
        if self.prefix:
            d["prefix"] = self.prefix
        if self.suffix:
            d["suffix"] = self.suffix
        if self.occurrence:
            d["occurrence"] = self.occurrence
        return d


@dataclass
class Annotation:
    """A highlight, optionally carrying a comment thread."""

    id: str
    target: TextTarget
    kind: str = "comment"  # "comment" (highlight + card) | "highlight" (no card)
    color: str = DEFAULT_COLOR
    status: str = "open"  # "open" | "resolved"
    thread: list[Comment] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # Free-form escape hatch so new features do not require a schema change.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Annotation:
        known = {"id", "target", "kind", "color", "status", "thread", "tags"}
        return cls(
            id=d["id"],
            target=TextTarget.from_dict(d["target"]),
            kind=d.get("kind", "comment"),
            color=d.get("color", DEFAULT_COLOR),
            status=d.get("status", "open"),
            thread=[Comment.from_dict(c) for c in d.get("thread", [])],
            tags=list(d.get("tags", [])),
            extra={k: v for k, v in d.items() if k not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "color": self.color,
            "status": self.status,
            "target": self.target.to_dict(),
        }
        if self.thread:
            d["thread"] = [c.to_dict() for c in self.thread]
        if self.tags:
            d["tags"] = self.tags
        d.update(self.extra)
        return d

    @property
    def has_card(self) -> bool:
        return self.kind == "comment" and bool(self.thread)

    @property
    def color_token(self) -> str:
        """Token name for the CSS class, or "custom" for a raw CSS colour."""
        return self.color if self.color in COLOR_TOKENS else "custom"

    @property
    def custom_color(self) -> str | None:
        """The raw CSS colour, when `color` is not a known token."""
        return None if self.color in COLOR_TOKENS else self.color


_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def safe_id(raw: str) -> str:
    """Make an annotation id usable inside an HTML id/class attribute."""
    cleaned = _ID_SAFE.sub("-", raw).strip("-")
    return cleaned or "ann"
