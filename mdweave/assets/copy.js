/* mdweave -- copying prose gives you the markdown behind it.
 *
 * Selecting a paragraph and pressing Cmd-C used to put the *rendered* text on
 * the clipboard, so the paste lost every link, every bold word, every bullet.
 * The selection is decomposed exactly as a cut is -- per block, as offsets
 * into the visible text, `ui.blockRanges` -- and the server hands back the
 * markdown underneath it.
 *
 * The awkward part is that the clipboard cannot be written from a promise:
 * `event.clipboardData` is only writable while the copy event is being
 * dispatched, and the markdown is a fetch away. So the event writes the
 * rendered text synchronously -- exactly what the browser would have done --
 * and the markdown replaces it a moment later through navigator.clipboard,
 * which needs HTTPS or localhost and has both here.
 *
 * Everything that can go wrong therefore degrades to a plain copy rather than
 * to no copy at all: no clipboard API, no server, a selection that is not
 * prose, a request that fails. Losing the formatting is a small
 * disappointment; a Cmd-C that silently does nothing is a lost paragraph.
 *
 * Plain ES5, no build step, same as its neighbours.
 */
(function () {
  "use strict";

  var ui = window.mdweaveUI;
  var doc = document.getElementById("doc");
  if (!ui || !doc) return;

  var DOC_ID = document.body.dataset.document || "";
  var ready = false; // the markdown lives on the server, so it has to be there

  /* The selection, if it is prose this page has source for.
   *
   * Outside the article -- a note card, the sidebar, the checkpoint dialog --
   * there is no source to map onto, and inside a block editor the reader is
   * already looking at markdown, which the browser copies perfectly well by
   * itself. Both cases want the default copy, untouched. */
  function proseRange(selection) {
    if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
    var range = selection.getRangeAt(0);
    if (!doc.contains(range.commonAncestorContainer)) return null;
    return range;
  }

  document.addEventListener("copy", function (event) {
    if (!ready || !navigator.clipboard || !event.clipboardData) return;
    if (event.target && /^(INPUT|TEXTAREA)$/.test(event.target.tagName)) return;
    // An open editor, or a save in flight: the source is in the middle of
    // changing, so the offsets in this selection may already be stale.
    if (window.mdweaveEdit && window.mdweaveEdit.isBusy()) return;

    var selection = window.getSelection();
    var range = proseRange(selection);
    if (!range) return;

    var spans = ui.blockRanges(range);
    if (!spans) return;

    // Take the copy over, but put the rendered text on the clipboard first: it
    // is what the default would have written, and it is what stays there if
    // the markdown never arrives.
    event.clipboardData.setData("text/plain", selection.toString());
    event.preventDefault();

    fetch("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document: DOC_ID, spans: spans }),
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (payload) {
        if (!payload || !payload.markdown) return null;
        return navigator.clipboard.writeText(payload.markdown);
      })
      .catch(function () {
        /* Deliberately silent. The plain text is already on the clipboard, and
         * a toast in front of someone who has just pressed Cmd-C reports a
         * problem they do not have. */
      });
  });

  ui.api().then(function (health) {
    if (health) ready = true;
  });
})();
