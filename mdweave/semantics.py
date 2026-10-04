"""What *kind* of remark a comment is, and what Claude should do about it.

A comment says what is wrong. A semantic type says how that sort of thing is
usually put right. Separating them is what makes a comment worth sending to a
model: "I don't follow this" plus "questions are answered in a footnote, not by
rewriting the sentence" is an instruction, where either half alone is not.

The types are the reader's own -- there is no shipped vocabulary, because the
useful set is particular to how somebody writes. A type is a name and,
optionally, an instruction:

    markdown_inputs/.mdweave-semantics.json
    {
      "version": 1,
      "types": [
        {"name": "question", "instruction": "Answer in a footnote rather than
                                             by rewriting the sentence."},
        {"name": "too long"}
      ],
      "defaults": {"3": "question"}
    }

`defaults` maps a color *slot* to a type name, so picking the color you
always use for questions attaches the type without a second gesture. Neither
half is required: a color need not have a default, and a type need not have an
instruction -- an unexplained type still groups comments usefully, and still
tells Claude these three remarks are the same kind of thing.

Beside the prose rather than beside the tool, like every other dotfile here:
the vocabulary belongs to the knowledge base and should follow it to the next
machine. Same failure policy too -- missing, unreadable or mangled all mean
"no types", because a broken dotfile must cost a feature and never a document.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SEMANTICS_FILE = ".mdweave-semantics.json"
SCHEMA_VERSION = 1

# Long enough to be a phrase, short enough to stay a pill in the composer.
MAX_NAME = 40
MAX_INSTRUCTION = 2000


@dataclass
class SemanticType:
    name: str
    instruction: str = ""

    def to_dict(self) -> dict:
        d: dict = {"name": self.name}
        if self.instruction:
            d["instruction"] = self.instruction
        return d


def _clean(raw, limit: int) -> str:
    return " ".join(str(raw or "").split())[:limit].strip()


@dataclass
class Semantics:
    """The reader's types, and which color reaches for which."""

    types: list[SemanticType]
    # Slot number (1..6) -> type name. Keyed by int here and by the string the
    # JSON had to use on disk.
    defaults: dict[int, str]

    def named(self, name: str) -> SemanticType | None:
        wanted = _clean(name, MAX_NAME).casefold()
        for kind in self.types:
            if kind.name.casefold() == wanted:
                return kind
        return None

    def default_for(self, slot: int | None) -> str | None:
        """The type a freshly picked color attaches, if the reader set one."""
        if slot is None:
            return None
        name = self.defaults.get(slot)
        return name if name and self.named(name) else None

    def instruction_for(self, name: str | None) -> str:
        """What Claude is told about this kind of remark. "" if nothing."""
        if not name:
            return ""
        kind = self.named(name)
        return kind.instruction if kind else ""

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "types": [t.to_dict() for t in self.types],
            # Keys stringified because JSON object keys are strings; `load`
            # turns them back. Only entries naming a type that still exists are
            # written, so deleting a type cleans up after itself.
            "defaults": {
                str(slot): name
                for slot, name in sorted(self.defaults.items())
                if self.named(name)
            },
        }


def empty() -> Semantics:
    return Semantics(types=[], defaults={})


def load(root: Path) -> Semantics:
    try:
        data = json.loads((root / SEMANTICS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return empty()
    if not isinstance(data, dict):
        return empty()

    types: list[SemanticType] = []
    seen: set[str] = set()
    for entry in data.get("types") or []:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name"), MAX_NAME)
        # Names are the identity here -- they key `defaults` and they are what
        # a comment stores -- so two differing only in case would be two types
        # the reader cannot tell apart.
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        types.append(
            SemanticType(name=name, instruction=_clean(entry.get("instruction"), MAX_INSTRUCTION))
        )

    defaults: dict[int, str] = {}
    raw_defaults = data.get("defaults")
    if isinstance(raw_defaults, dict):
        for slot, name in raw_defaults.items():
            try:
                number = int(slot)
            except (TypeError, ValueError):
                continue
            cleaned = _clean(name, MAX_NAME)
            if cleaned:
                defaults[number] = cleaned

    return Semantics(types=types, defaults=defaults)


def save(root: Path, semantics: Semantics) -> None:
    (root / SEMANTICS_FILE).write_text(
        json.dumps(semantics.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def from_payload(payload: dict) -> Semantics:
    """Parse what the settings window sends. Raises ValueError on nonsense."""
    raw = payload.get("types")
    if not isinstance(raw, list):
        raise ValueError("'types' must be a list")

    types: list[SemanticType] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("each type must be an object")
        name = _clean(entry.get("name"), MAX_NAME)
        if not name:
            raise ValueError("a type needs a name")
        if name.casefold() in seen:
            raise ValueError(f"two types are both called {name!r}")
        seen.add(name.casefold())
        types.append(
            SemanticType(name=name, instruction=_clean(entry.get("instruction"), MAX_INSTRUCTION))
        )

    defaults: dict[int, str] = {}
    raw_defaults = payload.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        raise ValueError("'defaults' must be an object")
    for slot, name in raw_defaults.items():
        try:
            number = int(slot)
        except (TypeError, ValueError):
            raise ValueError(f"{slot!r} is not a color slot")
        cleaned = _clean(name, MAX_NAME)
        if cleaned:
            defaults[number] = cleaned

    return Semantics(types=types, defaults=defaults)
