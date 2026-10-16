"""Tests for comment threads: replies, edits, a card's size, and Claude's answers.

A comment's thread was always a list with an author on each entry, and nothing
wrote past the first. Now the reader can reply and edit, Claude answers each
comment it was sent, and the card those land in can be dragged to any size.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from bs4 import BeautifulSoup

from mdweave.agent import actions
from mdweave.agent.broker import Broker
from mdweave.agent.daemon import read_replies
from mdweave.model import Annotation, Comment, Size, TextTarget
from mdweave.render import render_document
from mdweave.serve import Workspace, _writes_something, make_server
from mdweave.sources import sidecar

PROSE = "# Doc\n\nalpha bravo charlie delta echo foxtrot.\n"


def comment(ann_id="k1", quote="bravo charlie", replies=(), **kw) -> Annotation:
    thread = [Comment(body="why this?", author="me", at="2026-10-15T10:00:00Z")]
    thread += [Comment(body=body, author=author) for author, body in replies]
    return Annotation(
        id=ann_id,
        target=TextTarget(quote=quote) if quote else None,
        kind="comment",
        thread=thread,
        **kw,
    )


def card(html: str, ann_id="k1"):
    return BeautifulSoup(html, "html.parser").find("div", attrs={"data-ann": ann_id})


# --- the model ----------------------------------------------------------------

def test_a_size_survives_the_sidecar(tmp_path):
    path = tmp_path / "doc.ann.json"
    sidecar.save(path, [comment(size=Size(w=320, h=240))])
    assert sidecar.load(path)[0].size == Size(w=320, h=240)


def test_no_size_is_written_until_somebody_resizes(tmp_path):
    assert "size" not in comment().to_dict()


# --- rendering ------------------------------------------------------------------

def test_replies_are_nested_boxes_after_the_comment():
    note = card(render_document(PROSE, [comment(replies=[("Claude", "Because.")])]).html)
    first, = note.select(".note__body > .note__entry")
    assert first["data-entry"] == "0"
    assert first.select_one(".note__text").text == "why this?"

    reply, = note.select(".note__replies > .note__reply")
    assert reply["data-entry"] == "1"
    assert "note__reply--claude" in reply["class"]
    assert reply.select_one(".note__author").text == "Claude"
    assert reply.select_one(".note__text").text == "Because."


def test_a_reply_from_the_reader_is_not_marked_as_claudes():
    note = card(render_document(PROSE, [comment(replies=[("me", "fair")])]).html)
    assert "note__reply--claude" not in note.select_one(".note__reply")["class"]


def test_a_card_with_no_replies_still_has_somewhere_to_put_them():
    note = card(render_document(PROSE, [comment()]).html)
    assert note.select_one(".note__replies") is not None
    assert not note.select(".note__reply")


def test_a_stored_size_is_drawn_and_lifts_the_height_cap():
    note = card(render_document(PROSE, [comment(size=Size(w=330, h=210))]).html)
    body = note.select_one(".note__body")
    assert "note__body--sized" in body["class"]
    assert "width: 330px" in body["style"] and "height: 210px" in body["style"]


def test_a_comment_whose_text_has_gone_is_drawn_at_the_top_not_dropped():
    """It may be carrying Claude's answer; hiding it would hide the answer."""
    gone = comment(quote="words that are no longer there", replies=[("Claude", "Rewrote it.")])
    result = render_document(PROSE, [gone])
    note = card(result.html)

    assert note is not None
    assert note["data-level"] == "document"
    assert "words that are no longer there" in note.select_one(".note__detached").text
    assert note.select_one(".note__reply .note__text").text == "Rewrote it."
    # Still reported, so `build --strict` says what it always said.
    assert [p.annotation.id for p in result.unresolved] == ["k1"]


# --- the endpoints ----------------------------------------------------------------

@pytest.fixture()
def server(tmp_path):
    inputs, outputs = tmp_path / "markdown_inputs", tmp_path / "html_outputs"
    inputs.mkdir()
    (inputs / "doc.md").write_text(PROSE, encoding="utf-8")
    sidecar.save(inputs / "doc.ann.json", [comment(), comment("k2", quote="echo")])
    workspace = Workspace(inputs=inputs, outputs=outputs)
    workspace.rebuild_all()

    broker = Broker()
    httpd = make_server(workspace, "127.0.0.1", 0, jobs=broker)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", workspace, broker
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def call(base, path, payload=None, method="POST"):
    request = urllib.request.Request(
        base + path, data=json.dumps(payload or {}).encode(), method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def thread_of(workspace, ann_id="k1"):
    found = next(a for a in workspace.annotations_for("doc") if a.id == ann_id)
    return [(c.author, c.body) for c in found.thread]


def test_the_reader_can_reply(server):
    base, workspace, _ = server
    status, body = call(base, "/api/annotations/k1/replies",
                        {"document": "doc", "body": "on reflection, fine", "author": "me"})
    assert status == 201 and body["index"] == 1
    assert thread_of(workspace) == [("me", "why this?"), ("me", "on reflection, fine")]
    # And the page carries it on a reload.
    page = (workspace.outputs / "doc.html").read_text(encoding="utf-8")
    assert "on reflection, fine" in card(page).select_one(".note__reply").text


@pytest.mark.parametrize("path, payload, status", [
    ("/api/annotations/nope/replies", {"document": "doc", "body": "x"}, 404),
    ("/api/annotations/k1/replies", {"document": "doc", "body": "  "}, 400),
])
def test_a_reply_needs_a_comment_and_something_to_say(server, path, payload, status):
    base, workspace, _ = server
    assert call(base, path, payload)[0] == status
    assert len(thread_of(workspace)) == 1


def test_the_comment_itself_can_be_edited(server):
    base, workspace, _ = server
    status, _ = call(base, "/api/annotations/k1",
                     {"document": "doc", "entry": 0, "body": "  why this, really?  "}, "PATCH")
    assert status == 200
    assert thread_of(workspace) == [("me", "why this, really?")]


def test_a_reply_can_be_edited_without_touching_the_comment(server):
    base, workspace, _ = server
    call(base, "/api/annotations/k1/replies", {"document": "doc", "body": "first go"})
    call(base, "/api/annotations/k1", {"document": "doc", "entry": 1, "body": "second go"},
         "PATCH")
    assert thread_of(workspace) == [("me", "why this?"), ("me", "second go")]


@pytest.mark.parametrize("payload", [
    {"entry": 3, "body": "x"},
    {"entry": "0", "body": "x"},
    {"entry": 0, "body": ""},
])
def test_an_edit_must_name_a_message_and_say_something(server, payload):
    base, workspace, _ = server
    status, _ = call(base, "/api/annotations/k1", {"document": "doc", **payload}, "PATCH")
    assert status == 400
    assert thread_of(workspace) == [("me", "why this?")]


def test_a_size_is_saved_and_kept_within_reason(server):
    base, workspace, _ = server
    call(base, "/api/annotations/k1", {"document": "doc", "size": {"w": 10, "h": 99999}},
         "PATCH")
    saved = next(a for a in workspace.annotations_for("doc") if a.id == "k1").size
    assert saved == Size(w=160, h=2000)

    call(base, "/api/annotations/k1", {"document": "doc", "size": None}, "PATCH")
    assert next(a for a in workspace.annotations_for("doc") if a.id == "k1").size is None


def test_a_new_comment_is_born_where_its_composer_was_dragged(server):
    base, workspace, _ = server
    status, body = call(base, "/api/annotations", {
        "document": "doc", "quote": "delta", "body": "moved", "offset": {"dx": 140, "dy": -30},
    })
    assert status == 201
    assert body["annotation"]["offset"] == {"dx": 140.0, "dy": -30.0}


# --- Claude's answers ---------------------------------------------------------------

def finished_review(broker, replies, comments=("k1", "k2"), mode=""):
    """A review job, claimed and reported done by a runner."""
    job = broker.submit(
        "doc", actions.REVIEW,
        comments=[{"id": c, "quote": "q", "body": "b"} for c in comments],
        mode=mode,
    )
    assert broker.claim(timeout=1).id == job.id
    return job


def test_the_runner_reports_replies_but_does_not_write_them(server):
    """The runner's routes take no password, so what it says is held, not written."""
    base, workspace, broker = server
    job = finished_review(broker, {})
    status, _ = call(base, f"/api/agent/jobs/{job.id}/done", {
        "ok": True, "detail": "done",
        "replies": {"k1": "Because it reads better.", "stranger": "not sent", "k2": 7},
    })
    assert status == 200
    assert broker.get(job.id).replies == {"k1": "Because it reads better."}
    assert thread_of(workspace) == [("me", "why this?")]


def test_the_page_writes_them_in_once(server):
    base, workspace, broker = server
    job = finished_review(broker, {})
    call(base, f"/api/agent/jobs/{job.id}/done",
         {"ok": True, "replies": {"k1": "Because.", "k2": "Gone now."}})

    status, body = call(base, f"/api/agent/jobs/{job.id}/replies")
    assert status == 200 and sorted(body["added"]) == ["k1", "k2"]
    assert thread_of(workspace) == [("me", "why this?"), ("Claude", "Because.")]
    assert thread_of(workspace, "k2")[-1] == ("Claude", "Gone now.")
    # The answer comes back with the comments, for the cards on screen.
    k1 = next(a for a in body["annotations"] if a["id"] == "k1")
    assert k1["thread"][-1]["author"] == "Claude"

    # A second tab collecting the same job adds nothing.
    status, again = call(base, f"/api/agent/jobs/{job.id}/replies")
    assert status == 200 and again["added"] == []
    assert len(thread_of(workspace)) == 2


def test_replies_cannot_be_taken_before_the_job_is_done(server):
    base, _, broker = server
    job = finished_review(broker, {})
    assert call(base, f"/api/agent/jobs/{job.id}/replies")[0] == 409


def test_the_diff_carries_the_replies_and_the_mode(server):
    base, _, broker = server
    job = finished_review(broker, {}, mode="reply")
    call(base, f"/api/agent/jobs/{job.id}/done", {"ok": True, "replies": {"k1": "Yes."}})
    with urllib.request.urlopen(f"{base}/api/agent/jobs/{job.id}/diff") as response:
        diff = json.loads(response.read())
    assert diff["replies"] == {"k1": "Yes."} and diff["mode"] == "reply"


def test_the_runner_is_told_which_mode(server):
    from mdweave.agent import protocol

    _, _, broker = server
    job = finished_review(broker, {}, mode="reply")
    assert protocol.dispatch(job)["mode"] == "reply"


def test_writing_replies_in_arms_the_auto_commit():
    assert _writes_something("/api/agent/jobs/abc/replies")
    assert not _writes_something("/api/agent/jobs/abc/events")


# --- the prompt, and reading the answer back -------------------------------------------

def test_the_prompt_names_each_comment_and_where_to_answer():
    sent = [{"id": "k1", "quote": "q", "body": "why?",
             "replies": [{"author": "Claude", "body": "Because."}, {"author": "me", "body": "Hm?"}]}]
    prompt = actions.review_prompt(actions.DEFAULTS[0], "/x/doc.md", "", sent, "/tmp/r/replies.json")
    assert "Id: k1" in prompt
    assert "`/tmp/r/replies.json`" in prompt and '{"k1": "…"}' in prompt
    assert "Claude: Because." in prompt and "me: Hm?" in prompt
    assert "Edit the file directly" in prompt


def test_replies_only_says_not_to_edit():
    prompt = actions.review_prompt(
        actions.DEFAULTS[0], "/x/doc.md", "", [{"id": "k1", "body": "b"}], "/tmp/r.json", "reply"
    )
    assert "Do not change the document" in prompt
    assert "Edit the file directly" not in prompt


def test_a_knowledge_bases_own_wording_still_gets_the_reply_instructions():
    own = actions.Action(name="review", label="Go", prompt="Fix {document}. {instruction}")
    prompt = actions.review_prompt(own, "/x/doc.md", "", [{"id": "k1", "body": "b"}], "/tmp/r.json")
    assert prompt.startswith("Fix /x/doc.md.")
    assert "/tmp/r.json" in prompt


def test_reading_the_replies_keeps_only_what_can_be_used(tmp_path):
    path = tmp_path / "replies.json"
    path.write_text(json.dumps({"k1": " Because. ", "k9": "unsent", "k2": ["no"], "k3": ""}))
    sent = [{"id": "k1"}, {"id": "k2"}, {"id": "k3"}]
    assert read_replies(str(path), sent) == {"k1": "Because."}
    assert read_replies(str(tmp_path / "missing.json"), sent) == {}
    path.write_text("not json")
    assert read_replies(str(path), sent) == {}
