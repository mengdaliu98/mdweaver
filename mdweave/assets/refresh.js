/* mdweave -- reloading a document from disk.
 *
 * The markdown is a file, and this page is not the only thing that writes to
 * it: an editor, a `git pull`, another machine. The rendered HTML next to it
 * goes stale silently. This button asks the server to render it again and
 * swaps the result in.
 *
 * A plain reload would not do -- it would just re-serve the same stale HTML.
 * The rebuild has to happen server-side first.
 *
 * Plain ES5, no build step, same as its neighbours.
 */
(function () {
  "use strict";

  var ui = window.mdweaveUI;
  var button = document.getElementById("refresh");
  if (!ui || !button) return;

  var DOC_ID = document.body.dataset.document || "";
  var PREFIX = document.body.dataset.prefix || "";
  var SETTLE_MS = 4000; // how long to wait for an in-flight edit to land

  var busy = false;

  /* The documents this page's sidebar currently knows about. If the set has
   * changed on disk, swapping the article is not enough -- the sidebar is
   * baked into each page, so only a real reload will pick the new one up. */
  function sidebarDocuments() {
    var links = document.querySelectorAll(".tree__row--file");
    return Array.prototype.map
      .call(links, function (link) {
        var href = link.getAttribute("href") || "";
        if (PREFIX && href.indexOf(PREFIX) === 0) href = href.slice(PREFIX.length);
        return href.replace(/\.html$/, "");
      })
      .sort()
      .join("\n");
  }

  /* Saving an edit is asynchronous; refreshing on top of one in flight would
   * throw the edit away. Wait for it, rather than refusing outright. */
  function whenSettled() {
    var edit = window.mdweaveEdit;
    if (!edit || !edit.isBusy()) return Promise.resolve();

    return new Promise(function (resolve) {
      var deadline = Date.now() + SETTLE_MS;
      var timer = setInterval(function () {
        if (!edit.isBusy() || Date.now() > deadline) {
          clearInterval(timer);
          resolve();
        }
      }, 100);
    });
  }

  function setBusy(on) {
    busy = on;
    button.disabled = on;
    button.classList.toggle("page-action--spinning", on);
  }

  function refresh() {
    if (busy) return;
    setBusy(true);

    whenSettled()
      .then(function () {
        return fetch("/api/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ document: DOC_ID }),
        });
      })
      .then(function (response) {
        if (!response.ok) return ui.reject(response);
        return response.json();
      })
      .then(function (payload) {
        // A document has appeared or disappeared: every sidebar on disk has
        // just been rewritten, so take the whole page again.
        if (payload.documents.join("\n") !== sidebarDocuments()) {
          window.location.reload();
          return;
        }
        ui.adopt(payload);
        setBusy(false);
      })
      .catch(function (error) {
        setBusy(false);
        ui.toast("Could not refresh: " + error.message, "error");
      });
  }

  button.addEventListener("click", refresh);

  /* Rendering happens on the server, so this only exists when one is there. */
  ui.api().then(function (health) {
    if (health) button.hidden = false;
  });
})();
