"""Which Claude session owns a document.

Beside the prose, in the convention `.ann.json` already established:

    knowledge_base/markdown_inputs/
      metabridge-design-review.md              <- the prose
      metabridge-design-review.ann.json        <- highlights and comments
      metabridge-design-review.session.json    <- the session that co-writes it

A document and a conversation about it are two different things, and keeping
them in two files is what lets either be replaced without disturbing the other.
It also keeps the markdown clean, which is the whole premise of the sidecar:
frontmatter would put a machine identifier in the reader's prose, and would
shift every `data-src-start` offset in the document by the height of the block.

The file travels with its document. `Workspace.files_of` names it, so a
checkpoint carries it; `move_document` renames it and `delete_document` removes
it, for the same reason the annotations follow -- a session pinned to a path
that no longer exists is a session nobody can reach.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


def session_path(markdown_path: Path) -> Path:
    """`notes.md` -> `notes.session.json`

    Not `with_suffix`, which would turn `notes.md` into `notes.session.json`
    correctly but `weekly.notes.md` into `weekly.notes.session.json` -- fine --
    while `with_suffix` on a stem containing a dot is exactly where the
    annotation sidecar's naming and this one have to agree. They both derive
    from the same stem, so they always will.
    """
    return markdown_path.with_suffix(".session.json")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Session:
    """The Claude conversation that owns one document.

    `session_id` is absent until the first run: the id is minted by Claude
    itself, and inventing one here would mean resuming a conversation that has
    never existed. The first job on a document therefore starts fresh and
    records what came back.

    `warm` promotes the document to a resident `claude --bg` agent, which is
    the one you can `claude attach` into from a terminal. Off by default --
    a background process per document is a cost worth opting into.
    """

    session_id: str | None = None
    cwd: str | None = None
    warm: bool = False
    agent_id: str | None = None  # the short id `claude --bg` prints, when warm
    updated: str | None = None
    turns: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = dict(self.extra)
        payload.update(
            {
                "version": SCHEMA_VERSION,
                "session_id": self.session_id,
                "cwd": self.cwd,
                "warm": self.warm,
                "agent_id": self.agent_id,
                "updated": self.updated,
                "turns": self.turns,
            }
        )
        return {k: v for k, v in payload.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        known = {
            "version",
            "session_id",
            "cwd",
            "warm",
            "agent_id",
            "updated",
            "turns",
        }
        return cls(
            session_id=data.get("session_id") or None,
            cwd=data.get("cwd") or None,
            warm=bool(data.get("warm")),
            agent_id=data.get("agent_id") or None,
            updated=data.get("updated") or None,
            turns=int(data.get("turns") or 0),
            # Preserved verbatim through a round trip, same as an annotation's
            # unrecognised fields: a new field should not need a migration.
            extra={k: v for k, v in data.items() if k not in known},
        )


def load(path: Path) -> Session:
    """The session recorded for a document, or an empty one.

    A missing file is the normal state of a document nobody has asked Claude
    about yet, and a mangled one is not worth taking the page down for -- both
    answer "no session", which starts a fresh conversation.
    """
    if not path.exists():
        return Session()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return Session()
    if not isinstance(data, dict):
        return Session()
    return Session.from_dict(data)


def save(path: Path, session: Session) -> None:
    session.updated = now()
    path.write_text(
        json.dumps(session.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
