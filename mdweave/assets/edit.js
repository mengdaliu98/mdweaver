/* mdweave -- editing the prose in place.
 *
 * The page stays rendered. Click a paragraph and only that paragraph turns
 * into a box of its own markdown; click away and it turns back. Nothing else
 * on the page flickers, and at no point are you looking at a screen of raw
 * markdown.
 *
 * Every block carries the source lines it came from (data-src-start/end,
 * written by SourceMappedRenderer), which is what lets the server put an edit
 * back exactly where it came from without re-parsing anything.
 *
 * Selecting across blocks and pressing Delete works too, and likewise never
 * shows markup: the browser sends offsets into the *visible* text and the
 * server maps them back onto the source.
 *
 * Plain ES5, no build step, same as its neighbours.
 */
(function () {
  "use strict";

  var ui = window.mdweaveUI;
  var doc = document.getElementById("doc");
  if (!ui || !doc) return;

  var DOC_ID = document.body.dataset.document || "";
  var EDITABLE = "p, h1, h2, h3, h4, h5, h6, blockquote, ul, ol, pre, table";

  var open = null; // { block, editor, original, start, end }
  var busy = false;
  var ready = false; // editing writes to disk, so it needs the server

  /* --- talking to the server ---------------------------------------------- */

  function post(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (response) {
      if (!response.ok) return ui.reject(response);
      return response.json();
    });
  }

  /* A save that changed nothing leaves the page alone; the swap itself is
   * ui.adopt, shared with the refresh button. */
  function adopt(payload) {
    if (!payload || !payload.changed) return;
    ui.adopt(payload);
  }

  function failed(error) {
    ui.toast("Could not save: " + error.message, "error");
  }

  /* --- one block at a time ------------------------------------------------ */

  function spanOf(block) {
    var start = parseInt(block.getAttribute("data-src-start"), 10);
    var end = parseInt(block.getAttribute("data-src-end"), 10);
    return isNaN(start) || isNaN(end) ? null : { start: start, end: end };
  }

  /* The block a node sits in, or null if it is not editable prose. */
  function blockFor(node) {
    var el = node && node.nodeType === 3 ? node.parentNode : node;
    if (!el || !el.closest) return null;
    var block = el.closest("[data-src-start]");
    return block && doc.contains(block) ? block : null;
  }

  function edit(block) {
    if (!ready || open || busy) return;
    var span = spanOf(block);
    if (!span) return;

    var url =
      "/api/block?document=" + encodeURIComponent(DOC_ID) +
      "&start=" + span.start + "&end=" + span.end;

    fetch(url)
      .then(function (response) {
        if (!response.ok) return ui.reject(response);
        return response.json();
      })
      .then(function (payload) {
        show(block, span, payload.markdown);
      })
      .catch(function (error) {
        ui.toast("Could not open that for editing: " + error.message, "error");
      });
  }

  function show(block, span, markdown) {
    if (open) return;

    var editor = document.createElement("textarea");
    editor.className = "block-editor";
    editor.value = markdown;
    editor.spellcheck = false;
    editor.setAttribute("aria-label", "Markdown source for this block");

    // Match the space the block occupied, so nothing below it jumps.
    editor.style.minHeight = block.getBoundingClientRect().height + "px";

    block.classList.add("block--editing");
    block.style.display = "none";
    block.parentNode.insertBefore(editor, block.nextSibling);

    open = {
      block: block,
      editor: editor,
      original: markdown,
      start: span.start,
      end: span.end,
    };

    grow(editor);
    editor.focus();
    editor.setSelectionRange(editor.value.length, editor.value.length);

    editor.addEventListener("input", function () {
      grow(editor);
    });
    editor.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        cancel();
      } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        commit();
      }
    });
    // Clicking anywhere outside is the save gesture, per the blur.
    editor.addEventListener("blur", commit);
  }

  function grow(editor) {
    editor.style.height = "auto";
    editor.style.height = editor.scrollHeight + "px";
  }

  function close() {
    if (!open) return null;
    var state = open;
    open = null;
    state.editor.removeEventListener("blur", commit);
    state.editor.remove();
    state.block.style.display = "";
    state.block.classList.remove("block--editing");
    return state;
  }

  function cancel() {
    var state = close();
    if (state) state.block.focus && state.block.focus();
  }

  function commit() {
    var state = close();
    if (!state) return;

    var text = state.editor.value;
    if (text === state.original) return; // opened and closed; nothing to do

    busy = true;
    doc.classList.add("doc--saving");
    post("/api/block", {
      document: DOC_ID,
      start: state.start,
      end: state.end,
      text: text,
    })
      .then(adopt)
      .catch(failed)
      .then(function () {
        busy = false;
        doc.classList.remove("doc--saving");
      });
  }

  /* --- cutting a selection ------------------------------------------------ */

  /* How far into a block's visible text a boundary sits. Mirrors what the
   * server computes with BeautifulSoup's get_text(), so the two agree on what
   * "character 40 of this paragraph" means. */
  function offsetIn(block, node, offset) {
    var range = document.createRange();
    range.selectNodeContents(block);
    range.setEnd(node, offset);
    return range.toString().length;
  }

  /* Break a selection into one cut per block it touches. */
  function cutsFor(range) {
    var startBlock = blockFor(range.startContainer);
    var endBlock = blockFor(range.endContainer);
    if (!startBlock || !endBlock) return null;

    var blocks = Array.prototype.filter.call(
      doc.querySelectorAll("[data-src-start]"),
      function (block) {
        return range.intersectsNode(block);
      }
    );
    if (!blocks.length) blocks = [startBlock];

    var cuts = [];
    for (var i = 0; i < blocks.length; i++) {
      var block = blocks[i];
      var span = spanOf(block);
      if (!span) continue;

      var length = block.textContent.length;
      var from = block === startBlock
        ? offsetIn(block, range.startContainer, range.startOffset)
        : 0;
      var to = block === endBlock
        ? offsetIn(block, range.endContainer, range.endOffset)
        : length;

      if (to > from) {
        cuts.push({ start: span.start, end: span.end, from: from, to: to });
      }
    }
    return cuts.length ? cuts : null;
  }

  function cutSelection() {
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return false;

    var range = selection.getRangeAt(0);
    if (!doc.contains(range.commonAncestorContainer)) return false;

    var cuts = cutsFor(range);
    if (!cuts) return false;

    busy = true;
    doc.classList.add("doc--saving");
    selection.removeAllRanges();

    post("/api/cut", { document: DOC_ID, cuts: cuts })
      .then(adopt)
      .catch(failed)
      .then(function () {
        busy = false;
        doc.classList.remove("doc--saving");
      });
    return true;
  }

  /* --- gestures ----------------------------------------------------------- */

  doc.addEventListener("click", function (event) {
    if (!ready || open || busy) return;

    // A click that ends a drag is a selection, not a request to edit.
    var selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;

    // Highlights belong to annotate.js: clicking one opens its note.
    if (event.target.closest && event.target.closest("mark.hl")) return;
    // Links should navigate.
    if (event.target.closest && event.target.closest("a")) return;

    var block = blockFor(event.target);
    if (block) edit(block);
  });

  document.addEventListener("keydown", function (event) {
    if (!ready || open || busy) return;
    if (event.key !== "Backspace" && event.key !== "Delete") return;
    if (event.target && /^(INPUT|TEXTAREA)$/.test(event.target.tagName)) return;

    if (cutSelection()) event.preventDefault();
  });

  document.addEventListener("cut", function (event) {
    if (!ready || open || busy) return;
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed) return;
    if (!doc.contains(selection.getRangeAt(0).commonAncestorContainer)) return;

    // Let the clipboard have the text, then take it out of the document.
    if (event.clipboardData) {
      event.clipboardData.setData("text/plain", selection.toString());
      event.preventDefault();
    }
    cutSelection();
  });

  /* Refreshing replaces the prose wholesale, which would throw away an edit in
   * progress -- so it waits for this to go quiet first. */
  window.mdweaveEdit = {
    isBusy: function () {
      return open !== null || busy;
    },
  };

  /* Editing writes to disk, so it only exists when a server is listening.
   * Over file:// the page stays exactly as readable, just not editable. */
  ui.api().then(function (health) {
    if (!health) return;
    ready = true;
    doc.classList.add("doc--editable");
  });
})();
