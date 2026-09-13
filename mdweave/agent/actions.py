"""What the buttons are.

An action is a name, a label, and a prompt with `{document}` and
`{instruction}` in it. Both halves read the same registry, and it lives in the
*knowledge base* -- `markdown_inputs/.mdweave-agent.json`, beside
`.mdweave-theme.json` and for the same reason. Both machines already have that
checkout, so adding a button is a commit to your notes rather than a redeploy
of the tool, which is what makes a new one cheap enough to be worth trying.

A missing or mangled file falls back to what ships here, so a broken dotfile
costs a button and never a document.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REGISTRY = ".mdweave-agent.json"

SCHEMA_VERSION = 1


@dataclass
class Action:
    name: str
    label: str
    prompt: str
    #  Whether the dialog asks for anything. A "summarise this" button has
    #  nothing to type; "ask Claude to..." is all typing.
    needs_instruction: bool = True
    placeholder: str = ""
    #  `oneshot` runs `claude -p --resume` and exits. `warm` keeps a
    #  `claude --bg` agent resident for the document, which is the one you can
    #  `claude attach` into from a terminal.
    mode: str = "oneshot"

    def render(self, document: str, instruction: str) -> str:
        """Fill the template. Missing braces are the author's, not a crash."""
        try:
            return self.prompt.format(document=document, instruction=instruction)
        except (KeyError, IndexError):
            return f"{self.prompt}\n\nDocument: {document}\n\n{instruction}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "needs_instruction": self.needs_instruction,
            "placeholder": self.placeholder,
            "mode": self.mode,
        }


# The prompts say where the document is and that the answer is an edit, not a
# reply: a session driven from a button has nobody reading its stdout, so prose
# it prints instead of writing is prose that is lost.
_PREAMBLE = (
    "You are co-writing a knowledge base with me. The document is "
    "`markdown_inputs/{document}.md`, relative to the knowledge base checkout "
    "you are running in.\n\n"
)

DEFAULTS = [
    Action(
        name="revise",
        label="Ask Claude",
        placeholder="What should change?",
        prompt=_PREAMBLE
        + "Do this to it:\n\n{instruction}\n\n"
        "Edit the file directly. Keep my voice and formatting conventions. "
        "Do not add a summary of your changes to the document itself. When you "
        "are done, reply with one sentence saying what you changed.",
    ),
    Action(
        name="probe",
        label="Probe the session",
        placeholder="What do you want to know?",
        prompt=_PREAMBLE
        + "Answer this about the document, from what you already know of it "
        "and this conversation:\n\n{instruction}\n\n"
        "Do not edit the file. Reply in a few sentences.",
    ),
    Action(
        name="tighten",
        label="Tighten the prose",
        needs_instruction=False,
        prompt=_PREAMBLE
        + "Tighten the prose. Cut hedging and repetition, keep every claim and "
        "all the structure, and change nothing about what the document says. "
        "Edit the file directly, then reply with one sentence on what you cut.",
    ),
]


def load(inputs: Path) -> list[Action]:
    """The registry for this knowledge base."""
    path = inputs / REGISTRY
    if not path.exists():
        return list(DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return list(DEFAULTS)

    raw = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        return list(DEFAULTS)

    actions = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        prompt = str(entry.get("prompt") or "").strip()
        if not name or not prompt:
            continue
        actions.append(
            Action(
                name=name,
                label=str(entry.get("label") or name),
                prompt=prompt,
                needs_instruction=bool(entry.get("needs_instruction", True)),
                placeholder=str(entry.get("placeholder") or ""),
                mode=str(entry.get("mode") or "oneshot"),
            )
        )
    return actions or list(DEFAULTS)


def find(inputs: Path, name: str) -> Action | None:
    for action in load(inputs):
        if action.name == name:
            return action
    return None


def write_default(inputs: Path) -> Path:
    """Drop the shipped registry into the knowledge base, to be edited."""
    path = inputs / REGISTRY
    payload = {
        "version": SCHEMA_VERSION,
        "actions": [
            {
                "name": a.name,
                "label": a.label,
                "needs_instruction": a.needs_instruction,
                "placeholder": a.placeholder,
                "mode": a.mode,
                "prompt": a.prompt,
            }
            for a in DEFAULTS
        ],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path
