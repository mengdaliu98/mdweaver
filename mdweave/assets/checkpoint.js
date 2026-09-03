/* mdweave -- checkpointing a document.
 *
 * A button in the top right, a dialog in the middle, and on submit the server
 * commits and pushes this one article with the message you wrote.
 *
 * The dialog is a real <dialog> opened with showModal(), which brings the
 * backdrop, the focus trap and Escape-to-close with it rather than having them
 * hand-rolled here.
 *
 * Plain ES5, no build step, same as its neighbours.
 */
(function () {
  "use strict";

  var ui = window.mdweaveUI;
  var button = document.getElementById("checkpoint");
  var dialog = document.getElementById("checkpoint-dialog");
  if (!ui || !button || !dialog) return;

  var form = document.getElementById("checkpoint-form");
  var message = document.getElementById("checkpoint-message");
  var what = document.getElementById("checkpoint-what");
  var error = document.getElementById("checkpoint-error");
  var cancel = document.getElementById("checkpoint-cancel");
  var submit = document.getElementById("checkpoint-submit");

  var DOC_ID = document.body.dataset.document || "";
  var busy = false;

  function showError(text) {
    error.textContent = text;
    error.hidden = false;
  }

  function open() {
    error.hidden = true;
    message.value = "";
    what.textContent = DOC_ID + ".md, its annotations, and its rendered page";
    setBusy(false);

    if (dialog.showModal) {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", ""); // very old browser: no backdrop, still usable
    }
    message.focus();
  }

  function close() {
    if (dialog.close) dialog.close();
    else dialog.removeAttribute("open");
  }

  function setBusy(on) {
    busy = on;
    submit.disabled = on;
    cancel.disabled = on;
    message.readOnly = on;
    submit.textContent = on ? "Pushing…" : "Commit and push";
  }

  function send() {
    var text = message.value.trim();
    if (!text) {
      showError("A checkpoint needs a message.");
      message.focus();
      return;
    }

    setBusy(true);
    error.hidden = true;

    fetch("/api/checkpoint", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document: DOC_ID, message: text }),
    })
      .then(function (response) {
        if (!response.ok) return ui.reject(response);
        return response.json();
      })
      .then(function (payload) {
        close();
        ui.toast(
          payload.committed
            ? "Checkpointed and pushed (" + payload.revision + ")"
            : "Nothing had changed; the remote is up to date"
        );
      })
      .catch(function (err) {
        setBusy(false);
        showError(err.message);
      });
  }

  button.addEventListener("click", open);

  form.addEventListener("submit", function (event) {
    // method="dialog" would close the dialog before the request is made.
    event.preventDefault();
    if (!busy) send();
  });

  cancel.addEventListener("click", function () {
    if (!busy) close();
  });

  // Escape reaches the dialog directly; refuse it only while a push is in
  // flight, so the reader is not left wondering whether it went through.
  dialog.addEventListener("cancel", function (event) {
    if (busy) event.preventDefault();
  });

  /* Committing needs the server, like everything else that writes. */
  ui.api().then(function (health) {
    if (health) button.hidden = false;
  });
})();
