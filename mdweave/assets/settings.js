/* Settings: a floating window, and the two things the reader owns inside it.
 *
 * Two sections, because two different things are configured here and they fail
 * differently. **Color schemes** repaint every page in the knowledge base, so
 * saving one rewrites the HTML and reloads. **Comment card semantic types** --
 * what kinds of remark exist, and what Claude is told to do about each -- touch
 * no document at all: they are a vocabulary, read by the composer when a
 * comment is written. Each section therefore carries its own save button,
 * since one Apply over both would promise that the cheap edit costs what the
 * expensive one does.
 *
 * Three ideas hold the color half together.
 *
 * A color is a **slot**, not a hue. An annotation says "the third color",
 * so switching schemes moves every highlight to the third color of the new
 * one and nothing has to be rewritten. Dragging the swatches is the opposite
 * case: the colors move, so the annotations have to move with them to stay
 * the color they were -- that is the `remap` the server is sent, and it is
 * the one edit here that touches a sidecar.
 *
 * **Preview is a lie told carefully.** It writes the same custom properties
 * the generated stylesheet writes, into one <style> element, so the page you
 * are looking at is the page you would get. Nothing reaches the disk until
 * Apply, and closing the window takes the lie back.
 *
 * **The window is not a modal.** It is moved by its title bar and leaves the
 * document underneath live, because the whole point is to watch the prose
 * change color while you pick.
 */
(function () {
  "use strict";

  var ui = window.mdweaveUI;
  if (!ui) return;

  var SLOTS = 6;
  var PREVIEW_ID = "mdweave-preview";

  var state = null; // { schemes, active, editing, order, previewing }
  var win = null;

  /* --- talking to the server ---------------------------------------------- */

  function get(path) {
    return fetch(path, { headers: { Accept: "application/json" } }).then(function (r) {
      if (!r.ok) throw new Error("could not read the schemes");
      return r.json();
    });
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      // The body may not be JSON at all: a server too old to know a route
      // answers with an HTML 404, and letting the parse failure through would
      // report a syntax error where the truth is "that endpoint is not there".
      return r
        .json()
        .catch(function () {
          return null;
        })
        .then(function (data) {
          if (!r.ok) {
            throw new Error((data && data.error) || "the server refused it");
          }
          return data;
        });
    });
  }

  /* The vocabulary, or an empty one.
   *
   * Soft, where the schemes are not. A knowledge base has never had a semantic
   * type until somebody adds one, and a server that predates the feature has
   * no route to answer with -- neither is an error, and neither may be allowed
   * to cost the reader the color section sitting above it. */
  function getSemantics() {
    return fetch("/api/semantics", { headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .catch(function () {
        return null;
      })
      .then(function (data) {
        return data || { types: [], defaults: {} };
      });
  }

  /* --- the colors, worked out the way the server works them out -----------
   *
   * Only the fill is chosen; the pin and the card are derived from it. The
   * arithmetic is mirrored from scheme.derive in the Python -- if the two ever
   * disagree the preview stops being a preview, so `test_schemes.py` checks
   * them against each other rather than trusting this comment. */

  function toRgb(hex) {
    var v = hex.replace("#", "");
    return [
      parseInt(v.slice(0, 2), 16),
      parseInt(v.slice(2, 4), 16),
      parseInt(v.slice(4, 6), 16),
    ];
  }

  function channel(c) {
    c /= 255;
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  }

  function luminance(rgb) {
    return (
      0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])
    );
  }

  function contrast(a, b) {
    var x = luminance(a);
    var y = luminance(b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  }

  function toHls(rgb) {
    var r = rgb[0] / 255, g = rgb[1] / 255, b = rgb[2] / 255;
    var max = Math.max(r, g, b), min = Math.min(r, g, b);
    var l = (max + min) / 2;
    if (max === min) return [0, l, 0];
    var d = max - min;
    var s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    var h;
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
    else if (max === g) h = ((b - r) / d + 2) / 6;
    else h = ((r - g) / d + 4) / 6;
    return [h, l, s];
  }

  function hue(p, q, t) {
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  }

  function fromHls(h, l, s) {
    if (s === 0) {
      var v = Math.round(l * 255);
      return [v, v, v];
    }
    var q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    var p = 2 * l - q;
    return [
      Math.round(hue(p, q, h + 1 / 3) * 255),
      Math.round(hue(p, q, h) * 255),
      Math.round(hue(p, q, h - 1 / 3) * 255),
    ];
  }

  function shift(base, light, satScale) {
    var hls = toHls(base);
    return fromHls(hls[0], light, Math.min(1, hls[2] * (satScale === undefined ? 1 : satScale)));
  }

  function darkenUntil(base, target, against, satScale) {
    var out = base;
    for (var step = 0; step < 100; step++) {
      out = shift(base, 0.6 - step * 0.005, satScale);
      if (contrast(out, against) >= target) break;
    }
    return out;
  }

  var WHITE = [255, 255, 255];

  function css(rgb) {
    return "rgb(" + rgb[0] + " " + rgb[1] + " " + rgb[2] + ")";
  }

  function derive(fill) {
    var base = toRgb(fill);
    var noteBg = shift(base, 0.965, 0.9);
    return {
      bg: base,
      edge: darkenUntil(base, 5.0, WHITE, 1.35),
      noteBg: noteBg,
      noteInk: darkenUntil(base, 8.0, noteBg, 1.2),
    };
  }

  /* --- preview ------------------------------------------------------------- */

  function previewCss(scheme) {
    var out = [
      ":root {",
      "  --paper: " + css(toRgb(scheme.paper)) + ";",
      "  --sidebar-bg: " + css(toRgb(scheme.sidebar)) + ";",
    ];
    for (var i = 0; i < SLOTS; i++) {
      var got = derive(scheme.colors[i]);
      var slot = "c" + (i + 1);
      out.push("  --hl-" + slot + "-bg: " + css(got.bg) + ";");
      out.push("  --hl-" + slot + "-edge: " + css(got.edge) + ";");
      out.push("  --note-" + slot + "-bg: " + css(got.noteBg) + ";");
      out.push("  --note-" + slot + "-ink: " + css(got.noteInk) + ";");
    }
    out.push("}");
    return out.join("\n");
  }

  function showPreview(scheme) {
    var tag = document.getElementById(PREVIEW_ID);
    if (!tag) {
      tag = document.createElement("style");
      tag.id = PREVIEW_ID;
      document.head.appendChild(tag);
    }
    tag.textContent = previewCss(scheme);
  }

  function clearPreview() {
    var tag = document.getElementById(PREVIEW_ID);
    if (tag) tag.parentNode.removeChild(tag);
  }

  function repaint() {
    if (state.previewing) showPreview(state.editing);
    else clearPreview();
  }

  /* --- the window ---------------------------------------------------------- */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* One of the window's two halves: a heading and whatever follows it. A
   * heading rather than a bare label, because these are the two topics the
   * window covers and a screen reader should be able to jump between them. */
  function section(title) {
    var node = el("div", "settings__section");
    node.appendChild(el("h2", "settings__title", title));
    return node;
  }

  /* Moved by its bar, and kept on screen: a window dragged off the edge is a
   * window you cannot get back without knowing where it went. */
  function draggable(node, handle) {
    var from = null;
    handle.addEventListener("pointerdown", function (event) {
      if (event.target.closest("button")) return;
      var box = node.getBoundingClientRect();
      from = { x: event.clientX - box.left, y: event.clientY - box.top };
      handle.setPointerCapture(event.pointerId);
      node.classList.add("settings--moving");
    });
    handle.addEventListener("pointermove", function (event) {
      if (!from) return;
      var maxX = window.innerWidth - node.offsetWidth;
      var maxY = window.innerHeight - node.offsetHeight;
      node.style.left = Math.max(0, Math.min(maxX, event.clientX - from.x)) + "px";
      node.style.top = Math.max(0, Math.min(maxY, event.clientY - from.y)) + "px";
    });
    handle.addEventListener("pointerup", function () {
      from = null;
      node.classList.remove("settings--moving");
    });
  }

  function swatchRow() {
    var row = el("div", "settings__slots");
    row.setAttribute("role", "list");
    row.setAttribute("aria-label", "The six colors, in order. Drag to rearrange.");

    state.editing.colors.forEach(function (fill, index) {
      var cell = el("div", "settings__slot");
      cell.draggable = true;
      cell.dataset.index = String(index);
      cell.setAttribute("role", "listitem");

      var picker = document.createElement("input");
      picker.type = "color";
      picker.value = fill;
      picker.className = "settings__picker";
      picker.setAttribute("aria-label", "Color " + (index + 1));
      picker.addEventListener("input", function () {
        state.editing.colors[index] = picker.value;
        repaint();
      });

      cell.appendChild(picker);
      cell.appendChild(el("span", "settings__ordinal", String(index + 1)));
      row.appendChild(cell);
    });

    wireSlotDrag(row);
    return row;
  }

  /* Dragging a swatch moves the color and takes its annotations with it, so
   * the arrangement has to remember where each color *started*: `order` is
   * the original 1-based slots in their new positions, which is exactly what
   * the server needs to renumber by. */
  function wireSlotDrag(row) {
    var picked = null;

    row.addEventListener("dragstart", function (event) {
      var cell = event.target.closest(".settings__slot");
      if (!cell) return;
      picked = Number(cell.dataset.index);
      event.dataTransfer.effectAllowed = "move";
      // Firefox will not start a drag without something on the transfer.
      event.dataTransfer.setData("text/plain", String(picked));
      cell.classList.add("settings__slot--dragging");
    });

    row.addEventListener("dragover", function (event) {
      if (picked === null) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
    });

    row.addEventListener("drop", function (event) {
      if (picked === null) return;
      event.preventDefault();
      var cell = event.target.closest(".settings__slot");
      if (!cell) return;
      var onto = Number(cell.dataset.index);
      if (onto === picked) return;

      state.editing.colors.splice(onto, 0, state.editing.colors.splice(picked, 1)[0]);
      state.order.splice(onto, 0, state.order.splice(picked, 1)[0]);
      picked = null;
      render();
      repaint();
    });

    row.addEventListener("dragend", function () {
      picked = null;
      var moving = row.querySelector(".settings__slot--dragging");
      if (moving) moving.classList.remove("settings__slot--dragging");
    });
  }

  function colorField(label, key) {
    var field = el("label", "settings__field");
    field.appendChild(el("span", "settings__label", label));

    var picker = document.createElement("input");
    picker.type = "color";
    picker.className = "settings__picker";
    picker.dataset.field = key;
    picker.setAttribute("aria-label", label);
    picker.value = state.editing[key];
    picker.addEventListener("input", function () {
      state.editing[key] = picker.value;
      repaint();
    });

    field.appendChild(picker);
    return field;
  }

  function body() {
    var wrap = el("div", "settings__body");

    var chooser = el("div", "settings__row");
    chooser.appendChild(el("span", "settings__label", "Scheme"));

    var select = document.createElement("select");
    select.className = "settings__select";
    state.schemes.forEach(function (scheme) {
      var option = document.createElement("option");
      option.value = scheme.name;
      option.textContent = scheme.name;
      option.selected = scheme.name === state.editing.name;
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      edit(select.value);
    });
    chooser.appendChild(select);

    var add = el("button", "settings__button", "New");
    add.type = "button";
    add.title = "Add a scheme, starting from this one";
    add.addEventListener("click", addScheme);
    chooser.appendChild(add);

    // Absent at one scheme rather than present and dead. A scheme is what the
    // page is painted with, so there has to be one; a disabled Delete would
    // only invite a reader to work out why, and the answer is a rule they can
    // already see -- there is nothing else in the list.
    if (state.schemes.length > 1) {
      var drop = el("button", "settings__button settings__button--drop", "Delete");
      drop.type = "button";
      drop.title = "Delete this scheme";
      drop.addEventListener("click", deleteScheme);
      chooser.appendChild(drop);
    }

    wrap.appendChild(chooser);

    var name = el("label", "settings__field");
    name.appendChild(el("span", "settings__label", "Name"));
    var input = document.createElement("input");
    input.type = "text";
    input.className = "settings__text";
    input.value = state.editing.name;
    input.addEventListener("input", function () {
      state.editing.name = input.value;
    });
    name.appendChild(input);
    wrap.appendChild(name);

    wrap.appendChild(colorField("Left panel", "sidebar"));
    wrap.appendChild(colorField("Document background", "paper"));

    wrap.appendChild(el("span", "settings__label", "Highlights and comments"));
    wrap.appendChild(swatchRow());

    return wrap;
  }

  function footer() {
    var bar = el("div", "settings__footer");

    var preview = el("label", "settings__preview");
    var box = document.createElement("input");
    box.type = "checkbox";
    box.checked = state.previewing;
    box.addEventListener("change", function () {
      state.previewing = box.checked;
      repaint();
    });
    preview.appendChild(box);
    preview.appendChild(el("span", null, "Preview"));
    bar.appendChild(preview);

    var apply = el("button", "settings__button settings__button--go", "Apply");
    apply.type = "button";
    apply.addEventListener("click", apply_);
    bar.appendChild(apply);

    return bar;
  }

  /* --- comment card semantic types -----------------------------------------
   *
   * A comment says what is wrong; its semantic type says what kind of remark
   * it is, and carries the instruction Claude is given for putting that kind
   * of thing right. This section is where the vocabulary is written: the kinds
   * themselves, their instructions, and which kind each of the six colors
   * reaches for when a comment is started in it.
   *
   * Neither half of a pairing is required. A color need not have a default --
   * most do not -- and a kind need not have an instruction, since an
   * unexplained kind still groups remarks usefully and still tells a model
   * that these three are the same sort of thing.
   *
   * Types are held by POSITION while the window is open, not by name. A select
   * stores the index of the kind it points at, so renaming "question" to
   * "query" in the field above does not have to chase down and rewrite every
   * default that named it -- the name is only read back at Save, which is also
   * the only moment any of this is a vocabulary rather than a form.
   */

  /** The server's shape -- names everywhere -- turned into the editing shape. */
  function adoptSemantics(data) {
    var types = (data.types || []).map(function (kind) {
      return { name: kind.name || "", instruction: kind.instruction || "" };
    });

    var slots = [];
    for (var n = 1; n <= SLOTS; n++) {
      var wanted = String((data.defaults || {})[String(n)] || "").toLowerCase();
      var at = -1;
      types.forEach(function (kind, index) {
        if (wanted && kind.name.toLowerCase() === wanted) at = index;
      });
      // A default naming a kind that no longer exists is no default at all,
      // which is the same reading the Python takes of the file on disk.
      slots.push(at);
    }
    return { types: types, slots: slots };
  }

  function text(value, placeholder, label, onInput) {
    var input = document.createElement("input");
    input.type = "text";
    input.className = "semantics__field";
    input.value = value;
    input.placeholder = placeholder;
    input.setAttribute("aria-label", label);
    input.addEventListener("input", function () {
      onInput(input.value);
    });
    return input;
  }

  function typeRow(kind, index) {
    var row = el("div", "semantics__type");

    var head = el("div", "semantics__head");
    var name = text(kind.name, "Name", "Name of kind " + (index + 1), function (value) {
      kind.name = value;
      // The selects below point at this row by position, so their labels are
      // the one thing a rename has to keep in step with by hand.
      relabel(index, value);
    });
    name.className = "semantics__field semantics__name";
    head.appendChild(name);

    var drop = el("button", "semantics__drop", "×");
    drop.type = "button";
    drop.title = "Delete this kind";
    drop.setAttribute("aria-label", "Delete the kind " + (kind.name || index + 1));
    drop.addEventListener("click", function () {
      dropType(index);
    });
    head.appendChild(drop);
    row.appendChild(head);

    row.appendChild(
      text(
        kind.instruction,
        "What Claude should do about it (optional)",
        "Claude instruction for kind " + (index + 1),
        function (value) {
          kind.instruction = value;
        }
      )
    );

    return row;
  }

  /* Rename in place rather than re-render: redrawing the section on every
   * keystroke would take the focus out of the field being typed into. */
  function relabel(index, value) {
    if (!win) return;
    win.querySelectorAll(".semantics__pick").forEach(function (select) {
      var option = select.options[index + 1]; // 0 is "no default"
      if (option) option.textContent = value || "untitled";
    });
  }

  function dropType(index) {
    state.kinds.types.splice(index, 1);
    state.kinds.slots = state.kinds.slots.map(function (at) {
      if (at === index) return -1; // the color loses a default it no longer has
      return at > index ? at - 1 : at;
    });
    renderSemantics();
  }

  function addType() {
    state.kinds.types.push({ name: "", instruction: "" });
    renderSemantics();
    // Straight into the field that has to be filled in: a nameless kind is the
    // one thing the server will refuse.
    var fields = win.querySelectorAll(".semantics__name");
    if (fields.length) fields[fields.length - 1].focus();
  }

  /* One row per color slot: the color itself, and the kind a comment started
   * in it is given. The swatch is drawn from the same generated `--hl-*`
   * variables the highlights use, so this is the color as the page paints it
   * and not a second copy of the palette. */
  function defaultRow(slot) {
    var row = el("div", "semantics__default");

    var dot = el("span", "semantics__dot swatch--c" + slot);
    dot.setAttribute("aria-hidden", "true");
    row.appendChild(dot);
    row.appendChild(el("span", "semantics__ordinal", String(slot)));

    var select = document.createElement("select");
    select.className = "semantics__pick";
    select.setAttribute("aria-label", "Default kind for color " + slot);

    var none = document.createElement("option");
    none.value = "-1";
    none.textContent = "No default";
    select.appendChild(none);

    state.kinds.types.forEach(function (kind, index) {
      var option = document.createElement("option");
      option.value = String(index);
      option.textContent = kind.name || "untitled";
      select.appendChild(option);
    });

    select.value = String(state.kinds.slots[slot - 1]);
    select.addEventListener("change", function () {
      state.kinds.slots[slot - 1] = Number(select.value);
    });

    row.appendChild(select);
    return row;
  }

  function semanticsBody() {
    var wrap = el("div", "semantics");

    var list = el("div", "semantics__list");
    if (!state.kinds.types.length) {
      list.appendChild(
        el(
          "p",
          "semantics__empty",
          "No kinds yet. Add one and it appears under every comment box."
        )
      );
    }
    state.kinds.types.forEach(function (kind, index) {
      list.appendChild(typeRow(kind, index));
    });
    wrap.appendChild(list);

    var add = el("button", "semantics__add", "Add a kind");
    add.type = "button";
    add.addEventListener("click", addType);
    wrap.appendChild(add);

    // Read as a sentence down the column: color 3, question. Most colors will
    // say "No default", which is the honest resting state -- a default is a
    // shortcut for the one or two kinds somebody reaches for constantly.
    wrap.appendChild(el("span", "settings__label", "Default kind for each color"));
    var defaults = el("div", "semantics__defaults");
    for (var slot = 1; slot <= SLOTS; slot++) defaults.appendChild(defaultRow(slot));
    wrap.appendChild(defaults);

    var bar = el("div", "semantics__footer");
    var save = el("button", "semantics__save", "Save kinds");
    save.type = "button";
    save.addEventListener("click", saveSemantics);
    bar.appendChild(save);
    wrap.appendChild(bar);

    return wrap;
  }

  function saveSemantics() {
    var types = state.kinds.types.map(function (kind) {
      return { name: (kind.name || "").trim(), instruction: (kind.instruction || "").trim() };
    });

    // Checked here as well as on the server, because the server can only
    // answer about the first offender and the reader is looking at all of
    // them. The two refusals are the same two: nameless, and named twice.
    var seen = {};
    for (var i = 0; i < types.length; i++) {
      if (!types[i].name) {
        ui.toast("A kind needs a name", "error");
        return;
      }
      var key = types[i].name.toLowerCase();
      if (seen[key]) {
        ui.toast('Two kinds are both called "' + types[i].name + '"', "error");
        return;
      }
      seen[key] = true;
    }

    var defaults = {};
    state.kinds.slots.forEach(function (at, index) {
      if (at >= 0 && types[at]) defaults[String(index + 1)] = types[at].name;
    });

    post("/api/semantics", { types: types, defaults: defaults })
      .then(function (data) {
        state.kinds = adoptSemantics(data || { types: types, defaults: defaults });
        renderSemantics();
        // No reload. Nothing on disk was re-rendered -- a vocabulary is read
        // when a comment is written, not baked into the HTML -- so the one
        // thing that has to hear about this is the composer's own copy.
        if (window.mdweaveSemantics) window.mdweaveSemantics.reload();
        ui.toast("Saved");
      })
      .catch(function (error) {
        ui.toast(error.message, "error");
      });
  }

  function renderSemantics() {
    if (!win) return;
    var old = win.querySelector(".semantics");
    if (old) old.parentNode.replaceChild(semanticsBody(), old);
  }

  function render() {
    if (!win) return;
    var old = win.querySelector(".settings__body");
    if (old) old.parentNode.replaceChild(body(), old);
    var bar = win.querySelector(".settings__footer");
    if (bar) bar.parentNode.replaceChild(footer(), bar);
  }

  function edit(name) {
    var found = null;
    state.schemes.forEach(function (scheme) {
      if (scheme.name === name) found = scheme;
    });
    if (!found) return;

    // A copy: nothing the reader does here touches the saved scheme until
    // Apply, which is what makes Preview safe to leave on.
    state.editing = JSON.parse(JSON.stringify(found));
    state.from = name;
    // Identity of each color before any dragging, so a reorder can be
    // described to the server as "slot 5 is now first".
    state.order = [1, 2, 3, 4, 5, 6];
    render();
    repaint();
  }

  function addScheme() {
    var copy = JSON.parse(JSON.stringify(state.editing));
    var n = 2;
    var base = copy.name.replace(/ \d+$/, "");
    var taken = {};
    state.schemes.forEach(function (s) { taken[s.name] = true; });
    while (taken[base + " " + n]) n += 1;
    copy.name = base + " " + n;

    state.schemes.push(copy);
    state.editing = copy;
    state.from = null; // brand new, so nothing of its is on disk to reorder
    state.order = [1, 2, 3, 4, 5, 6];
    render();
    repaint();
  }

  /* Deleting is saving the list without it.
   *
   * There is no delete route and there does not need to be: /api/schemes is
   * given the whole list every time and writes it over the theme file, so an
   * entry that is simply not in the list is gone. The one shape that request
   * cannot express is an empty list -- which is also the one state the reader
   * must not be left in, since a scheme is what the page is painted with. The
   * button is therefore absent at a single scheme rather than disabled.
   *
   * What takes over is deliberately not whatever is being edited. The active
   * scheme is kept if it survives, so deleting one you are not looking at
   * leaves every page exactly as it was; only when the active one is the one
   * going does anything repaint, and then it is the first of what is left,
   * because something has to paint the page.
   */
  function deleteScheme() {
    if (state.schemes.length < 2) return;

    // The entry on the list, which is not `state.editing`: that is a copy,
    // and the Name field may have renamed it since. A scheme made with New is
    // its own entry, so it is the one case matched by identity rather than by
    // the name it was opened under.
    var doomed = null;
    state.schemes.forEach(function (scheme) {
      var mine = state.from === null ? scheme === state.editing
                                     : scheme.name === state.from;
      if (mine) doomed = scheme;
    });
    if (!doomed) return;

    var left = state.schemes.filter(function (scheme) {
      return scheme !== doomed;
    });
    var surviving = {};
    left.forEach(function (scheme) { surviving[scheme.name] = true; });
    var takesOver = surviving[state.wasActive] ? state.wasActive : left[0].name;

    var sure = window.confirm(
      'Delete the scheme "' + doomed.name + '"?\n\n' +
        (takesOver === state.wasActive
          ? "Nothing on the pages changes; only the scheme goes. "
          : 'Every page is repainted in "' + takesOver + '". ') +
        "Highlights name a slot rather than a hue, so no comment and no " +
        "choice of slot is lost.\n\nThis cannot be undone."
    );
    if (!sure) return;

    // Made with New and never applied: the knowledge base never heard of it,
    // so there is nothing to ask the server to forget. Dropping it here spares
    // a rebuild of every page, and the reload after one, for no change at all.
    if (state.from === null) {
      state.schemes = left;
      edit(takesOver);
      return;
    }

    // No `remap`. A reorder is the one edit here that renumbers sidecars, and
    // the only slots dragged were the ones about to stop existing -- renumbering
    // the reader's annotations on the way out would be a rewrite nobody asked
    // for, in aid of a scheme that is being thrown away.
    post("/api/schemes", { active: takesOver, schemes: left })
      .then(function (data) {
        clearPreview();
        (data.warnings || []).forEach(function (warning) {
          ui.toast(warning, "error");
        });
        // Every page was rewritten, including this one.
        window.location.reload();
      })
      .catch(function (error) {
        ui.toast(error.message, "error");
      });
  }

  function apply_() {
    var name = (state.editing.name || "").trim();
    if (!name) {
      ui.toast("A scheme needs a name", "error");
      return;
    }
    state.editing.name = name;

    var schemes = state.schemes.map(function (scheme) {
      return scheme.name === state.from || scheme === state.editing
        ? state.editing
        : scheme;
    });
    if (schemes.indexOf(state.editing) === -1) schemes.push(state.editing);

    var payload = { active: name, schemes: schemes };

    // Whenever the colors moved, whichever scheme they moved in.
    //
    // This used to be conditional on editing the scheme already in use, on the
    // theory that renumbering for a scheme nobody is looking at would repaint
    // the page. It is the opposite: Apply *activates* whatever is being
    // edited, so the edited scheme is always the one about to be looked at,
    // and skipping the renumbering is what makes the drag visible. Pressing
    // New and rearranging the copy repainted five highlights out of six.
    //
    // The rule that holds in every case is simpler than the one it replaces:
    // a reorder contributes nothing visible, on top of whatever else the
    // apply does. Same scheme, and nothing changes at all. A different one,
    // and you get exactly the switch you would have got without the drag.
    if (state.order.join(",") !== "1,2,3,4,5,6") payload.remap = state.order;

    post("/api/schemes", payload)
      .then(function (data) {
        clearPreview();
        (data.warnings || []).forEach(function (warning) {
          ui.toast(warning, "error");
        });
        // Every page was rewritten, including this one.
        window.location.reload();
      })
      .catch(function (error) {
        ui.toast(error.message, "error");
      });
  }

  function close() {
    clearPreview();
    if (win) win.parentNode.removeChild(win);
    win = null;
    var button = document.getElementById("sidebar-settings");
    if (button) {
      button.setAttribute("aria-expanded", "false");
      button.focus();
    }
  }

  function open() {
    if (win) {
      close();
      return;
    }

    // Both at once. The vocabulary cannot fail the window -- `getSemantics`
    // resolves to an empty one rather than rejecting -- so the only thing that
    // can keep the window shut is still the schemes, which is right: a scheme
    // is what the page is painted with and there always has to be one.
    Promise.all([get("/api/schemes"), getSemantics()])
      .then(function (answers) {
        var data = answers[0];
        state = {
          schemes: data.schemes,
          wasActive: data.active,
          editing: null,
          from: null,
          order: [1, 2, 3, 4, 5, 6],
          previewing: false,
          kinds: adoptSemantics(answers[1]),
        };

        win = el("div", "settings");
        win.setAttribute("role", "dialog");
        win.setAttribute("aria-label", "Settings");
        win.style.left = "auto";
        win.style.right = "1.5rem";
        win.style.top = "4rem";

        var bar = el("div", "settings__bar");
        // The bar names the window now that there is more than one thing in
        // it; each section names itself below.
        bar.appendChild(el("span", "settings__heading", "Settings"));
        var shut = el("button", "settings__close", "×");
        shut.type = "button";
        shut.setAttribute("aria-label", "Close settings");
        shut.addEventListener("click", close);
        bar.appendChild(shut);

        win.appendChild(bar);

        var colors = section("Color schemes");
        colors.appendChild(el("div", "settings__body"));
        colors.appendChild(el("div", "settings__footer"));
        win.appendChild(colors);

        var kinds = section("Comment card semantic types");
        kinds.appendChild(el("div", "semantics"));
        win.appendChild(kinds);

        document.body.appendChild(win);
        draggable(win, bar);

        var button = document.getElementById("sidebar-settings");
        if (button) button.setAttribute("aria-expanded", "true");

        edit(data.active);
        renderSemantics();
      })
      .catch(function (error) {
        ui.toast(error.message, "error");
      });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && win) close();
  });

  /* Delegated, rather than a listener on the button. The panel is re-rendered
   * in place after any tree change, which throws the button away along with
   * anything attached to it -- and a settings window that stops opening once
   * you have made a folder is a worse bug than the one it would replace. */
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("#sidebar-settings");
    if (button) open();
  });

  window.mdweaveSettings = {
    open: open,
    close: close,
    previewCss: previewCss,
    derive: derive,
  };
})();
