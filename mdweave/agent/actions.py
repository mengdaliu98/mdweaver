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
        }


# The one action that ships. `revise`, `probe` and `tighten` used to sit here
# -- three ways of typing a sentence at a document -- and the comment review
# replaces all of them: the comments *are* the instruction, written in place
# over days, with a semantic type on each saying how that kind of remark is
# meant to be answered. A free-text box beside that is a worse way of saying
# the same thing, and nothing was ever asked of it that a comment could not
# carry better.
#
# The name is reserved: `daemon.py` recognises it and builds the prompt itself,
# because the comment list is not something a `{}` placeholder can hold. What
# stays templated is the framing below, so a knowledge base can reword how it
# talks to Claude without reimplementing any of the machinery.
REVIEW = "review"

# The prompt says where the document is and that the answer is an edit, not a
# reply: a session driven from a button has nobody reading its stdout, so prose
# it prints instead of writing is prose that is lost. It also has to be
# self-contained, because every press gets a session that has never seen this
# document before.
#
# `{document}` is a *location*, not an id -- the caller fills it with the path
# the model should actually open, which for a linked document is the original
# rather than the symlink standing in for it under `markdown_inputs/`.
_REVIEW_PROMPT = (
    "You are co-writing a knowledge base with me. The document is "
    "`{document}`.\n\n"
    "This is a document-iteration request. I have read the document and left "
    "comments on it, and I want each one addressed in the document itself. "
    "Work through every comment listed below: some ask for a change, some ask "
    "a question that the prose should answer, and each one carries a note "
    "saying what kind of remark it is and how that kind should be handled.\n\n"
    "Edit the file directly. Keep my voice and formatting conventions, change "
    "nothing the comments did not ask about, and do not write a summary of "
    "your changes into the document.\n\n"
    "{instruction}"
)

# The other way to send comments: for an answer, not an edit. Not an entry in
# the registry, because it is not a different request so much as the same one
# with the editing taken out -- and a knowledge base that rewords its review
# prompt should not then have to keep a second one in step with it.
_REPLY_ONLY_PROMPT = (
    "You are co-writing a knowledge base with me. The document is "
    "`{document}`.\n\n"
    "I have read the document and left comments on it, and this time I want "
    "your answer to each one rather than an edit. Do not change the document, "
    "or any file other than the one named below. Read the document, and "
    "whatever around it you need, and reply to every comment listed: answer "
    "the question if it asks one, give your view if it asks for one, and if it "
    "asks for a change, say what you would change and why.\n\n"
    "{instruction}"
)

# How replies come back. Appended by `review_prompt` rather than written into
# the templates above, so a knowledge base with its own wording still gets
# answers in the comments: what it can reword is how to treat them, not the
# plumbing that carries the answer home.
#
# A file rather than the turn's final message. That message is a progress
# line in a log, clipped, and free prose; a reply per comment needs a key per
# comment, and a file the model writes on purpose is the one channel that
# survives a chatty last turn intact.
_REPLIES_INSTRUCTION = (
    "Answer every comment in its own thread{also}. Write the answers to "
    "`{path}` as one JSON object mapping each comment's id to your reply, "
    "for example {{\"{example}\": \"…\"}}. Each reply is shown inside the comment "
    "it answers, addressed to me: {what} Keep each to a few sentences of plain "
    "text. Write that file even if some comments needed nothing done, and say "
    "so in their replies. When you are finished, end with a one-line summary."
)

DEFAULTS = [
    Action(
        name=REVIEW,
        label="Address my comments",
        needs_instruction=False,
        placeholder="Anything to add?",
        prompt=_REVIEW_PROMPT,
    ),
]


def describe_comments(comments: list[dict]) -> str:
    """The selected comments, as the block of prompt that carries them.

    Each one is given its quote, its body, its semantic type and *that type's
    instruction*, which is the whole point of the feature: "I don't follow
    this" is a complaint, and "questions are answered in a footnote rather
    than by rewriting the sentence" is a method, and only the pair of them is
    something a model can act on.

    Numbered and spelled out field by field rather than run together as prose,
    so that with six comments in flight there is no ambiguity about which
    instruction belongs to which remark -- which is exactly the mistake a wall
    of text invites, and the one that would quietly ruin a review.
    """
    if not comments:
        return ""

    blocks = [f"Here are the {len(comments)} comments to address."]
    for at, comment in enumerate(comments, start=1):
        quote = str(comment.get("quote") or "").strip()
        where = (
            f"It is attached to this text: “{quote}”"
            if quote
            else "It is about the document as a whole, not about any one passage."
        )
        kind = str(comment.get("semantic_type") or "").strip()
        instruction = str(comment.get("instruction") or "").strip()

        lines = [
            f"--- Comment {at} of {len(comments)} ---",
            f"Id: {comment.get('id', '')}",
            where,
            f"What I wrote: {str(comment.get('body') or '').strip()}",
        ]
        # A second round on the same comment: the reader answered the last
        # reply, and the answer only makes sense next to what it answers.
        earlier = [r for r in comment.get("replies") or [] if isinstance(r, dict)]
        if earlier:
            lines.append("The thread so far, oldest first:")
            lines.extend(
                f"  {str(r.get('author') or 'someone')}: {str(r.get('body') or '').strip()}"
                for r in earlier
            )
        if kind:
            lines.append(f"Kind of remark: {kind}")
            # A type with no instruction still earns its line. It groups this
            # remark with the others of its kind, which is information even
            # when nobody has written down what to do about them.
            lines.append(
                f"How I want a {kind!r} handled: {instruction}"
                if instruction
                else f"I have not written down how a {kind!r} should be handled; use your judgement."
            )
        else:
            lines.append("No kind was set on this one; use your judgement.")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def review_prompt(
    action: Action,
    location: str,
    instruction: str,
    comments: list[dict],
    replies_path: str = "",
    mode: str = "",
) -> str:
    """The whole prompt for one review turn: framing, comments, how to answer.

    `mode` "reply" swaps the reader's framing for one that forbids editing.
    `replies_path` is where the per-comment answers are to be written; with
    none, the turn edits and nothing is asked of it beyond that.
    """
    if mode == "reply":
        framing = (
            _REPLY_ONLY_PROMPT.replace("{document}", location)
            .replace("{instruction}", instruction.strip())
            .strip()
        )
    else:
        framing = action.render(location, instruction.strip()).strip()
    parts = [framing, describe_comments(comments)]
    if replies_path and comments:
        what = (
            "answer it, or say what you would change and why."
            if mode == "reply"
            else "say what you changed and why, or answer it if it asked a question."
        )
        parts.append(
            _REPLIES_INSTRUCTION.format(
                path=replies_path,
                example=str(comments[0].get("id") or "abc12"),
                what=what,
                also="" if mode == "reply" else " as well",
            )
        )
    return "\n\n".join(p for p in parts if p).strip()


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
                "prompt": a.prompt,
            }
            for a in DEFAULTS
        ],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path
