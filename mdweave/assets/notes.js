/* mdweave -- sticky-note behaviour.
 *
 * A note is a small square pinned to the upper-right corner of the text it
 * annotates, floating above the page rather than parked in a gutter. Click it
 * and the card opens as a popover beside the pin; click again to close
 * (Preview.app style). The pin never moves when the card opens.
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

  /* Position one note. `taken` enables the un-stacking pass; pass null to skip
   * it, which is what dragging does so the note tracks the cursor exactly. */
  function place(note, bounds, taken) {
    var corner = anchorCorner(note.dataset.ann);
    if (!corner) {
      note.style.display = "none";
      return null;
    }
    note.style.display = "";

    var shift = offsetOf(note);
    var moved = !!(shift.dx || shift.dy);
    note.classList.toggle("note--moved", moved);

    var spot = {
      left: corner.left + NUDGE + shift.dx,
      top: corner.top - NUDGE + shift.dy,
    };

    // A note the reader placed by hand is left exactly where they put it.
    if (!moved && taken) {
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

    return { spot: spot, corner: corner, moved: moved };
  }

  /* Pin every note to its highlight. Pins that would land on top of each other
   * step sideways along the line rather than dropping down into the next one. */
  function layout() {
    var bounds = layer.getBoundingClientRect();
    var taken = [];
    var links = [];

    layer.querySelectorAll(".note").forEach(function (note) {
      var placed = place(note, bounds, taken);
      if (!placed) return;
      taken.push(placed.spot);
      if (placed.moved) links.push({ note: note, from: placed.corner, to: placed.spot });
    });

    drawLinks(links);
    layoutComposer(bounds);
  }

  /* --- leader lines ------------------------------------------------------ */

  /* A note dragged away from its text loses the visual link to it, so draw a
   * faint line back to the anchor. Only moved notes get one. */
  var SVG_NS = "http://www.w3.org/2000/svg";
  var canvas = document.createElementNS(SVG_NS, "svg");
  canvas.setAttribute("class", "notes-links");
  canvas.setAttribute("aria-hidden", "true");
  layer.insertBefore(canvas, layer.firstChild);

  function drawLinks(links) {
    while (canvas.firstChild) canvas.removeChild(canvas.firstChild);

    links.forEach(function (link) {
      var pin = link.note.querySelector(".note__pin");
      var line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("x1", link.from.left);
      line.setAttribute("y1", link.from.top);
      line.setAttribute("x2", link.to.left + PIN / 2);
      line.setAttribute("y2", link.to.top + PIN / 2);
      if (pin) line.setAttribute("stroke", getComputedStyle(pin).backgroundColor);
      canvas.appendChild(line);
    });
  }

  /* The composer stands in for a note's card before the note exists, so it is
   * placed exactly where that card would open: beside the pin the annotation is
   * about to get, flipping to the other side or upwards when short of room.
   *
   * It has no pin of its own, so unlike `orient` this moves the panel itself
   * rather than toggling a class. */
  function layoutComposer(bounds) {
    var composer = layer.querySelector(".composer");
    if (!composer) return;

    var corner = anchorCorner(composer.dataset.ann);
    if (!corner) return;

    var pinLeft = corner.left + NUDGE;
    var pinTop = corner.top - NUDGE;
    var width = composer.offsetWidth;
    var height = composer.offsetHeight;

    // Default: to the right of the pin, top edges aligned -- `.note__body`.
    var left = pinLeft + PIN + CARD_GAP;
    if (left + width > bounds.width - EDGE) {
      left = pinLeft - CARD_GAP - width; // the `.note--flip` position
    }
    left = Math.max(EDGE, Math.min(left, bounds.width - width - EDGE));

    var top = pinTop - CARD_GAP;
    if (top + height > bounds.height - EDGE) {
      top = pinTop + PIN + CARD_GAP - height; // the `.note--up` position
    }
    top = Math.max(EDGE, top);

    composer.style.left = left + "px";
    composer.style.top = top + "px";
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
      layer.classList.add("notes-layer--dragging");
      note.dataset.dx = String(base.dx + delta.dx);
      note.dataset.dy = String(base.dy + delta.dy);

      // Reposition only this note: a full layout on every pointermove would
      // measure every highlight in the document.
      var bounds = layer.getBoundingClientRect();
      var placed = place(note, bounds, null);
      if (placed) drawLinks([{ note: note, from: placed.corner, to: placed.spot }]);
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
      layer.classList.remove("notes-layer--dragging");
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

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    notes.forEach(function (note) {
      if (note.classList.contains("note--open")) setOpen(note, false);
    });
  });

  /* --- wide tables ------------------------------------------------------ */

  /* Tables are the one block that routinely exceeds the text column. Wrapping
   * them in a scroller keeps the measure intact without touching the renderer. */
  doc.querySelectorAll("table").forEach(function (table) {
    if (table.parentElement.classList.contains("table-scroll")) return;
    var wrap = document.createElement("div");
    wrap.className = "table-scroll";
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
  });

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
    register: register,
    setOpen: setOpen,
    resetPosition: resetPosition,
    setMoveHandler: function (fn) {
      moveHandler = fn;
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
