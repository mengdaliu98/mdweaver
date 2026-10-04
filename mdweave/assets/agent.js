/* mdweave -- the comments on this document, and what Claude did with them.
 *
 * One button, two screens.
 *
 * The first is a review. It lists every comment on the document -- the ones
 * pinned to a sentence and the ones about the piece as a whole -- each with a
 * tick that decides whether Claude is told about it. There is no box to type
 * an instruction into any more, and that is the point: the instruction was
 * already written, in the margin, next to the sentence it is about. Retyping
 * it into a dialog threw away the one thing a comment has that a prompt does
 * not, which is where it points.
 *
 * The second is what came back. The job does not write the document; it hands
 * back the text it would have written, and this page shows it beside the text
 * that is there, hunk by hunk, with accept and reject on each. Only then is
 * anything written. The flow this replaces wrote first and showed you
 * afterwards, which is a fine bargain for a typo fix and a poor one for a
 * model rewriting prose you care about.
 *
 * The diff is computed here rather than fetched. Three reasons, in order of
 * weight: the reader toggles hunks and the right-hand side has to follow, and
 * a round trip per tick is absurd; the shape of a hunk -- how much context,
 * what joins two changes into one -- is a presentation decision and belongs
 * with the presentation; and it keeps the endpoint down to "here are two
 * documents", which is the least the server can be asked to know.
 *
 * Plain ES5, no build step, same as its neighbours.
 */
(function () {
  "use strict";

  var ui = window.mdweaveUI;
  var button = document.getElementById("agent");
  var dialog = document.getElementById("agent-dialog");
  if (!ui || !button || !dialog) return;

  var DOC_ID = document.body.dataset.document || "";
  var ANNOTATIONS = "/api/annotations";

  /* Two different numbers, which a unified diff ties together and this does
   * not, because what they are for here is different.
   *
   * CONTEXT is how much unchanged text to show around a change. Three lines,
   * because in markdown one of them is usually blank: a single line of
   * context is a paragraph break and tells the reader nothing about where
   * they are.
   *
   * GAP is how much unchanged text between two changes still counts as one
   * change, and it is as small as it can be. A hunk is the unit of accept and
   * reject, so joining two of them takes a choice away -- worth it only when
   * splitting them would let the reader build a passage that neither they nor
   * Claude ever wrote. One line is the case that matters: a rewritten
   * sentence hard-wrapped across three lines of which the middle one happened
   * to survive. A whole untouched paragraph between two changes is three
   * lines, stays above this, and stays two separate decisions. */
  var CONTEXT = 3;
  var GAP = 1;

  /* The biggest LCS table we will build. A document is a few hundred lines and
   * the common prefix and suffix are trimmed before any table exists, so this
   * is reached by a rewrite that shares almost nothing with the original --
   * exactly the case where a line-by-line diff would have been noise anyway.
   * Past it, the changed middle is shown as one replace-all hunk, which is
   * honest about what happened and costs nothing to compute. */
  var MAX_CELLS = 1000000;

  /* --- what we know right now ---------------------------------------------- */

  var comments = []; // every comment on the document, as the server sent it
  var instructions = {}; // semantic type name -> its instruction, for the tag
  var sent = []; // the comments this run was actually given
  var pieces = []; // the diff: unchanged runs and hunks, in document order
  var hunkCount = 0;
  var accepted = []; // one flag per hunk, index-aligned with the `hunk` field
  var jobId = "";
  var stream = null;
  var parts = null; // the shell's elements, found once and kept

  /* --- the shell ------------------------------------------------------------
   *
   * The dialog element itself is the one the template already ships, and only
   * its *contents* are replaced. Building a second <dialog> in script would
   * mean restating the backdrop, the shadow, the print rule and the modal
   * semantics that this one already has, and leaving an unused one in the DOM
   * besides. For the same reason the title, the error line and the two footer
   * buttons keep the `checkpoint-dialog__*` classes: this is the same kind of
   * dialog as Checkpoint and should not drift into a second shape. Everything
   * genuinely new -- the comment list, the merge view -- is `review__*` and
   * styled in review.css. */

  var SHELL =
    '<h2 class="checkpoint-dialog__title" id="agent-title">Comments on this document</h2>' +
    '<p class="checkpoint-dialog__what"><code class="review__where"></code></p>' +
    '<div class="review__stage review__stage--pick">' +
    '<p class="review__note"></p>' +
    '<ul class="review__list"></ul>' +
    "</div>" +
    '<div class="review__stage review__stage--run" hidden>' +
    '<ul class="agent-log review__log" aria-live="polite" aria-label="Progress"></ul>' +
    "</div>" +
    '<div class="review__stage review__stage--result" hidden></div>' +
    '<p class="checkpoint-dialog__error review__error" hidden></p>' +
    '<div class="checkpoint-dialog__actions">' +
    '<button type="button" class="checkpoint-dialog__cancel review__close">Cancel</button>' +
    '<button type="button" class="checkpoint-dialog__submit review__go">Send to Claude</button>' +
    "</div>";

  function build() {
    if (parts) return;

    dialog.innerHTML = SHELL;
    dialog.classList.add("review-dialog");

    parts = {
      title: dialog.querySelector("#agent-title"),
      where: dialog.querySelector(".review__where"),
      pick: dialog.querySelector(".review__stage--pick"),
      note: dialog.querySelector(".review__note"),
      list: dialog.querySelector(".review__list"),
      run: dialog.querySelector(".review__stage--run"),
      log: dialog.querySelector(".review__log"),
      result: dialog.querySelector(".review__stage--result"),
      error: dialog.querySelector(".review__error"),
      close: dialog.querySelector(".review__close"),
      go: dialog.querySelector(".review__go"),
    };

    parts.close.addEventListener("click", close);
    parts.go.addEventListener("click", function () {
      if (parts.go.disabled) return;
      if (stageName === "pick") send();
      else if (stageName === "result") apply();
    });
  }

  var stageName = "pick";

  /* The heading is the only thing on screen that says which of the three
   * states this is, once the body below it has been swapped. Worth keeping
   * accurate: `aria-labelledby` on the dialog points at it, so for a screen
   * reader it is not decoration but the name of the thing that just opened. */
  var HEADINGS = {
    pick: "Comments on this document",
    run: "Claude is reading the comments",
    result: "What Claude would change",
  };

  function stage(name) {
    stageName = name;
    parts.title.textContent = HEADINGS[name];
    parts.pick.hidden = name !== "pick";
    parts.run.hidden = name !== "run";
    parts.result.hidden = name !== "result";

    /* The merge view needs two columns of prose side by side; the comment
     * list does not, and a list floating in a wide dialog reads as a mistake.
     * So the width is a property of the screen, not of the dialog. */
    dialog.classList.toggle("review-dialog--wide", name === "result");

    parts.go.hidden = false;
    parts.close.textContent = name === "pick" ? "Cancel" : "Close";
    if (name === "run") {
      parts.go.disabled = true;
      parts.go.textContent = "Working…";
    }
  }

  function showError(text) {
    parts.error.textContent = text;
    parts.error.hidden = false;
  }

  function hideError() {
    parts.error.hidden = true;
  }

  function note(kind, text) {
    var row = document.createElement("li");
    row.className = "agent-log__row agent-log__row--" + kind;
    row.textContent = text;
    parts.log.appendChild(row);
    parts.log.scrollTop = parts.log.scrollHeight;
    return row;
  }

  function open() {
    build();
    parts.where.textContent = DOC_ID;
    hideError();
    parts.log.innerHTML = "";
    parts.result.innerHTML = "";
    stage("pick");

    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute("open", "");

    load();
  }

  function close() {
    if (stream) {
      stream.close();
      stream = null;
    }
    if (dialog.close) dialog.close();
    else dialog.removeAttribute("open");
  }

  /* --- stage one: which comments go ----------------------------------------- */

  function load() {
    parts.list.innerHTML = "";
    parts.note.textContent = "Reading the comments…";
    parts.go.disabled = true;
    parts.go.textContent = "Send to Claude";

    fetch("/api/agent/comments?document=" + encodeURIComponent(DOC_ID))
      .then(function (response) {
        if (!response.ok) return ui.reject(response);
        return response.json();
      })
      .then(function (payload) {
        comments = (payload && payload.comments) || [];
        instructions = {};
        ((payload && payload.types) || []).forEach(function (type) {
          if (type && type.name) instructions[type.name] = type.instruction || "";
        });
        fill();
      })
      .catch(function (error) {
        parts.note.textContent = "";
        showError("Could not read the comments: " + error.message);
      });
  }

  function fill() {
    parts.list.innerHTML = "";
    if (!comments.length) {
      parts.note.textContent =
        "Nothing is commented on yet. Highlight a sentence and say what is " +
        "wrong with it, then come back.";
      tally();
      return;
    }

    parts.note.textContent = "Tick the ones Claude should deal with.";
    comments.forEach(function (comment) {
      parts.list.appendChild(row(comment));
    });
    tally();
  }

  function row(comment) {
    var item = document.createElement("li");
    item.className = "review__row";
    item.setAttribute("data-ann", comment.id);

    var box = document.createElement("input");
    box.type = "checkbox";
    box.className = "review__tick";
    box.checked = comment.send_to_claude !== false;
    box.setAttribute("aria-label", "Send this comment to Claude");
    box.addEventListener("change", function () {
      persist(comment, box);
    });
    item.appendChild(box);

    var main = document.createElement("div");
    main.className = "review__main";
    item.appendChild(main);

    var head = document.createElement("div");
    head.className = "review__head";
    main.appendChild(head);

    if (comment.semantic_type) {
      var tag = document.createElement("span");
      tag.className = "review__type";
      tag.textContent = comment.semantic_type;
      /* The type carries the standing instruction for comments of its kind,
       * which is the half of the request the comment itself never states.
       * Worth being able to see without leaving for the settings panel. */
      if (instructions[comment.semantic_type]) {
        tag.title = instructions[comment.semantic_type];
      }
      head.appendChild(tag);
    }

    var quote = document.createElement("span");
    if (comment.anchored && comment.quote) {
      quote.className = "review__quote";
      quote.textContent = "“" + comment.quote + "”";
    } else {
      /* Not an empty quote but a different kind of comment, and it has to
       * read as one: a blank where the quoted sentence goes looks like a
       * comment that lost its anchor. */
      quote.className = "review__quote review__quote--whole";
      quote.textContent = "About the whole document";
    }
    head.appendChild(quote);

    var body = document.createElement("p");
    body.className = "review__body";
    body.textContent = comment.body || "";
    /* Three lines in the stylesheet, not here. Cutting the string would mean
     * the dialog is the only place the comment is incomplete, and the reader
     * would have no way to tell a truncated remark from a terse one. */
    body.title = "Click to show the whole comment";
    body.addEventListener("click", function () {
      body.classList.toggle("review__body--full");
    });
    main.appendChild(body);

    if (comment.color) wear(item, comment.color);
    return item;
  }

  /* Paint the row in the comment's own color.
   *
   * `note--cN` is the class the generated scheme maps the slot's four custom
   * properties onto, and it sets nothing else -- the pin's shape and position
   * come from `.note`, which this is not. So borrowing the modifier is how a
   * new surface joins the palette without learning what any slot looks like,
   * which is the whole arrangement in scheme.py. */
  function wear(element, color) {
    var slot = slotOf(color);
    if (slot) {
      element.classList.add("note--" + slot);
      return;
    }
    /* A hand-edited sidecar may hold a raw CSS color instead of one of the
     * six. Setting it as a property is safe: an unparseable value is dropped
     * by the browser and the row simply stays uncolored. */
    if (typeof color === "string" && color) {
      element.classList.add("note--custom");
      element.style.setProperty("--note-custom", color);
    }
  }

  function slotOf(color) {
    if (typeof color === "number") {
      return color >= 1 && color <= 6 ? "c" + color : null;
    }
    if (typeof color !== "string") return null;
    if (/^c[1-6]$/.test(color)) return color;
    if (/^[1-6]$/.test(color)) return "c" + color;
    return null;
  }

  function chosen() {
    return comments.filter(function (comment) {
      var item = parts.list.querySelector('[data-ann="' + cssEscape(comment.id) + '"]');
      return !!(item && item.querySelector(".review__tick").checked);
    });
  }

  /* Annotation ids come from a sidecar somebody may have hand-written, so they
   * are not guaranteed to be selector-safe. Quoting the attribute value is
   * enough for everything except a quote or a backslash in the id itself. */
  function cssEscape(value) {
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function tally() {
    var count = chosen().length;
    parts.go.disabled = count === 0;
    parts.go.textContent =
      count === 0
        ? "Send to Claude"
        : "Send " + count + (count === 1 ? " comment" : " comments") + " to Claude";
  }

  /* A tick is a decision about the document, so it belongs in the document and
   * not in this dialog's memory -- untick three comments, close the tab, come
   * back, and they are still unticked. Written straight through rather than
   * batched at send time: the reader may well tick things now and run Claude
   * next week. */
  function persist(comment, box) {
    var wanted = box.checked;
    tally();

    fetch(ANNOTATIONS + "/" + encodeURIComponent(comment.id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document: DOC_ID, send_to_claude: wanted }),
    })
      .then(function (response) {
        if (!response.ok) return ui.reject(response);
        comment.send_to_claude = wanted;
      })
      .catch(function (error) {
        /* Put the box back. Everywhere else here a failed write is reported
         * and the optimistic change left standing, because the reader can see
         * what they did; a checkbox that silently disagrees with the file is
         * the one case where that would mislead them at the moment it
         * matters, which is the next time they press Send. */
        box.checked = !wanted;
        tally();
        ui.toast("Not saved: " + error.message, "error");
      });
  }

  /* --- sending, and watching it run ------------------------------------------ */

  function send() {
    sent = chosen();
    if (!sent.length) return;

    var ids = sent.map(function (comment) {
      return comment.id;
    });

    hideError();
    parts.log.innerHTML = "";
    stage("run");
    note("status", "queued");

    fetch("/api/agent/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document: DOC_ID, ids: ids }),
    })
      .then(function (response) {
        if (!response.ok) return ui.reject(response);
        return response.json();
      })
      .then(function (payload) {
        jobId = (payload && payload.job && payload.job.id) || "";
        if (!jobId) throw new Error("the server queued a job with no id");
        watch(jobId);
      })
      .catch(function (error) {
        stage("pick");
        tally();
        showError(error.message);
      });
  }

  /* The progress stream is the only long-lived connection in the design: the
   * browser's EventSource reconnects on its own and says where it got to,
   * which is worth having and costs nothing. Unchanged from the flow this
   * replaces -- what happens at the end of it is the part that differs. */
  function watch(id) {
    stream = new EventSource("/api/agent/jobs/" + encodeURIComponent(id) + "/events");

    ["status", "text", "tool", "error", "done"].forEach(function (kind) {
      stream.addEventListener(kind, function (message) {
        var payload;
        try {
          payload = JSON.parse(message.data);
        } catch (e) {
          return;
        }
        note(payload.kind || kind, payload.text || "");
      });
    });

    stream.addEventListener("closed", function (message) {
      var state = "";
      try {
        state = (JSON.parse(message.data) || {}).state || "";
      } catch (e) {
        /* fall through to the generic message */
      }
      stream.close();
      stream = null;

      if (state === "done") {
        note("status", "reading what changed…");
        collect();
        return;
      }

      parts.go.disabled = false;
      parts.go.textContent = "Send to Claude";
      stage("pick");
      tally();

      if (state === "stalled") {
        showError(
          "The runner stopped reporting. Nothing was retried — a Claude turn " +
            "is not safe to repeat automatically. Check `mdweave agent` on the devserver."
        );
        return;
      }
      showError("The job did not finish. See the log above.");
    });

    /* EventSource retries on its own; a visible error every time it does
     * would be noise, so only say something once it has actually given up. */
    stream.onerror = function () {
      if (stream && stream.readyState === 2) {
        note("error", "lost the progress stream — the job may still be running");
      }
    };
  }

  function collect() {
    fetch("/api/agent/jobs/" + encodeURIComponent(jobId) + "/diff")
      .then(function (response) {
        if (!response.ok) return ui.reject(response);
        return response.json();
      })
      .then(function (payload) {
        result(payload || {});
      })
      .catch(function (error) {
        parts.go.disabled = false;
        parts.go.textContent = "Send to Claude";
        stage("pick");
        tally();
        showError("It ran, but what it wrote could not be read back: " + error.message);
      });
  }

  /* --- the diff --------------------------------------------------------------
   *
   * A plain line-based longest-common-subsequence, with the matching head and
   * tail taken off first. The trim is not an optimisation so much as the thing
   * that makes the table affordable at all: a Claude turn that answers three
   * comments touches three paragraphs, and everything above the first and
   * below the last is identical. */

  function splitLines(text) {
    return String(text === null || text === undefined ? "" : text).split("\n");
  }

  function diffOps(a, b) {
    var ops = [];
    var head = 0;
    var aEnd = a.length;
    var bEnd = b.length;
    var i;

    while (head < aEnd && head < bEnd && a[head] === b[head]) head++;
    while (aEnd > head && bEnd > head && a[aEnd - 1] === b[bEnd - 1]) {
      aEnd--;
      bEnd--;
    }

    for (i = 0; i < head; i++) ops.push({ op: "=", text: a[i] });

    var left = a.slice(head, aEnd);
    var right = b.slice(head, bEnd);
    var middle =
      (left.length + 1) * (right.length + 1) > MAX_CELLS
        ? wholesale(left, right)
        : lcsOps(left, right);
    for (i = 0; i < middle.length; i++) ops.push(middle[i]);

    for (i = aEnd; i < a.length; i++) ops.push({ op: "=", text: a[i] });
    return ops;
  }

  function wholesale(a, b) {
    var ops = [];
    var i;
    for (i = 0; i < a.length; i++) ops.push({ op: "-", text: a[i] });
    for (i = 0; i < b.length; i++) ops.push({ op: "+", text: b[i] });
    return ops;
  }

  /* `table[i][j]` is the length of the longest common subsequence of `a` from
   * `i` on and `b` from `j` on, filled from the far corner back. Walking it
   * forwards afterwards is what puts a removal before the addition that
   * replaces it, which is the order a reader expects to see them in. */
  function lcsOps(a, b) {
    var n = a.length;
    var m = b.length;
    var table = new Array(n + 1);
    var i, j, row, next;

    table[n] = new Array(m + 1);
    for (j = 0; j <= m; j++) table[n][j] = 0;

    for (i = n - 1; i >= 0; i--) {
      row = new Array(m + 1);
      next = table[i + 1];
      row[m] = 0;
      for (j = m - 1; j >= 0; j--) {
        row[j] = a[i] === b[j] ? next[j + 1] + 1 : Math.max(next[j], row[j + 1]);
      }
      table[i] = row;
    }

    var ops = [];
    i = 0;
    j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        ops.push({ op: "=", text: a[i] });
        i++;
        j++;
      } else if (table[i + 1][j] >= table[i][j + 1]) {
        ops.push({ op: "-", text: a[i] });
        i++;
      } else {
        ops.push({ op: "+", text: b[j] });
        j++;
      }
    }
    while (i < n) {
      ops.push({ op: "-", text: a[i] });
      i++;
    }
    while (j < m) {
      ops.push({ op: "+", text: b[j] });
      j++;
    }
    return ops;
  }

  /* Ops in, pieces out: runs of unchanged lines, and hunks carrying the whole
   * of their two sides. A hunk is the unit the reader accepts or rejects, so
   * two changes a line apart have to become one -- otherwise accepting the
   * first and rejecting the second produces a sentence neither the writer nor
   * Claude ever wrote. */
  function piecesOf(ops) {
    var runs = [];
    var i, op, last;

    for (i = 0; i < ops.length; i++) {
      op = ops[i];
      last = runs.length ? runs[runs.length - 1] : null;
      if (op.op === "=") {
        if (!last || !last.same) {
          last = { same: [] };
          runs.push(last);
        }
        last.same.push(op.text);
      } else {
        if (!last || last.same) {
          last = { before: [], after: [] };
          runs.push(last);
        }
        if (op.op === "-") last.before.push(op.text);
        else last.after.push(op.text);
      }
    }

    var out = [];
    var hunks = 0;
    for (i = 0; i < runs.length; i++) {
      var run = runs[i];
      var prev = out.length ? out[out.length - 1] : null;
      var following = runs[i + 1];

      if (
        run.same &&
        run.same.length <= GAP &&
        prev &&
        !prev.same &&
        following &&
        !following.same
      ) {
        /* The bridging lines go into *both* sides of the merged hunk, so
         * accepting it and rejecting it leave them exactly where they are --
         * they are shown as part of the change only because they sit inside
         * it, not because anything happened to them. */
        prev.before = prev.before.concat(run.same, following.before);
        prev.after = prev.after.concat(run.same, following.after);
        i++; // `following` has been folded in
        continue;
      }

      if (run.same) {
        out.push({ same: run.same.slice() });
      } else {
        out.push({ hunk: hunks++, before: run.before.slice(), after: run.after.slice() });
      }
    }

    hunkCount = hunks;
    return out;
  }

  /* --- stage two: accept or reject -------------------------------------------- */

  function result(payload) {
    pieces = piecesOf(diffOps(splitLines(payload.before), splitLines(payload.after)));
    accepted = [];
    var i;
    /* Everything starts accepted. The flow this replaces wrote all of it to
     * disk without asking, so opening with the lot ticked is still strictly
     * more cautious than what came before -- and a reader who wants none of
     * it has one button to press. */
    for (i = 0; i < hunkCount; i++) accepted.push(true);

    hideError();
    stage("result");
    parts.result.innerHTML = "";

    if (!hunkCount) {
      /* Nothing to accept and nothing to delete: a comment is only dealt with
       * by the text changing, and the text did not. One sentence does not
       * need the two-column width either. */
      dialog.classList.remove("review-dialog--wide");
      var empty = document.createElement("p");
      empty.className = "review__note";
      empty.textContent =
        "Claude read the comments and left the document exactly as it was.";
      parts.result.appendChild(empty);
      parts.go.hidden = true;
      parts.close.textContent = "Close";
      return;
    }

    parts.go.hidden = false;
    parts.result.appendChild(merge());
    outcome();
  }

  function merge() {
    var root = document.createElement("div");
    root.className = "merge";

    var bar = document.createElement("div");
    bar.className = "merge__bar";
    var count = document.createElement("span");
    count.className = "merge__count";
    count.textContent =
      hunkCount + (hunkCount === 1 ? " change" : " changes") + " to look at";
    bar.appendChild(count);
    bar.appendChild(sweep("Accept all", true));
    bar.appendChild(sweep("Reject all", false));
    root.appendChild(bar);

    var heads = document.createElement("div");
    heads.className = "merge__heads";
    heads.appendChild(label("merge__label--before", "Before", "what is on disk now"));
    heads.appendChild(label("merge__label--after", "After", "what Claude would write"));
    root.appendChild(heads);

    var body = document.createElement("div");
    body.className = "merge__body";
    pieces.forEach(function (piece, index) {
      body.appendChild(
        piece.same
          ? unchanged(piece.same, index === 0, index === pieces.length - 1)
          : changed(piece)
      );
    });
    root.appendChild(body);

    var cards = document.createElement("div");
    cards.className = "merge__cards";
    cards.appendChild(column("before", "Sent to Claude"));
    cards.appendChild(column("after", "Still open afterwards"));
    root.appendChild(cards);

    return root;
  }

  function label(className, text, hint) {
    var el = document.createElement("div");
    el.className = "merge__label " + className;
    el.textContent = text;
    el.title = hint;
    return el;
  }

  function sweep(text, value) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "merge__sweep";
    el.textContent = text;
    el.addEventListener("click", function () {
      for (var i = 0; i < accepted.length; i++) accepted[i] = value;
      parts.result.querySelectorAll(".merge__hunk").forEach(function (block) {
        paint(block, value);
      });
      outcome();
    });
    return el;
  }

  function column(side, heading) {
    var el = document.createElement("section");
    el.className = "merge__column merge__column--" + side;
    var title = document.createElement("h3");
    title.className = "merge__heading";
    title.textContent = heading;
    el.appendChild(title);
    var holder = document.createElement("div");
    holder.className = "merge__cardlist";
    el.appendChild(holder);
    return el;
  }

  /* An unchanged run, spanning both columns rather than printed twice. Showing
   * a line that nobody touched under both "Before" and "After" invites the
   * reader to compare two identical things, which is work for no answer. */
  function unchanged(lines, first, last) {
    var el = document.createElement("div");
    el.className = "merge__same";

    var top = first ? 0 : CONTEXT;
    var bottom = last ? 0 : CONTEXT;

    if (top + bottom >= lines.length) {
      el.appendChild(lineBlock(lines, "merge__lines"));
      return el;
    }

    if (top) el.appendChild(lineBlock(lines.slice(0, top), "merge__lines"));

    var hidden = lines.length - top - bottom;
    var skip = document.createElement("p");
    skip.className = "merge__skip";
    skip.textContent =
      "⋯ " + hidden + (hidden === 1 ? " unchanged line" : " unchanged lines") + " ⋯";
    el.appendChild(skip);

    if (bottom) el.appendChild(lineBlock(lines.slice(lines.length - bottom), "merge__lines"));
    return el;
  }

  function changed(piece) {
    var el = document.createElement("div");
    el.className = "merge__hunk";
    el.setAttribute("data-hunk", String(piece.hunk));

    var head = document.createElement("div");
    head.className = "merge__hunkbar";

    var name = document.createElement("span");
    name.className = "merge__hunkname";
    name.textContent = "Change " + (piece.hunk + 1) + " of " + hunkCount;
    head.appendChild(name);

    var group = document.createElement("div");
    group.className = "merge__choice";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Change " + (piece.hunk + 1));
    group.appendChild(choice(piece, "Accept", true));
    group.appendChild(choice(piece, "Reject", false));
    head.appendChild(group);
    el.appendChild(head);

    var pair = document.createElement("div");
    pair.className = "merge__pair";
    pair.appendChild(side(piece.before, "before"));
    pair.appendChild(side(piece.after, "after"));
    el.appendChild(pair);

    paint(el, true);
    return el;
  }

  /* A removal with nothing opposite it, or an addition with nothing removed,
   * still needs a cell on the other column -- otherwise the two sides stop
   * lining up and the view is a diff only by accident. */
  function side(lines, which) {
    var el = document.createElement("div");
    el.className = "merge__side merge__side--" + which;
    if (!lines.length) {
      el.classList.add("merge__side--empty");
      el.textContent = which === "before" ? "nothing here before" : "removed entirely";
      return el;
    }
    el.appendChild(lineBlock(lines, "merge__lines"));
    return el;
  }

  function lineBlock(lines, className) {
    var el = document.createElement("pre");
    el.className = className;
    /* An empty markdown line is a paragraph break and has to stay visible as
     * one, so the text is joined with newlines and shown in a <pre> rather
     * than split into elements that would collapse. */
    el.textContent = lines.join("\n");
    return el;
  }

  function choice(piece, text, value) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "merge__pick merge__pick--" + (value ? "yes" : "no");
    el.textContent = text;
    el.setAttribute("data-takes", value ? "after" : "before");
    el.addEventListener("click", function () {
      accepted[piece.hunk] = value;
      paint(el.closest(".merge__hunk"), value);
      outcome();
    });
    return el;
  }

  function paint(block, value) {
    if (!block) return;
    block.classList.toggle("merge__hunk--accepted", value);
    block.classList.toggle("merge__hunk--rejected", !value);
    block.querySelectorAll(".merge__pick").forEach(function (pick) {
      var mine = pick.getAttribute("data-takes") === (value ? "after" : "before");
      pick.setAttribute("aria-pressed", mine ? "true" : "false");
      pick.classList.toggle("merge__pick--on", mine);
    });
  }

  /* The document the reader is actually asking for: the unchanged text, plus
   * whichever side of each hunk they chose. Neither "before" nor "after" on
   * its own is ever written unless every hunk went the same way. */
  function merged() {
    var out = [];
    for (var i = 0; i < pieces.length; i++) {
      var piece = pieces[i];
      out = out.concat(piece.same ? piece.same : accepted[piece.hunk] ? piece.after : piece.before);
    }
    return out.join("\n");
  }

  /* Collapse every run of whitespace to one space.
   *
   * The quote was captured from the rendered page and we are searching the
   * markdown it came from, so the two agree on prose and disagree wherever
   * source syntax falls inside the quoted run. Whitespace is the one
   * disagreement worth repairing, because it is the common one: markdown is
   * hard-wrapped and a rendered paragraph is not, so a quote of two sentences
   * is one line on screen and two in the file. Nothing else is attempted, and
   * the cost of a miss is a comment kept rather than deleted -- which is the
   * direction to fail in. */
  function flatten(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function survives(comment, haystack) {
    /* A document-level comment has no quoted text, so there is nothing to
     * look for and no evidence either way. It stays. Deleting a comment we
     * cannot show was dealt with is the one mistake here that loses writing
     * somebody did. */
    if (!comment.anchored || !comment.quote) return true;
    return haystack.indexOf(flatten(comment.quote)) !== -1;
  }

  /* Redraw everything that depends on which hunks are in: the right-hand
   * column of cards, the marks on the left-hand ones, and the button's count.
   * Cheap enough to run on every click, and running it on every click is what
   * makes the right-hand column mean "what you would be left with" rather than
   * "what Claude proposed". */
  function outcome() {
    var markdown = merged();
    var haystack = flatten(markdown);

    var taken = 0;
    for (var i = 0; i < accepted.length; i++) if (accepted[i]) taken++;

    var left = parts.result.querySelector(".merge__column--before .merge__cardlist");
    var right = parts.result.querySelector(".merge__column--after .merge__cardlist");
    if (!left || !right) return;

    left.innerHTML = "";
    right.innerHTML = "";

    if (!sent.length) {
      left.appendChild(hint("No comments were sent."));
      right.appendChild(hint("Nothing to carry over."));
    }

    sent.forEach(function (comment) {
      var stays = survives(comment, haystack);
      left.appendChild(card(comment, !stays));
      if (stays) right.appendChild(card(comment, false));
    });

    if (sent.length && !right.childNodes.length) {
      right.appendChild(hint("Every comment sent has been dealt with."));
    }

    /* Rejecting everything leaves the document byte-for-byte what it already
     * is, and the apply endpoint commits -- so offering it would be offering
     * an empty commit. Closing is the honest way out of that, and it is the
     * other button. */
    parts.go.disabled = taken === 0;
    if (taken === 0) {
      parts.go.textContent = "Nothing accepted";
    } else if (taken === hunkCount) {
      parts.go.textContent =
        "Apply " + taken + (taken === 1 ? " change" : " changes");
    } else {
      /* "1 of 2" is plural even though the count is one: the noun being
       * counted is the two, not the one. */
      parts.go.textContent = "Apply " + taken + " of " + hunkCount + " changes";
    }
  }

  function hint(text) {
    var el = document.createElement("p");
    el.className = "merge__hint";
    el.textContent = text;
    return el;
  }

  function card(comment, handled) {
    var el = document.createElement("div");
    el.className = "merge__card" + (handled ? " merge__card--handled" : "");
    if (comment.color) wear(el, comment.color);

    var head = document.createElement("div");
    head.className = "merge__cardhead";
    if (comment.semantic_type) {
      var tag = document.createElement("span");
      tag.className = "review__type";
      tag.textContent = comment.semantic_type;
      head.appendChild(tag);
    }
    var quote = document.createElement("span");
    quote.className =
      "review__quote" + (comment.anchored && comment.quote ? "" : " review__quote--whole");
    quote.textContent =
      comment.anchored && comment.quote
        ? "“" + comment.quote + "”"
        : "About the whole document";
    head.appendChild(quote);
    el.appendChild(head);

    var body = document.createElement("p");
    body.className = "review__body";
    body.textContent = comment.body || "";
    el.appendChild(body);

    if (handled) {
      var gone = document.createElement("p");
      gone.className = "merge__gone";
      gone.textContent = "The text this is about is being rewritten — it will be deleted.";
      el.appendChild(gone);
    }
    return el;
  }

  /* --- writing it down --------------------------------------------------------- */

  function apply() {
    var markdown = merged();
    var haystack = flatten(markdown);
    var doomed = sent.filter(function (comment) {
      return !survives(comment, haystack);
    });

    parts.go.disabled = true;
    parts.go.textContent = "Applying…";
    hideError();

    /* Comments first, then the document. The apply endpoint answers with the
     * rendered page, and a comment deleted after that answer arrived would
     * still be in the markup we are about to adopt -- back on screen, pinned
     * to a sentence that no longer exists, until the next reload. */
    erase(doomed)
      .then(function (failed) {
        return write(markdown).then(function (payload) {
          return { payload: payload, failed: failed };
        });
      })
      .then(function (got) {
        ui.adopt(got.payload);
        close();
        if (got.failed.length) {
          ui.toast(
            "Applied, but " +
              got.failed.length +
              " comment" +
              (got.failed.length === 1 ? "" : "s") +
              " could not be deleted",
            "error"
          );
        } else {
          ui.toast("Claude updated this document");
        }
      })
      .catch(function (error) {
        parts.go.disabled = false;
        outcome();
        showError("Nothing was written: " + error.message);
      });
  }

  function write(markdown) {
    return fetch("/api/agent/jobs/" + encodeURIComponent(jobId) + "/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: markdown }),
    }).then(function (response) {
      if (!response.ok) return ui.reject(response);
      return response.json();
    });
  }

  /* Delete the comments the accepted hunks dealt with.
   *
   * Resolves with the ones that could not be deleted rather than rejecting on
   * the first failure: a comment that outlives the sentence it was about is
   * untidy, and refusing to write the document because of it would be worse.
   * The reader is told, and can delete it by hand. */
  function erase(list) {
    var failed = [];
    return Promise.all(
      list.map(function (comment) {
        return fetch(
          ANNOTATIONS +
            "/" +
            encodeURIComponent(comment.id) +
            "?document=" +
            encodeURIComponent(DOC_ID),
          { method: "DELETE" }
        )
          .then(function (response) {
            if (!response.ok) failed.push(comment.id);
          })
          .catch(function () {
            failed.push(comment.id);
          });
      })
    ).then(function () {
      return failed;
    });
  }

  /* --- wiring ------------------------------------------------------------------ */

  button.addEventListener("click", open);

  /* While a job is running the dialog is the only view of it, and Escape
   * closing it would leave the reader with no way back -- but the work carries
   * on regardless, so this only guards the window in which it is watchable. */
  dialog.addEventListener("cancel", function (event) {
    if (stream) event.preventDefault();
  });

  /* Only offer this when there is somewhere for the work to go. A button that
   * queues a job nobody will collect is worse than no button at all. */
  ui.api().then(function (health) {
    if (!health) return;

    fetch("/api/agent/status?document=" + encodeURIComponent(DOC_ID))
      .then(function (response) {
        // Read the body either way. A server with no bridge configured
        // answers 503 here, and returning without draining it leaves the
        // request in flight as far as the browser is concerned -- which is
        // invisible until something waits for the network to go quiet.
        return response.json().then(
          function (payload) {
            return response.ok ? payload : null;
          },
          function () {
            return null;
          }
        );
      })
      .then(function (status) {
        if (!status || !status.connected) return;

        /* The list of actions is not consulted any more. What Claude is asked
         * to do is now the comments themselves, so a runner that is connected
         * is a runner that can do this -- there is no second condition. */
        button.title = "Review this document's comments and send them to Claude";
        button.hidden = false;
      })
      .catch(function () {
        /* no bridge configured on this server: leave the button hidden */
      });
  });
})();
