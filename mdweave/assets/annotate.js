/* mdweave -- select text, add a comment.
 *
 * Select something in the document and a "Comment" pill appears; click it,
 * type, save. The comment is POSTed to the local mdweave server, which writes
 * it into the document's .ann.json sidecar and re-renders the HTML, so a
 * reload shows exactly what you just created.
 *
 * Opened over file:// there is no server, so this degrades to read-only and
 * says so the first time you try to select something.
 *
 * The anchor maths here MIRRORS mdweave/anchors.py. If you change how a
 * selector is derived, change it in both places -- test_selector_round_trip
 * in tests/ is what catches a drift between them.
 */
(function () {
  "use strict";

  var mdw = window.mdweave;
  var ui = window.mdweaveUI;
  if (!mdw || !ui) return;

  var toast = ui.toast;
  var reject = ui.reject;

  var doc = mdw.doc;
  var layer = mdw.layer;
  var PENDING = "__pending__";
  var DOC_ID = document.body.dataset.document || "";
  var API = "/api/annotations";
  var CONTEXT = 48; // keep in step with anchors.CONTEXT_CHARS
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1 };

  var writable = false;
  var composer = null;
  var warned = false;

  /* --- flattened text index (mirror of anchors.TextIndex) --------------- */

  /* Walk every text node in document order, collapsing whitespace runs to a
   * single space -- including runs that straddle a node boundary -- and record
   * which (node, offset) each surviving character came from. */
  function buildIndex(root) {
    var chars = [];
    var origins = [];
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node;

    while ((node = walker.nextNode())) {
      if (node.parentNode && SKIP_TAGS[node.parentNode.nodeName]) continue;
      var value = node.nodeValue;
      for (var i = 0; i < value.length; i++) {
        var ch = value.charAt(i);
        if (/\s/.test(ch)) {
          if (chars.length && chars[chars.length - 1] === " ") continue;
          chars.push(" ");
        } else {
          chars.push(ch);
        }
        origins.push([node, i]);
      }
    }
    return { text: chars.join(""), origins: origins };
  }

  function normalize(text) {
    return text.replace(/\s+/g, " ").replace(/^ | $/g, "");
  }

  /** Every start offset of `needle` in `haystack`, including overlaps. */
  function allOccurrences(haystack, needle) {
    var out = [];
    var at = haystack.indexOf(needle);
    while (at !== -1) {
      out.push(at);
      at = haystack.indexOf(needle, at + 1);
    }
    return out;
  }

  /* Mirror of anchors.selector_for. */
  function selectorFor(index, start, end) {
    var quote = normalize(index.text.slice(start, end));
    var before = allOccurrences(index.text, quote).filter(function (s) {
      return s < start;
    });
    return {
      quote: quote,
      prefix: normalize(index.text.slice(Math.max(0, start - CONTEXT), start)),
      suffix: normalize(index.text.slice(end, end + CONTEXT)),
      occurrence: before.length,
    };
  }

  /* --- selection -> flattened offsets ----------------------------------- */

  /* Which text nodes does this range touch? Used to keep the offset scan from
   * calling comparePoint once per character in the whole document. */
  function touchedNodes(range) {
    var touched = new Set();
    var root = range.commonAncestorContainer;
    if (root.nodeType === Node.TEXT_NODE) {
      touched.add(root);
      return touched;
    }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      if (range.intersectsNode(node)) touched.add(node);
    }
    return touched;
  }

  /* A character occupies [offset, offset+1). It is selected when its start is
   * at or after the range start and its end is at or before the range end. */
  function rangeOffsets(index, range) {
    var touched = touchedNodes(range);
    var start = -1;
    var end = -1;

    for (var i = 0; i < index.origins.length; i++) {
      var node = index.origins[i][0];
      if (!touched.has(node)) continue;
      var offset = index.origins[i][1];
      if (
        range.comparePoint(node, offset) >= 0 &&
        range.comparePoint(node, offset + 1) <= 0
      ) {
        if (start === -1) start = i;
        end = i + 1;
      }
    }
    return start === -1 ? null : [start, end];
  }

  /* --- wrapping (mirror of inject.py) ----------------------------------- */

  function segmentsFor(index, start, end) {
    var segments = [];
    for (var i = start; i < end; i++) {
      var node = index.origins[i][0];
      var offset = index.origins[i][1];
      var last = segments[segments.length - 1];
      if (last && last.node === node) last.end = offset + 1;
      else segments.push({ node: node, start: offset, end: offset + 1 });
    }
    return segments;
  }

  /* Wrap a flattened span in <mark>s. A span crossing an element boundary
   * becomes several marks sharing one data-ann, which is what keeps the HTML
   * valid -- and is what the Python injector does too. */
  function wrapSpan(index, start, end, className, annId) {
    var segments = segmentsFor(index, start, end);
    var marks = [];

    segments.forEach(function (segment, position) {
      var mark = document.createElement("mark");
      mark.className = className;
      if (annId) {
        mark.dataset.ann = annId;
        if (position === 0) mark.id = "hl-" + annId;
      }
      // splitText leaves `middle` holding exactly the segment's characters.
      var middle = segment.node.splitText(segment.start);
      middle.splitText(segment.end - segment.start);
      middle.parentNode.replaceChild(mark, middle);
      mark.appendChild(middle);
      marks.push(mark);
    });
    return marks;
  }

  function unwrap(selector) {
    doc.querySelectorAll(selector).forEach(function (mark) {
      var parent = mark.parentNode;
      while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
      parent.removeChild(mark);
      parent.normalize();
    });
  }

  /* --- small UI pieces --------------------------------------------------- */

  var toolbar = document.createElement("div");
  toolbar.className = "selection-toolbar";
  toolbar.hidden = true;
  toolbar.innerHTML =
    '<button type="button" class="selection-toolbar__button">' +
    '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
    '<path fill="currentColor" d="M8 1.5c-3.6 0-6.5 2.4-6.5 5.4 0 1.7.9 3.2 2.4 4.2l-.6 2.6a.4.4 0 0 0 .6.4l2.9-1.6c.4.05.8.08 1.2.08 3.6 0 6.5-2.4 6.5-5.4S11.6 1.5 8 1.5Z"/>' +
    "</svg> Comment</button>";
  document.body.appendChild(toolbar);

  var toolbarButton = toolbar.querySelector("button");

  function hideToolbar() {
    toolbar.hidden = true;
  }

  function showToolbarAt(rect) {
    toolbar.hidden = false;
    var top = rect.top + window.scrollY - toolbar.offsetHeight - 8;
    var left = rect.left + window.scrollX + rect.width / 2 - toolbar.offsetWidth / 2;
    toolbar.style.top = Math.max(window.scrollY + 4, top) + "px";
    toolbar.style.left = Math.max(4, left) + "px";
  }

  /* --- composer ---------------------------------------------------------- */

  /* Drop the panel but leave the document alone. */
  function removeComposer() {
    if (composer) {
      composer.remove();
      composer = null;
    }
  }

  /* Abandon the whole editing session: panel and provisional highlight. */
  function closeComposer() {
    removeComposer();
    unwrap("mark.hl--pending");
    mdw.layout();
  }

  function openComposer(selector) {
    // Deliberately NOT closeComposer(): the pending highlight has already been
    // put in place by the caller and is what the composer anchors to. Unwrapping
    // it here left the panel with no anchor, so it fell back to the top-left
    // corner of the page.
    removeComposer();

    composer = document.createElement("div");
    composer.className = "composer";
    composer.dataset.ann = PENDING;
    composer.innerHTML =
      '<blockquote class="composer__quote"></blockquote>' +
      '<textarea class="composer__input" rows="4" placeholder="Add a comment…"></textarea>' +
      '<div class="composer__actions">' +
      '<button type="button" class="composer__button composer__button--ghost" data-act="cancel">Cancel</button>' +
      '<button type="button" class="composer__button" data-act="save">Comment</button>' +
      "</div>";
    composer.querySelector(".composer__quote").textContent = selector.quote;
    layer.appendChild(composer);
    mdw.layout();

    var input = composer.querySelector(".composer__input");
    var save = composer.querySelector('[data-act="save"]');
    input.focus();

    composer.querySelector('[data-act="cancel"]').addEventListener("click", closeComposer);
    save.addEventListener("click", function () {
      submit(selector, input, save);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        submit(selector, input, save);
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeComposer();
      }
    });
  }

  function submit(selector, input, saveButton) {
    var body = input.value.trim();
    if (!body) {
      input.focus();
      return;
    }

    saveButton.disabled = true;
    saveButton.textContent = "Saving…";

    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document: DOC_ID,
        quote: selector.quote,
        prefix: selector.prefix,
        suffix: selector.suffix,
        occurrence: selector.occurrence,
        body: body,
        author: localStorage.getItem("mdweave.author") || "me",
        at: new Date().toISOString(),
      }),
    })
      .then(function (response) {
        if (!response.ok) return reject(response);
        return response.json().then(function (payload) {
          return payload.annotation;
        });
      })
      .then(function (annotation) {
        // Re-derive the span: the pending marks changed the DOM, so the
        // index built before the composer opened is stale.
        unwrap("mark.hl--pending");
        var index = buildIndex(doc);
        var span = locate(index, annotation.target);
        closeComposer();

        if (span) {
          wrapSpan(index, span[0], span[1], "hl hl--amber hl--has-note", annotation.id);
          addNote(annotation);
        } else {
          toast("Saved, but could not place it here — reload to see it.", "warn");
        }
      })
      .catch(function (error) {
        saveButton.disabled = false;
        saveButton.textContent = "Comment";
        toast("Not saved: " + (error.message || "unknown error"), "error");
      });
  }

  /* Client-side twin of TextIndex.find, used to place a just-saved comment
   * without a page reload. */
  function locate(index, target) {
    var quote = normalize(target.quote);
    if (!quote) return null;

    var spans = allOccurrences(index.text, quote).map(function (s) {
      return [s, s + quote.length];
    });
    if (!spans.length) return null;

    var candidates = spans;
    var prefix = normalize(target.prefix || "");
    var suffix = normalize(target.suffix || "");

    if (prefix) {
      var byPrefix = candidates.filter(function (s) {
        var window = index.text.slice(Math.max(0, s[0] - prefix.length - 4), s[0]);
        return window.replace(/\s+$/, "").slice(-prefix.length) === prefix;
      });
      if (byPrefix.length) candidates = byPrefix;
    }
    if (suffix) {
      var bySuffix = candidates.filter(function (s) {
        var window = index.text.slice(s[1], s[1] + suffix.length + 4);
        return window.replace(/^\s+/, "").slice(0, suffix.length) === suffix;
      });
      if (bySuffix.length) candidates = bySuffix;
    }

    var idx = target.occurrence || 0;
    if (idx >= 0 && idx < spans.length && candidates.indexOf(spans[idx]) !== -1) {
      return spans[idx];
    }
    return candidates[0];
  }

  /* --- note construction (mirror of templates/document.html.j2) ---------- */

  function addNote(annotation) {
    var entry = (annotation.thread && annotation.thread[0]) || { author: "me", body: "" };

    var note = document.createElement("div");
    note.className = "note note--" + (annotation.color || "amber");
    note.id = "note-" + annotation.id;
    note.dataset.ann = annotation.id;
    note.dataset.status = annotation.status || "open";

    var pin = document.createElement("button");
    pin.type = "button";
    pin.className = "note__pin";
    pin.setAttribute("aria-expanded", "false");
    pin.title = annotation.target.quote;
    pin.textContent = (entry.author || "?").charAt(0).toUpperCase();

    var quote = document.createElement("blockquote");
    quote.className = "note__quote";
    quote.textContent = annotation.target.quote;

    var meta = document.createElement("div");
    meta.className = "note__meta";
    var author = document.createElement("span");
    author.className = "note__author";
    author.textContent = entry.author || "me";
    meta.appendChild(author);
    if (entry.at) {
      var when = document.createElement("time");
      when.className = "note__date";
      when.textContent = String(entry.at).slice(0, 10);
      meta.appendChild(when);
    }

    var text = document.createElement("p");
    text.className = "note__text";
    text.textContent = entry.body;

    var item = document.createElement("div");
    item.className = "note__entry";
    item.appendChild(meta);
    item.appendChild(text);

    var body = document.createElement("div");
    body.className = "note__body";
    body.id = "note-body-" + annotation.id;
    body.appendChild(quote);
    body.appendChild(item);
    if (writable) body.appendChild(noteActions(annotation.id));

    note.appendChild(pin);
    note.appendChild(body);
    layer.appendChild(note);

    mdw.register(note);
    mdw.setOpen(note, true);
    return note;
  }

  /* Persist a dragged note's position. Registered with notes.js only when the
   * server is reachable, so dragging over file:// simply is not saved. */
  function persistMove(annId, dx, dy) {
    fetch(API + "/" + encodeURIComponent(annId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document: DOC_ID,
        offset: dx || dy ? { dx: dx, dy: dy } : null,
      }),
    })
      .then(function (response) {
        if (!response.ok) return reject(response);
      })
      .catch(function (error) {
        toast("Position not saved: " + error.message, "error");
      });
  }

  function actionButton(className, label, onClick) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      onClick(button);
    });
    return button;
  }

  function noteActions(annId) {
    var row = document.createElement("div");
    row.className = "note__footer";

    row.appendChild(
      actionButton("note__reset", "Reset position", function () {
        mdw.resetPosition(annId);
      })
    );

    row.appendChild(
      actionButton("note__delete", "Delete", function (button) {
        button.disabled = true;
        fetch(
          API + "/" + encodeURIComponent(annId) + "?document=" + encodeURIComponent(DOC_ID),
          { method: "DELETE" }
        )
          .then(function (response) {
            if (!response.ok) return reject(response);
            mdw.remove(annId);
          })
          .catch(function (error) {
            button.disabled = false;
            toast("Not deleted: " + error.message, "error");
          });
      })
    );

    return row;
  }

  /* Give every note rendered into the page the same actions. */
  function addNoteActions() {
    layer.querySelectorAll(".note").forEach(function (note) {
      var body = note.querySelector(".note__body");
      if (body && !body.querySelector(".note__footer")) {
        body.appendChild(noteActions(note.dataset.ann));
      }
    });
  }

  /* --- selection wiring -------------------------------------------------- */

  document.addEventListener("selectionchange", function () {
    if (composer) return;

    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      hideToolbar();
      return;
    }

    var range = selection.getRangeAt(0);
    if (!doc.contains(range.commonAncestorContainer)) {
      hideToolbar();
      return;
    }
    if (!normalize(range.toString())) {
      hideToolbar();
      return;
    }

    if (!writable) {
      if (!warned) {
        warned = true;
        toast("Read-only. Run `mdweave serve` to add comments.", "warn");
      }
      return;
    }
    showToolbarAt(range.getBoundingClientRect());
  });

  toolbarButton.addEventListener("mousedown", function (event) {
    // Keep the selection alive: the default mousedown would collapse it.
    event.preventDefault();
  });

  toolbarButton.addEventListener("click", function () {
    var selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) return;

    var range = selection.getRangeAt(0);
    var index = buildIndex(doc);
    var span = rangeOffsets(index, range);
    hideToolbar();

    if (!span) {
      toast("Could not work out what was selected.", "error");
      return;
    }

    var selector = selectorFor(index, span[0], span[1]);
    if (!selector.quote) {
      toast("Select some text first.", "warn");
      return;
    }

    // Clear any previous session first, so its stale pending highlight cannot
    // be mistaken for this one's anchor.
    closeComposer();

    // Provisional highlight, so the target stays visible while typing -- and so
    // the composer has something to position itself against.
    wrapSpan(index, span[0], span[1], "hl hl--amber hl--pending", PENDING);
    selection.removeAllRanges();
    openComposer(selector);
  });

  document.addEventListener("mousedown", function (event) {
    if (toolbar.contains(event.target)) return;
    hideToolbar();
  });

  /* --- capability probe -------------------------------------------------- */

  /* Decide read-only vs editable by asking the server. Over file:// this
   * rejects immediately and the page simply stays readable. */
  if (DOC_ID) {
    ui.api().then(function (health) {
      if (!health || !health.ok) return;
      writable = true;
      document.body.classList.add("mdweave-editable");
      addNoteActions();
      mdw.setMoveHandler(persistMove);
    });
  }
})();
