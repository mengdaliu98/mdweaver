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
  // Slots, not hues: "the third color", whatever the active scheme paints
  // it. Keep in step with scheme.SLOT_CLASSES; a drift offers a swatch the
  // server will refuse.
  var COLORS = ["c1", "c2", "c3", "c4", "c5", "c6"];
  var DEFAULT_COLOR = "c5";
  var DOC_ID = document.body.dataset.document || "";
  var API = "/api/annotations";
  var SEMANTICS_API = "/api/semantics";
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

  /* Two rows, the palette on each. Picking a color *is* the action, so a
   * highlight is one click rather than "highlight, then recolor" -- and the
   * color you are choosing is shown in the color it will be. */
  var COMMENT_ICON =
    '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
    '<path fill="currentColor" d="M8 1.5c-3.6 0-6.5 2.4-6.5 5.4 0 1.7.9 3.2 2.4 4.2l-.6 2.6a.4.4 0 0 0 .6.4l2.9-1.6c.4.05.8.08 1.2.08 3.6 0 6.5-2.4 6.5-5.4S11.6 1.5 8 1.5Z"/></svg>';
  var HIGHLIGHT_ICON =
    '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
    '<path fill="currentColor" d="M10.6 1.9 14.1 5.4 7.4 12.1H3.9V8.6zM2.4 13.4h11.2v1.4H2.4z"/></svg>';

  /* The eraser. Highlights used to be removed by pressing the color they
   * already were, which meant a color press had to first work out what was
   * underneath -- and a press that looked like "make this blue" sometimes
   * meant "make this nothing". A press now always paints, whatever is there,
   * and taking a highlight off has its own button. */
  var FORMAT_ICON =
    '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
    '<path fill="currentColor" d="M6.1 2.3h5.6v1.4H9.4l-2 8.6h2v1.4H3.8v-1.4h2.1l2-8.6H6.1z"/></svg>';

  var ERASER_ICON =
    '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">' +
    '<path fill="currentColor" d="M9.1 2.2 14 7.1a1.1 1.1 0 0 1 0 1.6l-4.3 4.3H13v1.3H6.6l-4.3-4.3a1.1 1.1 0 0 1 0-1.6l5.2-5.2a1.1 1.1 0 0 1 1.6 0ZM4.6 8.7l3.1 3.1 1.2-1.2-3.1-3.1z"/></svg>';

  function toolbarRow(kind, icon, label) {
    var dots = COLORS.map(function (color) {
      return (
        '<button type="button" class="selection-toolbar__swatch swatch swatch--' + color +
        '" data-kind="' + kind + '" data-color="' + color + '"' +
        ' title="' + label + " in " + paletteName(color).toLowerCase() + '"' +
        ' aria-label="' + label + " in " + paletteName(color) + '"></button>'
      );
    }).join("");
    // Only on the highlight row: a comment's color is chosen when it is
    // written, and there is nothing to erase before it exists.
    var eraser =
      kind === "highlight"
        ? '<button type="button" class="selection-toolbar__swatch selection-toolbar__eraser"' +
          ' data-kind="erase" title="Remove highlighting"' +
          ' aria-label="Remove highlighting">' + ERASER_ICON + "</button>"
        : "";
    return (
      '<div class="selection-toolbar__row" role="group" aria-label="' + label + '">' +
      '<span class="selection-toolbar__what">' + icon + " " + label + "</span>" +
      '<span class="selection-toolbar__swatches">' + dots + eraser + "</span></div>"
    );
  }

  /* --- the format row ------------------------------------------------------
   *
   * Above the annotations, and separated from them, because the two do
   * different things to different files. Bold and italic put `**` and `*` in
   * the markdown: the document itself changes, and travels that way to
   * anything else that reads it. A comment or a highlight never touches the
   * prose -- it lives in the sidecar and only ever reaches the HTML. Putting
   * them in one undifferentiated list would hide that.
   *
   * Plain is here rather than as a second press of Bold for the same reason
   * the eraser is a button: a press that sometimes adds and sometimes removes
   * depends on state the reader cannot see, and the alternative to a Plain
   * button is opening the raw markdown to delete a pair of asterisks. */
  var FORMATS = [
    { style: "bold", label: "Bold", glyph: "B", weight: "700" },
    { style: "italic", label: "Italic", glyph: "I", style_: "italic" },
    { style: "plain", label: "Plain", glyph: "P" },
  ];

  function formatRow() {
    var buttons = FORMATS.map(function (item) {
      var css = item.weight
        ? ' style="font-weight:' + item.weight + '"'
        : item.style_
        ? ' style="font-style:' + item.style_ + '"'
        : "";
      return (
        '<button type="button" class="selection-toolbar__format"' +
        ' data-format="' + item.style + '" title="' + item.label + '"' +
        ' aria-label="' + item.label + '"' + css + ">" + item.glyph + "</button>"
      );
    }).join("");
    return (
      '<div class="selection-toolbar__row" role="group" aria-label="Format">' +
      '<span class="selection-toolbar__what">' + FORMAT_ICON + " Format</span>" +
      '<span class="selection-toolbar__swatches">' + buttons + "</span></div>"
    );
  }

  var toolbar = document.createElement("div");
  toolbar.className = "selection-toolbar";
  toolbar.hidden = true;
  toolbar.innerHTML =
    formatRow() +
    '<div class="selection-toolbar__split" aria-hidden="true"></div>' +
    toolbarRow("comment", COMMENT_ICON, "Comment") +
    toolbarRow("highlight", HIGHLIGHT_ICON, "Highlight");
  document.body.appendChild(toolbar);

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

  /* --- color ------------------------------------------------------------ */

  /* "custom" is the class a raw CSS color renders as. It has to come off with
   * the rest when a token is picked: its rule sits further down the stylesheet
   * at the same specificity, so leaving it on would quietly beat the choice. */
  var PALETTE = COLORS.concat(["custom"]);

  /* A slot has no name to speak, so the label is its position. Screen reader
   * users get "Color 3" rather than a hue that the next scheme contradicts. */
  function paletteName(color) {
    return "Color " + color.slice(1);
  }

  function wearColor(element, prefix, color) {
    PALETTE.forEach(function (other) {
      element.classList.remove(prefix + other);
    });
    element.classList.add(prefix + color);
  }

  /** Which of the six an element is currently wearing, or null for none. */
  function colorOf(element, prefix) {
    for (var i = 0; i < COLORS.length; i++) {
      if (element.classList.contains(prefix + COLORS[i])) return COLORS[i];
    }
    return null;
  }

  /* Recolor a highlight and its card in place. All four values -- fill,
   * underline, pin and card -- hang off the one class, so this is the whole
   * visual change; the inline property a custom color reads goes with it. */
  function recolor(annId, card, color) {
    mdw.marksFor(annId).forEach(function (mark) {
      wearColor(mark, "hl--", color);
      mark.style.removeProperty("--hl-custom");
    });
    if (card) {
      wearColor(card, "note--", color);
      card.style.removeProperty("--note-custom");
    }
  }

  var ARROWS = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 };

  /* Swatches as a radio group, not a row of toggles: the colors are mutually
   * exclusive. That buys the arrow keys and one tab stop for the whole group
   * instead of one each, which matters in a note card that already has two
   * buttons after it. */
  function colorPicker(current, onPick) {
    var row = document.createElement("div");
    row.className = "swatches";
    row.setAttribute("role", "radiogroup");
    row.setAttribute("aria-label", "Highlight color");

    var swatches = COLORS.map(function (color) {
      var swatch = document.createElement("button");
      swatch.type = "button";
      swatch.className = "swatch swatch--" + color;
      swatch.dataset.color = color;
      swatch.setAttribute("role", "radio");
      swatch.setAttribute("aria-label", paletteName(color));
      swatch.title = paletteName(color);
      row.appendChild(swatch);
      return swatch;
    });

    function select(color, focus) {
      swatches.forEach(function (swatch) {
        var on = swatch.dataset.color === color;
        swatch.setAttribute("aria-checked", on ? "true" : "false");
        // Roving tabindex: only the current color is a tab stop.
        swatch.tabIndex = on ? 0 : -1;
        if (on && focus) swatch.focus();
      });
    }

    select(current, false);
    // An annotation carrying a raw CSS color matches no swatch, which would
    // leave the group with no tab stop at all and no way to reach it.
    if (COLORS.indexOf(current) === -1) swatches[0].tabIndex = 0;

    row.addEventListener("click", function (event) {
      var swatch = event.target.closest && event.target.closest(".swatch");
      if (!swatch) return;
      event.stopPropagation(); // inside a card, a stray click closes the note
      select(swatch.dataset.color, false);
      onPick(swatch.dataset.color);
    });

    row.addEventListener("keydown", function (event) {
      var step = ARROWS[event.key];
      var at = swatches.indexOf(document.activeElement);
      if (!step || at === -1) return;
      event.preventDefault();

      var next = COLORS[(at + step + COLORS.length) % COLORS.length];
      select(next, true);
      onPick(next);
    });

    return row;
  }

  /* --- semantic types ------------------------------------------------------
   *
   * A comment says what is wrong with a passage. Its *semantic type* says what
   * kind of remark it is -- "question", "needs a citation", "too long" -- and
   * every type carries the instruction Claude is given for putting that kind of
   * thing right. The pair is what makes a comment worth handing to a model: "I
   * don't follow this" plus "answer in a footnote rather than by rewriting the
   * sentence" is an instruction, where either half on its own is not.
   *
   * The vocabulary is the reader's own and lives beside the prose, so it is
   * fetched rather than written down here; mdweave/semantics.py is the file it
   * comes from and the settings window is where most of it is edited. Nothing
   * about a comment requires a type, which is why every failure in here is
   * silent: no types, an unreadable dotfile and a server too old to know the
   * route all have to leave a perfectly usable composer behind them.
   */

  var semantics = { types: [], defaults: {} };
  var vocabulary = null;

  /* Asked for once and shared, the way ui.api() shares the health probe. Every
   * card wants the vocabulary -- to put the right instruction behind its tag --
   * and so does every composer, and a refresh brings the whole lot back at
   * once; one lookup per page, not one per card. Pass `true` to ask again,
   * which is what the settings window does after it saves. */
  function loadSemantics(again) {
    if (vocabulary && !again) return vocabulary;
    vocabulary = fetch(SEMANTICS_API, { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data) semantics = { types: data.types || [], defaults: data.defaults || {} };
      })
      .catch(function () {
        // Left as it was: an empty vocabulary, which the composer can show.
      });
    return vocabulary;
  }

  /** The type recorded under `name`, or null if there is no such type.
   *
   * A name is the identity of a type here -- it keys the per-color defaults and
   * it is what a comment stores -- and the Python matches names without regard
   * to case, so this has to as well, or a sidecar saying "Question" would show
   * no tag against a vocabulary that spells it "question". */
  function semanticNamed(name) {
    if (!name) return null;
    var wanted = String(name).toLowerCase();
    for (var i = 0; i < semantics.types.length; i++) {
      if (String(semantics.types[i].name).toLowerCase() === wanted) {
        return semantics.types[i];
      }
    }
    return null;
  }

  /* The type a color reaches for, if the reader gave it one.
   *
   * `defaults` is keyed by the slot *number* as a string, because a JSON object
   * key can be nothing else, so the class name "c3" has to lose its letter
   * before the lookup. An entry naming a type that has since been deleted is
   * treated as no entry at all. */
  function defaultSemanticFor(color) {
    var kind = semanticNamed(semantics.defaults[String(color).slice(1)]);
    return kind ? kind.name : null;
  }

  /* The one shape a type is ever shown in, in the composer and on a card
   * alike: a rounded box holding the type's name, with the Claude instruction
   * as its tooltip. Putting the instruction within reach of the pointer is
   * what keeps the tag from being a bare word -- the rule a remark is being
   * filed under can be read without opening the settings window. */
  function dressTag(tag, name) {
    var kind = semanticNamed(name);
    tag.className = "semantic-tag";
    tag.textContent = (kind && kind.name) || name;
    if (kind && kind.instruction) tag.title = kind.instruction;
    return tag;
  }

  function semanticLabel(name) {
    var tag = dressTag(document.createElement("span"), name);
    tag.className += " semantic-tag--static";
    return tag;
  }

  /* The vocabulary is a document, not a table of rows: /api/semantics is given
   * the complete set every time and writes it over the dotfile, exactly as
   * /api/schemes is. A type added from the composer is therefore the list as it
   * stands plus one, posted entire -- and the answer, not the request, is what
   * becomes the new vocabulary, since the server trims whitespace and clamps
   * a long name, so what was sent is not quite what was stored. */
  function saveSemantics(types, defaults) {
    return fetch(SEMANTICS_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        types: types,
        defaults: defaults || semantics.defaults,
      }),
    }).then(function (response) {
      if (!response.ok) return reject(response);
      return response.json().then(function (data) {
        semantics = { types: data.types || [], defaults: data.defaults || {} };
        return semantics;
      });
    });
  }

  /* The row of types in the composer.
   *
   * Every type the reader has, as a tag, at most one of them lit. Pressing a
   * lit tag puts it out, because a comment need not be of any kind and the only
   * way back from a mistaken tap would otherwise be to cancel and start the
   * comment again. `+` reveals the two fields that make a new one: a name, and
   * the instruction -- optional, since an unexplained type still groups remarks
   * usefully and still tells Claude that these three are the same sort of
   * thing. */
  function semanticPicker(initial, onPick) {
    var chosen = initial || null;

    var row = document.createElement("div");
    row.className = "composer__semantics";

    var tags = document.createElement("div");
    tags.className = "semantic-tags";
    tags.setAttribute("role", "group");
    tags.setAttribute("aria-label", "Kind of remark");
    // Named in the open rather than left to the tooltip on `+`: with no types
    // yet defined this row is a single plus sign, and a lone plus sign beside a
    // comment box says nothing at all about what it would add.
    tags.appendChild(el("span", "composer__semantics-label", "Kind"));
    row.appendChild(tags);

    var form = document.createElement("div");
    form.className = "semantic-new";
    form.hidden = true;
    row.appendChild(form);

    var name = field("Name, e.g. question", "Name of the new kind");
    var instruction = field(
      "What Claude should do (optional)",
      "Claude instruction for the new kind"
    );
    form.appendChild(name);
    form.appendChild(instruction);

    var plus = document.createElement("button");
    plus.type = "button";
    plus.className = "semantic-tag semantic-tag--add";
    plus.textContent = "+";
    plus.title = "Add a kind of remark";
    plus.setAttribute("aria-label", "Add a kind of remark");
    plus.setAttribute("aria-expanded", "false");

    /* One text input. A textarea would be the obvious home for an instruction
     * of a couple of sentences, but `.composer textarea` is how the comment box
     * itself is reached, and a composer with two of them is a composer whose
     * text box is ambiguous. The instruction is editable at full height in the
     * settings window, which is where a long one belongs anyway. */
    function field(placeholder, label) {
      var input = document.createElement("input");
      input.type = "text";
      input.className = "semantic-new__field";
      input.placeholder = placeholder;
      input.setAttribute("aria-label", label);
      input.addEventListener("keydown", function (event) {
        // Swallowed, both of them: Enter here means "add this type", not
        // "save the comment", and Escape means "never mind the type", not
        // "throw away everything I have written".
        if (event.key === "Enter") {
          event.preventDefault();
          event.stopPropagation();
          create();
        } else if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          reveal(false);
        }
      });
      return input;
    }

    function reveal(open) {
      form.hidden = !open;
      plus.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) name.focus();
      // The panel just grew or shrank, and it is positioned against the
      // highlight rather than by the flow, so nothing would move it otherwise.
      mdw.layout();
    }

    function create() {
      var wanted = name.value.trim();
      if (!wanted) {
        name.focus();
        return;
      }
      var next = semantics.types.slice();
      next.push({ name: wanted, instruction: instruction.value.trim() });

      plus.disabled = true;
      saveSemantics(next)
        .then(function () {
          name.value = "";
          instruction.value = "";
          // Adding a kind in the middle of writing a comment is how you say
          // this comment is one of those, so it is attached straight away.
          chosen = wanted;
          onPick(chosen);
          reveal(false);
          paint();
        })
        .catch(function (error) {
          // A duplicate or empty name comes back as a 400 with a sentence in
          // it. The fields keep what was typed, so the fix is one edit away.
          toast(error.message, "error");
        })
        .then(function () {
          plus.disabled = false;
        });
    }

    plus.addEventListener("click", function (event) {
      event.stopPropagation(); // inside a card, a stray click closes the note
      reveal(form.hidden);
    });

    function paint() {
      while (tags.childNodes.length > 1) tags.removeChild(tags.lastChild);

      semantics.types.forEach(function (kind) {
        var tag = dressTag(document.createElement("button"), kind.name);
        tag.type = "button";
        var on =
          chosen !== null && String(kind.name).toLowerCase() === String(chosen).toLowerCase();
        tag.setAttribute("aria-pressed", on ? "true" : "false");
        tag.addEventListener("click", function (event) {
          event.stopPropagation();
          chosen = on ? null : kind.name;
          onPick(chosen);
          paint();
        });
        tags.appendChild(tag);
      });

      tags.appendChild(plus);
      mdw.layout();
    }

    paint();
    return row;
  }

  /* --- composer ---------------------------------------------------------- */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

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

  function openComposer(selector, chosen) {
    // Deliberately NOT closeComposer(): the pending highlight has already been
    // put in place by the caller and is what the composer anchors to. Unwrapping
    // it here left the panel with no anchor, so it fell back to the top-left
    // corner of the page.
    removeComposer();

    composer = document.createElement("div");
    // The composer stands in for the card the annotation is about to get, so
    // it wears the same color class and reads the same four variables.
    // The color was already chosen by the swatch that opened this.
    composer.className = "composer note--" + (chosen || DEFAULT_COLOR);
    composer.dataset.ann = PENDING;
    composer.innerHTML =
      '<blockquote class="composer__quote"></blockquote>' +
      '<textarea class="composer__input" rows="4" placeholder="Add a comment…"></textarea>' +
      '<div class="composer__actions">' +
      '<button type="button" class="composer__button composer__button--ghost" data-act="cancel">Cancel</button>' +
      '<button type="button" class="composer__button" data-act="save">Comment</button>' +
      "</div>";

    // A comment about the document as a whole has no quote to show. The empty
    // blockquote that left behind is a bordered gap, which reads as a passage
    // that failed to load rather than as one that was never there.
    var quoted = composer.querySelector(".composer__quote");
    if (selector.quote) quoted.textContent = selector.quote;
    else quoted.parentNode.removeChild(quoted);

    var actions = composer.querySelector(".composer__actions");
    var color = chosen || DEFAULT_COLOR;

    /* The kind of remark, taken once from the color this composer opened in.
     *
     * A later color press deliberately leaves it alone. The per-color default
     * is a *starting* suggestion -- "the color I use for questions" -- and not
     * a property the color keeps imposing: once a type is on the card, whether
     * it arrived by default or by a press, it is the reader's. Re-deriving it
     * on every swatch would quietly overwrite a deliberate choice, and quietly
     * is the whole problem, since the eye is on the swatches at that moment and
     * the tag that changed is two rows above them. Worse, the overwrite has no
     * inverse: a color with no default would have to mean either "clear the
     * type" or "leave it", and both are wrong half the time.
     */
    var semanticType = defaultSemanticFor(color);

    actions.insertBefore(
      colorPicker(color, function (picked) {
        color = picked;
        // Nothing is saved yet: the pending highlight and the panel simply
        // show what the comment is about to look like.
        recolor(PENDING, composer, picked);
      }),
      actions.firstChild
    );

    // Between what the comment says and the buttons that commit it, because it
    // is part of writing the comment rather than part of filing it.
    composer.insertBefore(
      semanticPicker(semanticType, function (picked) {
        semanticType = picked;
      }),
      actions
    );

    /* Below the buttons, and ticked.
     *
     * A comment written in a knowledge base with a Claude button on it is
     * usually written *so that* something will be done about it, and the review
     * screen makes you look at the whole list before anything runs -- so the
     * cost of a wrong default here is one unticked box rather than a surprise
     * edit. It sits under the actions because it is about what happens to the
     * comment afterwards, which is a different question from what it says. */
    var send = el("label", "composer__send");
    var sendBox = document.createElement("input");
    sendBox.type = "checkbox";
    sendBox.className = "composer__send-box";
    sendBox.checked = true;
    send.appendChild(sendBox);
    send.appendChild(el("span", null, "Send to Claude"));
    composer.appendChild(send);

    layer.appendChild(composer);
    mdw.layout();

    var input = composer.querySelector(".composer__input");
    var save = composer.querySelector('[data-act="save"]');
    input.focus();

    composer.querySelector('[data-act="cancel"]').addEventListener("click", closeComposer);
    save.addEventListener("click", function () {
      submit(selector, input, save, color, semanticType, sendBox.checked);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        submit(selector, input, save, color, semanticType, sendBox.checked);
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeComposer();
      }
    });
  }

  function submit(selector, input, saveButton, color, semanticType, sendToClaude) {
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
        color: color,
        body: body,
        // Null rather than omitted: "this comment is of no particular kind" is
        // an answer the composer can give, and the field has to carry it.
        semantic_type: semanticType || null,
        send_to_claude: sendToClaude !== false,
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
          // `color_token` is the class, worked out by the server; `color` is
          // what the file holds, which may be a number or an old hue name.
          var token = annotation.color_token || DEFAULT_COLOR;
          wrapSpan(index, span[0], span[1], "hl hl--" + token + " hl--has-note", annotation.id);
          addNote(annotation);
        } else {
          // Saved, but this side could not work out where it goes. The
          // server can: it renders the page from the same sidecar that was
          // just written. Asking the reader to reload is telling them to do
          // by hand the one thing the page already knows how to do.
          if (window.mdweaveRefresh) window.mdweaveRefresh();
          else toast("Saved — reload to see it.", "warn");
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
    note.className = "note note--" + (annotation.color_token || DEFAULT_COLOR);
    note.id = "note-" + annotation.id;
    note.dataset.ann = annotation.id;
    note.dataset.status = annotation.status || "open";

    var pin = document.createElement("button");
    pin.type = "button";
    pin.className = "note__pin";
    pin.setAttribute("aria-expanded", "false");
    pin.title = annotation.target.quote;
    pin.textContent = (entry.author || "?").charAt(0).toUpperCase();

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
    body.appendChild(item);
    // Under the remark, not over it: the prose is what was written, the tag is
    // how it is to be read. The same order a server-rendered card gets below.
    if (annotation.semantic_type) {
      body.appendChild(semanticLabel(annotation.semantic_type));
    }
    if (writable) body.appendChild(noteActions(note));

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

  /* Persist a recolor. The note is already wearing the new color by the time
   * this runs, so a failure is reported rather than silently reverted -- same
   * bargain as a dragged position. */
  function persistColor(annId, color) {
    fetch(API + "/" + encodeURIComponent(annId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document: DOC_ID, color: color }),
    })
      .then(function (response) {
        if (!response.ok) return reject(response);
      })
      .catch(function (error) {
        toast("Color not saved: " + error.message, "error");
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

  function noteActions(note) {
    var annId = note.dataset.ann;
    var row = document.createElement("div");
    row.className = "note__footer";

    row.appendChild(
      colorPicker(colorOf(note, "note--"), function (color) {
        recolor(annId, note, color);
        persistColor(annId, color);
      })
    );

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

  /* Give every note rendered into the page its footer of actions. */
  function addNoteFooters() {
    layer.querySelectorAll(".note").forEach(function (note) {
      var body = note.querySelector(".note__body");
      if (body && !body.querySelector(".note__footer")) {
        body.appendChild(noteActions(note));
      }
    });
  }

  /* Put each server-rendered card's semantic type on it.
   *
   * The sidecar knows which kind every comment is, but the card arrives from
   * the template without it, so the kinds are asked for here and hung on the
   * cards already in the page. It is one request, not one per card:
   * /api/annotations hands over the whole document's list, which is the same
   * list the page was built from.
   *
   * The tag goes above the footer rather than at the end, so the reading order
   * of a card is always the same three things in the same three places -- what
   * was said, what kind of remark it is, and what can be done about it --
   * whether the card was rendered by the server or built by addNote.
   *
   * Any tag already there is redrawn rather than left alone. Renaming a kind
   * or rewriting its instruction changes what the tag should say without
   * changing a single annotation, so a tag that is merely present is not
   * necessarily a tag that is still right. */
  function addSemanticTags() {
    if (!DOC_ID) return Promise.resolve();
    return loadSemantics()
      .then(function () {
        return fetch(API + "?document=" + encodeURIComponent(DOC_ID), {
          headers: { Accept: "application/json" },
        });
      })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (!data) return;
        (data.annotations || []).forEach(function (annotation) {
          var note = mdw.noteFor(annotation.id);
          var body = note && note.querySelector(".note__body");
          if (!body) return;

          var worn = body.querySelector(".semantic-tag--static");
          if (worn) worn.parentNode.removeChild(worn);
          if (!annotation.semantic_type) return;

          body.insertBefore(
            semanticLabel(annotation.semantic_type),
            body.querySelector(".note__footer")
          );
        });
        mdw.layout();
      })
      .catch(function () {
        // A card without its tag is still a card. Nothing here is worth a
        // toast: the reader did not ask for this, the page did.
      });
  }

  /* Everything this file hangs on a card it did not itself build. The two are
   * one function because they are re-applied as one: saving an edit or
   * refreshing swaps the whole notes layer, and a card that comes back with
   * its footer but without its tag is half repaired. */
  function addNoteActions() {
    addNoteFooters();
    addSemanticTags();
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

  toolbar.addEventListener("mousedown", function (event) {
    // Keep the selection alive: the default mousedown would collapse it.
    event.preventDefault();
  });

  toolbar.addEventListener("click", function (event) {
    var format = event.target.closest
      ? event.target.closest(".selection-toolbar__format")
      : null;
    if (format) {
      // edit.js owns source edits; this file owns the menu. It reads the
      // selection itself, so nothing has to be handed over.
      hideToolbar();
      if (window.mdweaveFormat) window.mdweaveFormat(format.dataset.format);
      return;
    }

    var swatch = event.target.closest
      ? event.target.closest(".selection-toolbar__swatch")
      : null;
    if (!swatch) return;

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

    var color = swatch.dataset.color;

    // Clear any previous session first, so its stale pending highlight cannot
    // be mistaken for this one's anchor.
    closeComposer();

    if (swatch.dataset.kind === "erase") {
      selection.removeAllRanges();
      eraseHighlight(selector);
      return;
    }

    if (swatch.dataset.kind === "highlight") {
      selection.removeAllRanges();
      saveHighlight(selector, color);
      return;
    }

    // Provisional highlight, so the target stays visible while typing -- and so
    // the composer has something to position itself against. In the color that
    // was picked, since the pick already chose it.
    wrapSpan(index, span[0], span[1], "hl hl--" + color + " hl--pending", PENDING);
    selection.removeAllRanges();
    openComposer(selector, color);
  });

  /* Take the highlighting off a selection. A comment's highlight is left
   * alone: it is the handle for a thread, and no gesture in this toolbar
   * should delete a conversation. */
  function eraseHighlight(selector) {
    fetch(API + "/erase", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document: DOC_ID,
        quote: selector.quote,
        prefix: selector.prefix,
        suffix: selector.suffix,
        occurrence: selector.occurrence,
      }),
    })
      .then(function (response) {
        if (!response.ok) return reject(response);
        return response.json();
      })
      .then(function (data) {
        var gone = data.cleared || [];
        if (!gone.length) {
          toast("Nothing to remove in that selection.", "warn");
          return;
        }
        gone.forEach(unwrapAnnotation);
      })
      .catch(function (error) {
        toast(error.message, "error");
      });
  }

  /* A highlight has nothing to type, so there is no composer step: the click
   * that chose the color is the whole interaction. */
  function saveHighlight(selector, color) {
    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document: DOC_ID,
        kind: "highlight",
        quote: selector.quote,
        prefix: selector.prefix,
        suffix: selector.suffix,
        occurrence: selector.occurrence,
        color: color,
      }),
    })
      .then(function (response) {
        if (!response.ok) return reject(response);
        // The whole payload, not just `annotation`: which highlights were
        // cleared or replaced is the rest of the answer, and a clear has no
        // annotation at all.
        return response.json();
      })
      .then(function (result) {
        // Three outcomes, because a color pressed over a highlight means
        // something different depending on what was already there: the same
        // color over exactly that span clears it, a different one repaints
        // it, and anything else is simply new.
        (result.cleared || []).forEach(unwrapAnnotation);
        (result.replaced || []).forEach(unwrapAnnotation);

        var annotation = result.annotation;
        if (!annotation) return; // cleared: there is nothing to draw

        var index = buildIndex(doc);
        var span = locate(index, annotation.target);
        if (span) {
          wrapSpan(
            index, span[0], span[1],
            "hl hl--" + (annotation.color_token || DEFAULT_COLOR), annotation.id
          );
        } else {
          // Saved, but this side could not work out where it goes. The
          // server can: it renders the page from the same sidecar that was
          // just written. Asking the reader to reload is telling them to do
          // by hand the one thing the page already knows how to do.
          if (window.mdweaveRefresh) window.mdweaveRefresh();
          else toast("Saved — reload to see it.", "warn");
        }
      })
      .catch(function (error) {
        toast(error.message, "error");
      });
  }

  /* Take one annotation's marks back out of the page, leaving the text. */
  function unwrapAnnotation(id) {
    unwrap('mark.hl[data-ann="' + (window.CSS ? CSS.escape(id) : id) + '"]');
  }

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
      // Saving an edit or refreshing replaces the notes layer, which throws
      // away every footer and every tag along with the cards that carried them.
      mdw.setRefreshHandler(addNoteActions);
    });
  }

  /* The settings window is the other place the vocabulary is edited, and it
   * does not reload the page the way a scheme change has to -- nothing on disk
   * is re-rendered by renaming a kind of remark. So it says so here instead,
   * and the composer stops offering a list that went stale two gestures ago. */
  window.mdweaveSemantics = {
    reload: function () {
      return loadSemantics(true).then(addSemanticTags);
    },
  };
})();
