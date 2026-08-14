/* mdweave -- sticky-note behaviour.
 *
 * Notes live in a right-hand gutter and are absolutely positioned to line up
 * with the highlight they belong to. Collapsed they are a small square; click
 * expands the card, click again collapses it (Preview.app style).
 *
 * Exposes `window.mdweave` so annotate.js can add notes at runtime and have
 * them behave exactly like the ones rendered into the page.
 *
 * No framework and no build step: this file is copied verbatim next to the
 * generated HTML.
 */
(function () {
  "use strict";

  var GAP = 8; // vertical breathing room between stacked notes

  var gutter = document.getElementById("gutter");
  var doc = document.getElementById("doc");
  if (!gutter || !doc) return;

  var notes = Array.prototype.slice.call(gutter.querySelectorAll(".note"));

  /** All highlight pieces belonging to one annotation. */
  function marksFor(id) {
    return Array.prototype.slice.call(
      doc.querySelectorAll('mark.hl[data-ann="' + CSS.escape(id) + '"]')
    );
  }

  function noteFor(id) {
    return gutter.querySelector('.note[data-ann="' + CSS.escape(id) + '"]');
  }

  /** Vertical offset of an element's anchor, relative to the gutter's top. */
  function anchorTop(id) {
    var mark = doc.querySelector('mark.hl[data-ann="' + CSS.escape(id) + '"]');
    if (!mark) return null;
    return mark.getBoundingClientRect().top - gutter.getBoundingClientRect().top;
  }

  /* --- layout ----------------------------------------------------------- */

  /* Place every note at its anchor, then push overlapping ones down so the
   * stack stays readable. A composer, if one is open, is laid out too. */
  function layout() {
    if (getComputedStyle(gutter).display === "none") return;

    var placed = [];

    gutter.querySelectorAll(".note, .composer").forEach(function (el) {
      var top = el.dataset.anchorTop
        ? parseFloat(el.dataset.anchorTop)
        : anchorTop(el.dataset.ann);
      if (top === null || isNaN(top)) {
        el.style.display = "none";
        return;
      }
      el.style.display = "";
      placed.push({ el: el, want: top });
    });

    placed.sort(function (a, b) {
      return a.want - b.want;
    });

    var cursor = -Infinity;
    placed.forEach(function (item) {
      var top = Math.max(item.want, cursor);
      item.el.style.top = top + "px";
      cursor = top + item.el.offsetHeight + GAP;
    });

    gutter.style.minHeight = doc.offsetHeight + "px";
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

  /** Wire up one note. Safe to call again on a note already registered. */
  function register(note) {
    if (note.dataset.wired === "1") return note;
    note.dataset.wired = "1";

    var pin = note.querySelector(".note__pin");
    if (pin) {
      pin.addEventListener("click", function (event) {
        event.stopPropagation();
        toggle(note);
      });
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
    gutter: gutter,
    notes: notes,
    layout: layout,
    register: register,
    setOpen: setOpen,
    marksFor: marksFor,
    noteFor: noteFor,
    anchorTop: anchorTop,
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
