/* mdweave -- shared browser helpers.
 *
 * Loaded before every other script. Holds the two things more than one module
 * needs: transient messages, and the answer to "is there a server behind this
 * page?" -- asked once and shared, rather than probed separately by each.
 */
(function () {
  "use strict";

  var VISIBLE_MS = 3600;
  var FADE_MS = 300;

  function toast(message, kind) {
    var el = document.createElement("div");
    el.className = "mdweave-toast" + (kind ? " mdweave-toast--" + kind : "");
    el.textContent = message;
    document.body.appendChild(el);

    setTimeout(function () {
      el.classList.add("mdweave-toast--out");
      setTimeout(function () {
        el.remove();
      }, FADE_MS);
    }, VISIBLE_MS);

    return el;
  }

  /* A message in the middle of the page.
   *
   * For a failure about the gesture rather than the document -- a dropped file
   * we cannot take. The eye is on the cursor mid-drop, nowhere near the corner
   * a toast lives in, so that one is too easy to miss. Dismissed by clicking
   * it, by Escape, or by waiting. */
  var noticeEl = null;
  var noticeTimer = null;

  function dismissNotice() {
    if (noticeTimer) {
      clearTimeout(noticeTimer);
      noticeTimer = null;
    }
    if (!noticeEl) return;

    var el = noticeEl;
    noticeEl = null;
    el.classList.add("mdweave-notice--out");
    setTimeout(function () {
      el.remove();
    }, FADE_MS);
  }

  function notice(message) {
    dismissNotice(); // one at a time -- a second bad drop replaces the first

    var el = document.createElement("div");
    el.className = "mdweave-notice";
    el.setAttribute("role", "alert");

    var card = document.createElement("p");
    card.className = "mdweave-notice__card";
    card.textContent = message;
    el.appendChild(card);

    el.addEventListener("click", dismissNotice);
    document.body.appendChild(el);

    noticeEl = el;
    noticeTimer = setTimeout(dismissNotice, VISIBLE_MS);
    return el;
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") dismissNotice();
  });

  /* Swap freshly rendered prose into the page.
   *
   * Replacing only the article and the notes leaves the sidebar, the scroll
   * position, and everything else exactly where they were -- far less jarring
   * than a reload. Shared by the two things that produce new HTML: saving an
   * edit, and refreshing from disk.
   *
   * `notes.js` caches the note elements it found at load, so it has to be told
   * to go and look again. */
  function adopt(payload) {
    if (!payload) return;

    var doc = document.getElementById("doc");
    if (doc && typeof payload.body === "string") doc.innerHTML = payload.body;

    var layer = document.getElementById("notes-layer");
    if (layer && typeof payload.notes === "string") layer.innerHTML = payload.notes;

    if (window.mdweave && window.mdweave.refresh) window.mdweave.refresh();
  }

  /* --- describing a selection to the server --------------------------------
   *
   * Every top-level block is rendered carrying the source lines it came from
   * (data-src-start/end, written by SourceMappedRenderer), so a selection can
   * be described without any markdown crossing into the browser: a source-line
   * range, plus offsets into the text the reader can actually see.
   *
   * Two features need exactly that -- cutting a selection out (edit.js) and
   * copying it as markdown (copy.js) -- and one character of disagreement
   * between them would land an edit in the wrong place, so there is one
   * implementation and they share it. */

  function article() {
    return document.getElementById("doc");
  }

  function spanOf(block) {
    var start = parseInt(block.getAttribute("data-src-start"), 10);
    var end = parseInt(block.getAttribute("data-src-end"), 10);
    return isNaN(start) || isNaN(end) ? null : { start: start, end: end };
  }

  /* The block a node sits in, or null if it is not prose from the document. */
  function blockFor(node) {
    var doc = article();
    var el = node && node.nodeType === 3 ? node.parentNode : node;
    if (!doc || !el || !el.closest) return null;
    var block = el.closest("[data-src-start]");
    return block && doc.contains(block) ? block : null;
  }

  /* How far into a block's visible text a boundary sits. Mirrors what the
   * server computes with BeautifulSoup's get_text(), so the two agree on what
   * "character 40 of this paragraph" means. */
  function offsetIn(block, node, offset) {
    var range = document.createRange();
    range.selectNodeContents(block);
    range.setEnd(node, offset);
    return range.toString().length;
  }

  /* Break a selection into one {start, end, from, to} per block it touches. */
  function blockRanges(range) {
    var doc = article();
    if (!doc) return null;

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

    var ranges = [];
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
        ranges.push({ start: span.start, end: span.end, from: from, to: to });
      }
    }
    return ranges.length ? ranges : null;
  }

  /* Resolves to the /api/health payload, or null when the page is static.
   * Memoised: opening a document must not cost several identical probes. */
  var probe = null;

  function api() {
    if (probe) return probe;

    if (location.protocol.indexOf("http") !== 0) {
      probe = Promise.resolve(null); // file:// -- there is nothing to talk to
    } else {
      probe = fetch("/api/health")
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .catch(function () {
          return null;
        });
    }
    return probe;
  }

  /* Turn a failed response into an Error carrying whatever the server said.
   * An outdated server answers 501 with an HTML body, not JSON, so fall back
   * to the status line rather than swallowing it. */
  function failure(response) {
    return response
      .json()
      .catch(function () {
        return null;
      })
      .then(function (payload) {
        if (payload && payload.error) return new Error(payload.error);
        if (response.status === 501) {
          return new Error(
            "the server does not support this yet — run `mdweave start` to restart it, then reload"
          );
        }
        return new Error("server said " + response.status + " " + response.statusText);
      });
  }

  function reject(response) {
    return failure(response).then(function (error) {
      throw error;
    });
  }

  window.mdweaveUI = {
    toast: toast,
    notice: notice,
    dismissNotice: dismissNotice,
    adopt: adopt,
    blockFor: blockFor,
    spanOf: spanOf,
    blockRanges: blockRanges,
    api: api,
    reject: reject,
  };
})();
