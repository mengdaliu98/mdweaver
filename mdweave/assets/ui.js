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
            "the server does not support this yet — restart it (mdweave_stop, then reload)"
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

  window.mdweaveUI = { toast: toast, api: api, reject: reject };
})();
