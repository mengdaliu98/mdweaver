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
# these, and not a legacy alias below, is treated as a raw CSS colour and
# emitted as an inline custom property.
COLOR_TOKENS = ("pink", "purple", "blue", "green", "yellow", "orange")
DEFAULT_COLOR = "yellow"

# The palette these six replaced. Sidecars written before the change still
# carry the old names, and a render is no place to rewrite a user's .ann.json
# -- so the old names are resolved on the way out and the files are left alone.
#
# Six names onto six tokens, one each, which is worth more here than getting
# every hue exactly right: two legacy names sharing a token makes two notes
# that were deliberately different look the same. `amber` gets `orange`, so it
# no longer collides with the notes that were actually written yellow. `slate`
# is still the odd one -- there is no grey to send it to, and every other token
# is spoken for -- but the palette is muted throughout now, so landing on
# yellow reads as a soft sand rather than the shout it used to be.
LEGACY_COLOR_ALIASES = {
    "amber": "orange",
    "rose": "pink",
    "mint": "green",
    "violet": "purple",
    "sky": "blue",
    "slate": "yellow",
}


def resolve_color_token(color: str) -> str | None:
    """The palette token `color` names, or None when it is a raw CSS colour."""
    if color in COLOR_TOKENS:
        return color
    return LEGACY_COLOR_ALIASES.get(color)


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
class Offset:
    """Where a note sits relative to its anchor, in CSS pixels.

    Stored as a displacement rather than an absolute position so a dragged note
    still travels with its highlight when the surrounding text reflows.
    """

    dx: float = 0.0
    dy: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Offset:
        return cls(dx=float(d.get("dx", 0)), dy=float(d.get("dy", 0)))

    def to_dict(self) -> dict[str, float]:
        return {"dx": self.dx, "dy": self.dy}

    def __bool__(self) -> bool:
        return bool(self.dx or self.dy)


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
    # None means "wherever the anchor puts it" -- the default placement.
    offset: Offset | None = None
    # Free-form escape hatch so new features do not require a schema change.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Annotation:
        known = {"id", "target", "kind", "color", "status", "thread", "tags", "offset"}
        raw_offset = d.get("offset")
        offset = Offset.from_dict(raw_offset) if isinstance(raw_offset, dict) else None
        return cls(
            id=d["id"],
            target=TextTarget.from_dict(d["target"]),
            kind=d.get("kind", "comment"),
            color=d.get("color", DEFAULT_COLOR),
            status=d.get("status", "open"),
            thread=[Comment.from_dict(c) for c in d.get("thread", [])],
            tags=list(d.get("tags", [])),
            offset=offset or None,
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
        if self.offset:
            d["offset"] = self.offset.to_dict()
        d.update(self.extra)
        return d

    @property
    def has_card(self) -> bool:
        return self.kind == "comment" and bool(self.thread)

    @property
    def color_token(self) -> str:
        """Token name for the CSS class, or "custom" for a raw CSS colour.

        A legacy token resolves to its replacement here rather than in the
        stored `color`, so `to_dict` still writes back what was read.
        """
        return resolve_color_token(self.color) or "custom"

    @property
    def custom_color(self) -> str | None:
        """The raw CSS colour, when `color` is not a known token."""
        return None if resolve_color_token(self.color) else self.color


_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def safe_id(raw: str) -> str:
    """Make an annotation id usable inside an HTML id/class attribute."""
    cleaned = _ID_SAFE.sub("-", raw).strip("-")
    return cleaned or "ann"
