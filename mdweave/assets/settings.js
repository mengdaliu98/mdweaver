/* Settings: a floating window, and the colour schemes inside it.
 *
 * Three ideas hold this together.
 *
 * A colour is a **slot**, not a hue. An annotation says "the third colour",
 * so switching schemes moves every highlight to the third colour of the new
 * one and nothing has to be rewritten. Dragging the swatches is the opposite
 * case: the colours move, so the annotations have to move with them to stay
 * the colour they were -- that is the `remap` the server is sent, and it is
 * the one edit here that touches a sidecar.
 *
 * **Preview is a lie told carefully.** It writes the same custom properties
 * the generated stylesheet writes, into one <style> element, so the page you
 * are looking at is the page you would get. Nothing reaches the disk until
 * Apply, and closing the window takes the lie back.
 *
 * **The window is not a modal.** It is moved by its title bar and leaves the
 * document underneath live, because the whole point is to watch the prose
 * change colour while you pick.
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
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.error || "the server refused it");
        return data;
      });
    });
  }

  /* --- the colours, worked out the way the server works them out ----------
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
    row.setAttribute("aria-label", "The six colours, in order. Drag to rearrange.");

    state.editing.colors.forEach(function (fill, index) {
      var cell = el("div", "settings__slot");
      cell.draggable = true;
      cell.dataset.index = String(index);
      cell.setAttribute("role", "listitem");

      var picker = document.createElement("input");
      picker.type = "color";
      picker.value = fill;
      picker.className = "settings__picker";
      picker.setAttribute("aria-label", "Colour " + (index + 1));
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

  /* Dragging a swatch moves the colour and takes its annotations with it, so
   * the arrangement has to remember where each colour *started*: `order` is
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
    wrap.appendChild(
      el(
        "p",
        "settings__hint",
        "Six, always. Drag to rearrange — existing highlights keep the colour " +
          "they have and follow it to its new place."
      )
    );

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
    // Identity of each colour before any dragging, so a reorder can be
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

    // Whenever the colours moved, whichever scheme they moved in.
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

    get("/api/schemes")
      .then(function (data) {
        state = {
          schemes: data.schemes,
          wasActive: data.active,
          editing: null,
          from: null,
          order: [1, 2, 3, 4, 5, 6],
          previewing: false,
        };

        win = el("div", "settings");
        win.setAttribute("role", "dialog");
        win.setAttribute("aria-label", "Settings");
        win.style.left = "auto";
        win.style.right = "1.5rem";
        win.style.top = "4rem";

        var bar = el("div", "settings__bar");
        bar.appendChild(el("span", "settings__heading", "Colour schemes"));
        var shut = el("button", "settings__close", "×");
        shut.type = "button";
        shut.setAttribute("aria-label", "Close settings");
        shut.addEventListener("click", close);
        bar.appendChild(shut);

        win.appendChild(bar);
        win.appendChild(el("div", "settings__body"));
        win.appendChild(el("div", "settings__footer"));
        document.body.appendChild(win);
        draggable(win, bar);

        var button = document.getElementById("sidebar-settings");
        if (button) button.setAttribute("aria-expanded", "true");

        edit(data.active);
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
