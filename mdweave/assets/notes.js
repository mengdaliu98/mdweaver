/* mdweave -- sticky-note behaviour.
 *
 * A note is a small square pinned to the upper-right corner of the text it
 * annotates, floating above the page rather than parked in a gutter. Click it
 * and the card opens as a popover beside the pin; click again to close
 * (Preview.app style). The pin never moves when the card opens.
 *
 * A comment about the document as a whole has no text to pin to. Those are
 * dealt out into a grid in the top-left corner of the page instead, filling
 * left to right and wrapping when a row runs out of room. Only where they
 * start is different: the drag, the open and close, the saved offset and the
 * card itself are all the same machinery, further down, untouched.
 *
 * Exposes `window.mdweave` so annotate.js can add notes at runtime and have
 * them behave exactly like the ones rendered into the page.
 *
 * No framework and no build step: this file is copied verbatim next to the
 * generated HTML.
 */
(function () {
  "use strict";

  var PIN = 22; // keep in step with --pin in base.css
  var NUDGE = 4; // how far the pin sits right of, and above, the highlight
  var CARD_GAP = 6; // pin-to-card gap; keep in step with --card-gap
  var STEP = PIN + 2; // shift applied to un-stack coincident pins
  var EDGE = 8; // keep cards this far inside the page
  var DRAG_SLOP = 4; // movement below this is a click, not a drag

  /* The corner grid the document-level pins are dealt into: how far in from
   * the top-left of the page it starts, and the air between neighbours.
   *
   * These two have no CSS twin, unlike PIN and CARD_GAP above -- the grid is
   * absolute positions this file writes, and nothing in the stylesheet has to
   * agree with it. So there is nothing here to keep in step by hand. */
  var DOC_MARGIN = 12;
  var DOC_GAP = 8;

  /* The slot a document-level comment is born in. Kept in step by hand with
   * annotate.js's DEFAULT_COLOR and model.DEFAULT_COLOR in the Python; the
   * composer has to name one, because `.composer` is painted from the four
   * variables that only a `note--cN` class defines.
   *
   * There is deliberately no palette here. The six swatches live in
   * annotate.js, where a test holds them to the slots Python knows, and a
   * second copy would be a third place to keep in step for one gesture. A
   * document-level comment is recolored from its own card, which carries that
   * picker already. */
  var DEFAULT_COLOR = "c5";

  /* What the pin says when there is no quote to show. The same sentence as
   * the one templates/document.html.j2 writes, for the same pin -- one from
   * the server at load, one from here after a save. */
  var DOC_LABEL = "Comment on this document";

  var ui = window.mdweaveUI;
  var DOC_ID = document.body.dataset.document || "";
  var API = "/api/annotations";

  var layer = document.getElementById("notes-layer");
  var doc = document.getElementById("doc");
  if (!layer || !doc) return;

  var notes = Array.prototype.slice.call(layer.querySelectorAll(".note"));

  /** All highlight pieces belonging to one annotation. */
  function marksFor(id) {
    return Array.prototype.slice.call(
      doc.querySelectorAll('mark.hl[data-ann="' + CSS.escape(id) + '"]')
    );
  }

  function noteFor(id) {
    return layer.querySelector('.note[data-ann="' + CSS.escape(id) + '"]');
  }

  /** The upper-right corner of an annotation's first highlight, relative to
   * the notes layer. Null when the annotation has no highlight on the page. */
  function anchorCorner(id) {
    if (!id) return null;
    var mark = doc.querySelector('mark.hl[data-ann="' + CSS.escape(id) + '"]');
    if (!mark) return null;
    var box = mark.getBoundingClientRect();
    var origin = layer.getBoundingClientRect();
    return { left: box.right - origin.left, top: box.top - origin.top };
  }

  /** Is this note about the document as a whole rather than a span of text? */
  function isDocLevel(note) {
    return note.dataset.level === "document";
  }

  /** The document-level notes, in the order the page lists them. That order
   * is the order of the grid, so a note keeps the slot it was given for as
   * long as the ones before it are still there. */
  function docLevelNotes() {
    return Array.prototype.slice.call(
      layer.querySelectorAll('.note[data-level="document"]')
    );
  }

  /* The nth slot of the corner grid: a row at a time, left to right, wrapping
   * when the next pin would cross the right-hand margin.
   *
   * A row is as wide as the page rather than some smaller cluster, because
   * "full" has to mean something the reader can see for themselves -- the
   * edge of the page is the only such line, and on any real window the first
   * two rows still sit in the white band above the prose. */
  function docSlot(n, bounds) {
    var pitch = PIN + DOC_GAP;
    var columns = Math.max(
      1,
      Math.floor((bounds.width - 2 * DOC_MARGIN + DOC_GAP) / pitch)
    );
    return {
      left: DOC_MARGIN + (n % columns) * pitch,
      top: DOC_MARGIN + Math.floor(n / columns) * pitch,
    };
  }

  /* --- layout ----------------------------------------------------------- */

  function overlaps(spot, taken) {
    for (var i = 0; i < taken.length; i++) {
      if (
        Math.abs(spot.left - taken[i].left) < PIN + 2 &&
        Math.abs(spot.top - taken[i].top) < PIN + 2
      ) {
        return true;
      }
    }
    return false;
  }

  /* Open the card towards whichever side has room for it. The pin stays put;
   * only the popover flips. */
  function orient(note, left, top, bounds) {
    var body = note.querySelector(".note__body");
    if (!body) return;

    note.classList.remove("note--flip", "note--up");
    if (left + PIN + CARD_GAP + body.offsetWidth > bounds.width - EDGE) {
      note.classList.add("note--flip");
    }
    if (top + body.offsetHeight > bounds.height - EDGE) {
      note.classList.add("note--up");
    }
  }

  function offsetOf(note) {
    return {
      dx: parseFloat(note.dataset.dx) || 0,
      dy: parseFloat(note.dataset.dy) || 0,
    };
  }

  /* Where a note sits before anybody moves it: beside its highlight when it
   * has one, in the corner grid when it is about the document instead.
   *
   * Null means the note has nowhere to be -- an anchored comment whose quote
   * is no longer in the prose. A document-level one can never be in that
   * position, which is the whole reason `data-level` is written into the
   * markup rather than guessed at from the missing `<mark>`. */
  function homeOf(note, bounds) {
    if (isDocLevel(note)) {
      return docSlot(docLevelNotes().indexOf(note), bounds);
    }
    var corner = anchorCorner(note.dataset.ann);
    if (!corner) return null;
    return { left: corner.left + NUDGE, top: corner.top - NUDGE };
  }

  /* Position one note. `taken` enables the un-stacking pass; pass null to skip
   * it, which is what dragging does so the note tracks the cursor exactly. */
  function place(note, bounds, taken) {
    var home = homeOf(note, bounds);
    if (!home) {
      note.style.display = "none";
      return null;
    }
    note.style.display = "";

    var shift = offsetOf(note);
    var moved = !!(shift.dx || shift.dy);
    note.classList.toggle("note--moved", moved);

    var spot = { left: home.left + shift.dx, top: home.top + shift.dy };

    // A note the reader placed by hand is left exactly where they put it --
    // and so is a document-level one, whose grid has already found it a slot
    // nothing else is standing in.
    if (!moved && taken && !isDocLevel(note)) {
      for (var guard = 0; overlaps(spot, taken) && guard < 12; guard++) {
        spot.left += STEP;
      }
    }

    // Never let a note escape the page entirely.
    spot.left = Math.max(0, Math.min(spot.left, bounds.width - PIN));
    spot.top = Math.max(0, spot.top);

    note.style.left = spot.left + "px";
    note.style.top = spot.top + "px";
    orient(note, spot.left, spot.top, bounds);

    return { spot: spot, moved: moved };
  }

  /* Pin every note to its highlight. Pins that would land on top of each other
   * step sideways along the line rather than dropping down into the next one. */
  function layout() {
    var bounds = layer.getBoundingClientRect();
    var taken = [];
    var all = Array.prototype.slice.call(layer.querySelectorAll(".note"));

    // The corner grid goes down first. It is fixed -- a document-level pin
    // does not step aside for anything -- so the anchored pins that follow
    // find those squares already claimed and walk around them, rather than
    // the first paragraph's comment landing on top of the corner.
    all.sort(function (a, b) {
      return (isDocLevel(b) ? 1 : 0) - (isDocLevel(a) ? 1 : 0);
    });

    all.forEach(function (note) {
      var placed = place(note, bounds, taken);
      if (placed) taken.push(placed.spot);
    });

    layoutComposers(bounds);
  }

  /* The composer stands in for a note's card before the note exists, so it is
   * placed exactly where that card would open: beside the pin the annotation is
   * about to get, flipping to the other side or upwards when short of room.
   *
   * It has no pin of its own, so unlike `orient` this moves the panel itself
   * rather than toggling a class. */
  function layoutComposer(composer, bounds) {
    var pin;
    if (isDocLevel(composer)) {
      // The slot the new pin is about to take: one past the last one dealt,
      // so the panel opens exactly where its own card will.
      pin = docSlot(docLevelNotes().length, bounds);
    } else {
      var corner = anchorCorner(composer.dataset.ann);
      if (!corner) return;
      pin = { left: corner.left + NUDGE, top: corner.top - NUDGE };
    }

    var width = composer.offsetWidth;
    var height = composer.offsetHeight;

    // Default: to the right of the pin, top edges aligned -- `.note__body`.
    var left = pin.left + PIN + CARD_GAP;
    if (left + width > bounds.width - EDGE) {
      left = pin.left - CARD_GAP - width; // the `.note--flip` position
    }
    left = Math.max(EDGE, Math.min(left, bounds.width - width - EDGE));

    var top = pin.top - CARD_GAP;
    if (top + height > bounds.height - EDGE) {
      top = pin.top + PIN + CARD_GAP - height; // the `.note--up` position
    }
    top = Math.max(EDGE, top);

    composer.style.left = left + "px";
    composer.style.top = top + "px";
  }

  /* Every panel in the layer, not the first one found. There are two kinds --
   * the one annotate.js opens against a selection and the document-level one
   * below -- and they anchor to different things, so picking whichever came
   * first in the markup would put one of them where the other belongs. */
  function layoutComposers(bounds) {
    layer.querySelectorAll(".composer").forEach(function (composer) {
      layoutComposer(composer, bounds);
    });
  }

  /* --- open / close ----------------------------------------------------- */

  function setOpen(note, open) {
    note.classList.toggle("note--open", open);

    var pin = note.querySelector(".note__pin");
    if (pin) pin.setAttribute("aria-expanded", open ? "true" : "false");

    marksFor(note.dataset.ann).forEach(function (mark) {
      mark.classList.toggle("hl--active", open);
    });

    layout();
  }

  function toggle(note) {
    setOpen(note, !note.classList.contains("note--open"));
  }

  /* --- dragging ---------------------------------------------------------- */

  /* Set by annotate.js to persist a move. Left unset when there is no server,
   * in which case dragging still works but only for the session. */
  var moveHandler = null;

  /* What to re-run after the notes layer has been replaced wholesale, so that
   * whatever annotate.js added to each card survives an in-place edit. */
  var refreshHandler = null;

  /* A drag ends with a click event the browser fires anyway; this stops that
   * click from also toggling the card open. */
  var swallowClick = false;

  function dragPixels(event, from) {
    return {
      dx: event.clientX - from.x,
      dy: event.clientY - from.y,
    };
  }

  function makeDraggable(note, pin) {
    var from = null;
    var base = null;
    var active = null;
    var moved = false;

    pin.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      active = event.pointerId;
      from = { x: event.clientX, y: event.clientY };
      base = offsetOf(note);
      moved = false;
      pin.setPointerCapture(active);
    });

    pin.addEventListener("pointermove", function (event) {
      if (active === null || event.pointerId !== active) return;

      var delta = dragPixels(event, from);
      // Ignore the tiny movement inherent in a click.
      if (!moved && Math.abs(delta.dx) + Math.abs(delta.dy) < DRAG_SLOP) return;

      moved = true;
      note.classList.add("note--dragging");
      note.dataset.dx = String(base.dx + delta.dx);
      note.dataset.dy = String(base.dy + delta.dy);

      // Reposition only this note: a full layout on every pointermove would
      // measure every highlight in the document.
      place(note, layer.getBoundingClientRect(), null);
    });

    function finish(event) {
      if (active === null || (event && event.pointerId !== active)) return;
      try {
        pin.releasePointerCapture(active);
      } catch (err) {
        /* the capture may already be gone */
      }
      active = null;
      note.classList.remove("note--dragging");
      if (!moved) return;

      moved = false;
      swallowClick = true;
      var shift = offsetOf(note);
      layout();
      if (moveHandler) moveHandler(note.dataset.ann, shift.dx, shift.dy);
    }

    pin.addEventListener("pointerup", finish);
    pin.addEventListener("pointercancel", finish);
  }

  /** Put a note back where its anchor says it belongs. */
  function resetPosition(id) {
    var note = noteFor(id);
    if (!note) return;
    delete note.dataset.dx;
    delete note.dataset.dy;
    layout();
    if (moveHandler) moveHandler(id, 0, 0);
  }

  /** Wire up one note. Safe to call again on a note already registered. */
  function register(note) {
    if (note.dataset.wired === "1") return note;
    note.dataset.wired = "1";

    var pin = note.querySelector(".note__pin");
    if (pin) {
      pin.addEventListener("click", function (event) {
        event.stopPropagation();
        if (swallowClick) {
          swallowClick = false;
          return;
        }
        toggle(note);
      });
      makeDraggable(note, pin);
    }

    // Hovering a collapsed note previews which text it refers to.
    note.addEventListener("mouseenter", function () {
      marksFor(note.dataset.ann).forEach(function (mark) {
        mark.classList.add("hl--active");
      });
    });
    note.addEventListener("mouseleave", function () {
      if (note.classList.contains("note--open")) return;
      marksFor(note.dataset.ann).forEach(function (mark) {
        mark.classList.remove("hl--active");
      });
    });

    if (notes.indexOf(note) === -1) notes.push(note);
    return note;
  }

  notes.forEach(register);

  /* Clicking highlighted text opens its note and scrolls it into view. */
  doc.addEventListener("click", function (event) {
    var mark = event.target.closest && event.target.closest("mark.hl--has-note");
    if (!mark) return;

    var note = noteFor(mark.dataset.ann);
    if (!note) return;

    toggle(note);
    if (note.classList.contains("note--open")) {
      note.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  });

  function closeAll() {
    notes.forEach(function (note) {
      if (note.classList.contains("note--open")) setOpen(note, false);
    });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeAll();
  });

  /* A click anywhere else puts the cards away.
   *
   * On the capture phase, so it runs before the handlers that open one: the
   * pin's own click and a click on highlighted text both bubble up to here,
   * and closing afterwards would shut the card that click had just opened.
   * A click inside a card is left alone -- selecting its text, or pressing
   * one of its buttons, is not a request to dismiss it. */
  document.addEventListener(
    "click",
    function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      if (target.closest(".note")) return; // working inside a card
      if (target.closest("mark.hl--has-note")) return; // the toggle handles it
      if (target.closest(".composer, .selection-toolbar")) return;
      closeAll();
    },
    true
  );

  /* --- commenting on the document itself --------------------------------- */

  /* Everything above this point belongs to a note that already exists. This
   * is the one gesture that makes one: the button in the page-action row,
   * which opens the composer with nothing selected.
   *
   * It lives here rather than in annotate.js because annotate.js is driven
   * entirely by a selection -- the toolbar it shows, the provisional
   * highlight it paints, the anchor it computes and sends. A comment about
   * the document has none of those, and the one thing it does need is the
   * corner grid, which is this file's. */

  var docComposer = null;

  function closeDocComposer() {
    if (!docComposer) return;
    docComposer.remove();
    docComposer = null;
    layout();
  }

  function openDocComposer() {
    // Pressing the button again while one is open is not a request for a
    // second panel, and throwing the first one away would take a half-typed
    // comment with it. Go back to it instead.
    if (docComposer) {
      docComposer.querySelector(".composer__input").focus();
      return;
    }

    var panel = document.createElement("div");
    // The same panel annotate.js opens against a selection, minus the quote:
    // there is no span to show back, and an empty blockquote would be a rule
    // hanging in mid-air above the box. `data-level` is what tells
    // `layoutComposer` to aim it at the corner grid instead of at a mark.
    panel.className = "composer note--" + DEFAULT_COLOR;
    panel.dataset.level = "document";
    panel.innerHTML =
      '<textarea class="composer__input" rows="4"' +
      ' placeholder="Add a comment about this document…"' +
      ' aria-label="' + DOC_LABEL + '"></textarea>' +
      '<div class="composer__actions">' +
      '<button type="button" class="composer__button composer__button--ghost" data-act="cancel">Cancel</button>' +
      '<button type="button" class="composer__button" data-act="save">Comment</button>' +
      "</div>";

    layer.appendChild(panel);
    docComposer = panel;
    layout();

    var input = panel.querySelector(".composer__input");
    var save = panel.querySelector('[data-act="save"]');
    input.focus();

    panel
      .querySelector('[data-act="cancel"]')
      .addEventListener("click", closeDocComposer);
    save.addEventListener("click", function () {
      submitDocComment(input, save);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        submitDocComment(input, save);
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeDocComposer();
      }
    });
  }

  function submitDocComment(input, saveButton) {
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
        // `level` is said out loud rather than left to a missing `quote`:
        // "about the document" is something the reader asked for, and a
        // selection that silently came out empty is a bug. The server can
        // tell the two apart only if this field exists.
        level: "document",
        body: body,
        color: DEFAULT_COLOR,
        semantic_type: null,
        author: localStorage.getItem("mdweave.author") || "me",
        at: new Date().toISOString(),
      }),
    })
      .then(function (response) {
        if (!response.ok) return ui.reject(response);
        return response.json();
      })
      .then(function (payload) {
        closeDocComposer();
        addDocNote(payload.annotation);
      })
      .catch(function (error) {
        saveButton.disabled = false;
        saveButton.textContent = "Comment";
        ui.toast("Not saved: " + (error.message || "unknown error"), "error");
      });
  }

  /* Mirror of the note block in templates/document.html.j2, for the comment
   * that has just been saved -- the same bargain annotate.js makes for an
   * anchored one. Fetching the page again would be the authoritative answer
   * and is what `refresh()` is for, but it re-renders the prose as well to
   * show a pin the browser already has everything it needs to draw. */
  function addDocNote(annotation) {
    var entry = (annotation.thread && annotation.thread[0]) || {
      author: "me",
      body: "",
    };

    var note = document.createElement("div");
    note.className = "note note--" + (annotation.color_token || DEFAULT_COLOR);
    note.id = "note-" + annotation.id;
    note.dataset.ann = annotation.id;
    note.dataset.status = annotation.status || "open";
    note.dataset.level = "document";

    var pin = document.createElement("button");
    pin.type = "button";
    pin.className = "note__pin";
    pin.setAttribute("aria-expanded", "false");
    pin.setAttribute("aria-controls", "note-body-" + annotation.id);
    pin.title = DOC_LABEL;

    var initial = document.createElement("span");
    initial.className = "note__initial";
    initial.setAttribute("aria-hidden", "true");
    initial.textContent = (entry.author || "?").charAt(0).toUpperCase();
    pin.appendChild(initial);

    var spoken = document.createElement("span");
    spoken.className = "sr-only";
    spoken.textContent = DOC_LABEL;
    pin.appendChild(spoken);

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
    body.setAttribute("role", "region");
    body.appendChild(item);

    note.appendChild(pin);
    note.appendChild(body);
    layer.appendChild(note);

    register(note);
    // annotate.js hangs the color picker, "Reset position" and "Delete" off
    // every card, and registers that as the handler to re-run whenever the
    // layer gains cards it has not seen. Calling it is how the new card gets
    // them without this file learning what any of them are.
    if (refreshHandler) refreshHandler();
    setOpen(note, true);
    return note;
  }

  /* The button only appears once /api/health has answered, like every other
   * control that reaches back to the server: over file:// there is nowhere to
   * put a comment, and a button that does nothing is worse than none. */
  var addButton = document.getElementById("add-comment");
  if (addButton && ui) {
    addButton.addEventListener("click", openDocComposer);
    ui.api().then(function (health) {
      if (health) addButton.hidden = false;
    });
  }

  /* --- wide tables ------------------------------------------------------ */

  /* Tables are the one block that routinely exceeds the text column. Wrapping
   * them in a scroller keeps the measure intact without touching the renderer. */
  function wrapTables() {
    doc.querySelectorAll("table").forEach(function (table) {
      if (table.parentElement.classList.contains("table-scroll")) return;
      var wrap = document.createElement("div");
      wrap.className = "table-scroll";
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    });
  }

  wrapTables();

  /* Re-adopt the page after edit.js has swapped the prose underneath us. The
   * old note elements are gone with the old markup, so the cached list has to
   * be rebuilt; `register` is idempotent, so re-wiring costs nothing. */
  function refresh() {
    // The layer was replaced wholesale, so a composer that was standing in it
    // went with the markup. Forget it rather than hold a detached element
    // that the next `closeDocComposer` would tidy up out of the document.
    docComposer = null;
    notes = Array.prototype.slice.call(layer.querySelectorAll(".note"));
    notes.forEach(register);
    wrapTables();
    // Before layout, not after: annotate.js appends a footer of actions to
    // each card, which changes its height, and a card measured without one
    // is placed as though it were shorter than it is.
    if (refreshHandler) refreshHandler();
    layout();
  }

  /* --- keep in sync ----------------------------------------------------- */

  layout();
  window.addEventListener("resize", layout);
  window.addEventListener("load", layout);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(layout);
  if (window.ResizeObserver) new ResizeObserver(layout).observe(doc);

  window.mdweave = {
    doc: doc,
    layer: layer,
    notes: notes,
    layout: layout,
    refresh: refresh,
    register: register,
    setOpen: setOpen,
    resetPosition: resetPosition,
    setMoveHandler: function (fn) {
      moveHandler = fn;
    },
    setRefreshHandler: function (fn) {
      refreshHandler = fn;
    },
    marksFor: marksFor,
    noteFor: noteFor,
    anchorCorner: anchorCorner,
    remove: function (id) {
      marksFor(id).forEach(function (mark) {
        var parent = mark.parentNode;
        while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
        parent.removeChild(mark);
        parent.normalize();
      });
      var note = noteFor(id);
      if (note) {
        var at = notes.indexOf(note);
        if (at !== -1) notes.splice(at, 1);
        note.remove();
      }
      layout();
    },
  };
})();
