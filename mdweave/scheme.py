"""Colour schemes: what the page is painted with, and what a highlight means.

An annotation used to say `"color": "yellow"`, and yellow was a fixed value in
a stylesheet. Once the reader can choose the colours, a hue is the wrong thing
to store: renaming what yellow looks like would silently restyle every note
that ever mentioned it, and switching to a scheme with no yellow in it would
leave those notes with nothing to resolve to.

So an annotation names a **slot** -- first colour, second colour, up to the
sixth -- and a scheme says what the six slots look like. Two consequences,
both of them the point:

* Switching schemes moves every highlight to the same position in the new
  one. Nothing is rewritten; the slot is already what the file says.
* Reordering the slots inside a scheme is the opposite. The colours move, so
  every annotation has to move with them to stay the colour it was, and that
  *is* a rewrite -- see `remap` and its caller.

Six is fixed. It is the width of the picker, the width of the selection menu,
and the number a reader can tell apart at a glance; making it a setting would
be four more states to draw and no more expressive.

The other three values per slot -- the pin, the card, the card's text -- are
derived from the fill rather than chosen. Asking anyone to pick four colours
that stay legible together, six times over, is asking them to do arithmetic;
`derive` does it, and lands on WCAG AA by construction.
"""

from __future__ import annotations

import colorsys
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SLOTS = 6
THEME_FILE = ".mdweave-theme.json"

# Slot classes are positional -- `hl--c1`, never `hl--yellow`. A name would be
# a lie the moment the reader recoloured it.
SLOT_CLASSES = tuple(f"c{i + 1}" for i in range(SLOTS))

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _channel(v: float) -> float:
    v /= 255
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def luminance(color: tuple[int, int, int]) -> float:
    r, g, b = (_channel(c) for c in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(one: tuple[int, int, int], other: tuple[int, int, int]) -> float:
    a, b = luminance(one), luminance(other)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _shift(base: tuple[int, int, int], light: float, sat_scale: float = 1.0):
    h, _, s = colorsys.rgb_to_hls(*[c / 255 for c in base])
    r, g, b = colorsys.hls_to_rgb(h, light, min(1.0, s * sat_scale))
    return tuple(round(c * 255) for c in (r, g, b))


def _darken_until(base, target: float, against, sat_scale: float = 1.0):
    """The same hue, taken down until it clears `target` against `against`."""
    out = base
    for step in range(100):
        out = _shift(base, 0.60 - step * 0.005, sat_scale)
        if contrast(out, against) >= target:
            break
    return out


WHITE = (255, 255, 255)


@dataclass(frozen=True)
class Derived:
    """The four values a slot needs, three of them worked out from the first."""

    bg: tuple[int, int, int]
    edge: tuple[int, int, int]
    note_bg: tuple[int, int, int]
    note_ink: tuple[int, int, int]
    dark_note_bg: tuple[int, int, int]
    dark_note_ink: tuple[int, int, int]


def derive(fill: str) -> Derived:
    """Everything a slot needs, from the one colour that was chosen.

    The edge carries white text as the pin, so it is darkened until it clears
    AA against white. The card is a near-white wash of the same hue with ink
    dark enough to read on it. The dark scheme mirrors both.
    """
    base = _rgb(fill)
    _, _, sat = colorsys.rgb_to_hls(*[c / 255 for c in base])
    note_bg = _shift(base, 0.965, 0.9)
    return Derived(
        bg=base,
        edge=_darken_until(base, 5.0, WHITE, 1.35),
        note_bg=note_bg,
        note_ink=_darken_until(base, 8.0, note_bg, 1.2),
        dark_note_bg=_shift(base, 0.135, 0.85),
        dark_note_ink=_shift(base, 0.80, 1.0),
    )


@dataclass
class Scheme:
    """One named set of colours: the two backgrounds, and the six slots."""

    name: str
    sidebar: str
    paper: str
    colors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "sidebar": self.sidebar,
            "paper": self.paper,
            "colors": list(self.colors),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Scheme:
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("a scheme needs a name")

        colors = raw.get("colors")
        if not isinstance(colors, list) or len(colors) != SLOTS:
            raise ValueError(f"a scheme needs exactly {SLOTS} colours")

        cleaned = []
        for value in [raw.get("sidebar"), raw.get("paper"), *colors]:
            if not isinstance(value, str) or not _HEX.match(value.strip()):
                raise ValueError(f"{value!r} is not a #rrggbb colour")
            cleaned.append(value.strip().lower())

        return cls(name=name, sidebar=cleaned[0], paper=cleaned[1], colors=cleaned[2:])


# The palette the reader already has. Kept as the fallback so a knowledge base
# with no theme file renders exactly as it did before there were schemes.
DEFAULT_SCHEME = Scheme(
    name="Warm paper",
    sidebar="#d1c7b7",
    paper="#f2efe4",
    colors=["#d4b0b5", "#c3b0d4", "#b0b2d4", "#b0d4b1", "#eddf91", "#face98"],
)

# What the six were called when they were hues rather than positions, and the
# palette before that. Sidecars in the wild say these, and a render is no place
# to rewrite someone's file -- so they resolve on the way out, to the slot the
# colour occupied at the time.
LEGACY_SLOTS = {
    "pink": 1, "purple": 2, "blue": 3, "green": 4, "yellow": 5, "orange": 6,
    "rose": 1, "violet": 2, "sky": 3, "mint": 4, "slate": 5, "amber": 6,
}


def slot_of(color) -> int | None:
    """The 1-based slot `color` names, or None if it is a raw CSS colour."""
    if isinstance(color, bool):
        return None
    if isinstance(color, int):
        return color if 1 <= color <= SLOTS else None
    if isinstance(color, str):
        text = color.strip().lower()
        # `c4` is what the picker sends and what the class attribute says; the
        # bare number is what a sidecar holds. Both name the same slot, and a
        # round trip through the browser has to survive either.
        if text.startswith("c") and text[1:].isdigit():
            text = text[1:]
        if text.isdigit():
            n = int(text)
            return n if 1 <= n <= SLOTS else None
        return LEGACY_SLOTS.get(text)
    return None


@dataclass
class Theme:
    """Every scheme the reader has made, and which one is in use."""

    schemes: list[Scheme] = field(default_factory=lambda: [DEFAULT_SCHEME])
    active: str = DEFAULT_SCHEME.name

    def current(self) -> Scheme:
        for scheme in self.schemes:
            if scheme.name == self.active:
                return scheme
        return self.schemes[0] if self.schemes else DEFAULT_SCHEME

    def to_dict(self) -> dict:
        return {"active": self.active, "schemes": [s.to_dict() for s in self.schemes]}


def load(root: Path) -> Theme:
    """The theme beside the documents, or the default one.

    Unreadable, hand-mangled, or missing all mean the same thing: fall back.
    A broken dotfile must not take the whole page down with it -- the reader
    would lose the prose over a colour.
    """
    try:
        raw = json.loads((root / THEME_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Theme()
    if not isinstance(raw, dict):
        return Theme()

    schemes = []
    for entry in raw.get("schemes", []):
        if isinstance(entry, dict):
            try:
                schemes.append(Scheme.from_dict(entry))
            except ValueError:
                continue  # one bad scheme is not a reason to lose the others
    if not schemes:
        return Theme()

    active = raw.get("active")
    known = {s.name for s in schemes}
    return Theme(
        schemes=schemes,
        active=active if isinstance(active, str) and active in known else schemes[0].name,
    )


def save(root: Path, theme: Theme) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / THEME_FILE).write_text(
        json.dumps(theme.to_dict(), indent=2) + "\n", encoding="utf-8"
    )


def _rgb_css(value: tuple[int, int, int]) -> str:
    return f"rgb({value[0]} {value[1]} {value[2]})"


def css(scheme: Scheme) -> str:
    """The scheme as a stylesheet, appended after everything it overrides.

    Generated rather than hand-written, and last in the file, so the static
    themes keep every rule about *structure* and own none of the colour.
    """
    light: list[str] = [
        f"  --paper: {_rgb_css(_rgb(scheme.paper))};",
        f"  --sidebar-bg: {_rgb_css(_rgb(scheme.sidebar))};",
    ]
    dark: list[str] = []
    mapping: list[str] = []

    for index, fill in enumerate(scheme.colors, start=1):
        got = derive(fill)
        slot = f"c{index}"
        light += [
            f"  --hl-{slot}-bg: {_rgb_css(got.bg)};",
            f"  --hl-{slot}-edge: {_rgb_css(got.edge)};",
            f"  --note-{slot}-bg: {_rgb_css(got.note_bg)};",
            f"  --note-{slot}-ink: {_rgb_css(got.note_ink)};",
        ]
        dark += [
            f"    --note-{slot}-bg: {_rgb_css(got.dark_note_bg)};",
            f"    --note-{slot}-ink: {_rgb_css(got.dark_note_ink)};",
        ]
        mapping.append(
            f".hl--{slot}, .note--{slot}, .swatch--{slot} {{"
            f" --hl-bg: var(--hl-{slot}-bg);"
            f" --hl-edge: var(--hl-{slot}-edge);"
            f" --note-bg: var(--note-{slot}-bg);"
            f" --note-ink: var(--note-{slot}-ink); }}"
        )

    body = "\n".join(light)
    dark_body = "\n".join(dark)
    rules = "\n".join(mapping)
    return (
        f"/* Generated from the colour scheme {scheme.name!r}. Edited in the\n"
        f"   browser, under the gear beside DOCUMENTS -- not by hand: the file\n"
        f"   it comes from is markdown_inputs/{THEME_FILE}. */\n"
        f":root {{\n{body}\n}}\n\n"
        f"@media (prefers-color-scheme: dark) {{\n  :root {{\n{dark_body}\n  }}\n}}\n\n"
        f"{rules}\n"
    )


PAGE_INK = (24, 27, 33)


def unreadable(scheme: Scheme) -> list[str]:
    """Slots whose fill leaves body text below AA. Reported, never refused.

    The reader picked it; a warning is help and a veto is not. It reaches the
    dialog so the choice is informed rather than discovered later.
    """
    poor = []
    for index, fill in enumerate(scheme.colors, start=1):
        if contrast(_rgb(fill), PAGE_INK) < 4.5:
            poor.append(f"colour {index} ({fill}) is too dark for the text on it")
    if contrast(_rgb(scheme.paper), PAGE_INK) < 4.5:
        poor.append(f"the page background ({scheme.paper}) is too dark for its text")
    return poor
