/* mdweave -- asking the Claude session that owns this document to do something.
 *
 * The button queues a job; a machine somewhere else -- the one with the
 * checkouts and the sessions on it -- takes the job, runs a turn, pushes the
 * result, and this page pulls it in. None of that happens here. What happens
 * here is a dialog, a progress list fed by an EventSource, and a swap of the
 * article when it is over.
 *
 * The progress stream is deliberately the only long-lived connection in the
 * design: the browser's EventSource reconnects on its own and says where it
 * got to, which is worth having and costs nothing. The other direction --
 * reaching the devserver -- cannot work this way at all, so it does not.
 *
 * Plain ES5, no build step, same as its neighbours.
 */
(function () {
  "use strict";

  var ui = window.mdweaveUI;
  var button = document.getElementById("agent");
  var dialog = document.getElementById("agent-dialog");
  if (!ui || !button || !dialog) return;

  var form = document.getElementById("agent-form");
  var picker = document.getElementById("agent-action");
  var input = document.getElementById("agent-instruction");
  var error = document.getElementById("agent-error");
  var cancel = document.getElementById("agent-cancel");
  var submit = document.getElementById("agent-submit");
  var log = document.getElementById("agent-log");
  var where = document.getElementById("agent-where");

  var DOC_ID = document.body.dataset.document || "";
  var KEY_NAME = "mdweave.operatorKey";

  var actions = [];
  var busy = false;
  var stream = null;

  /* The operator key, when the server is configured to want one. Kept in
   * sessionStorage rather than localStorage: a credential that runs commands
   * on somebody's devserver should not outlive the tab it was typed into.
   *
   * Sent as a custom header, which is also most of a CSRF defence on its own
   * -- a form on another site cannot set one, and a fetch that tries forces a
   * preflight this server does not answer. */
  function storedKey() {
    try {
      return window.sessionStorage.getItem(KEY_NAME) || "";
    } catch (e) {
      return "";
    }
  }

  function rememberKey(value) {
    try {
      window.sessionStorage.setItem(KEY_NAME, value);
    } catch (e) {
      /* private mode: it just gets asked for again */
    }
  }

  function headers() {
    var out = { "Content-Type": "application/json" };
    var key = storedKey();
    if (key) out["X-Mdweave-Operator-Key"] = key;
    return out;
  }

  function showError(text) {
    error.textContent = text;
    error.hidden = false;
  }

  function note(kind, text) {
    var row = document.createElement("li");
    row.className = "agent-log__row agent-log__row--" + kind;
    row.textContent = text;
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  }

  function currentAction() {
    for (var i = 0; i < actions.length; i++) {
      if (actions[i].name === picker.value) return actions[i];
    }
    return null;
  }

  function syncInstruction() {
    var action = currentAction();
    var wants = !action || action.needs_instruction;
    input.hidden = !wants;
    input.placeholder = (action && action.placeholder) || "What should change?";
  }

  function setBusy(on) {
    busy = on;
    submit.disabled = on;
    picker.disabled = on;
    input.readOnly = on;
    cancel.textContent = on ? "Close" : "Cancel";
    submit.textContent = on ? "Working…" : "Send to Claude";
  }

  function open() {
    error.hidden = true;
    log.innerHTML = "";
    input.value = "";
    where.textContent = DOC_ID;
    setBusy(false);
    syncInstruction();

    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute("open", "");
    input.focus();
  }

  function close() {
    if (stream) {
      stream.close();
      stream = null;
    }
    if (dialog.close) dialog.close();
    else dialog.removeAttribute("open");
  }

  /* Pull the finished work into the page.
   *
   * The devserver pushed to git and the site merged it in, so the prose on
   * disk here is already right -- this is the same rebuild the refresh button
   * does, and it goes through the same adopt() so the scroll position and the
   * sidebar survive. */
  function collect() {
    return fetch("/api/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document: DOC_ID }),
    })
      .then(function (response) {
        if (!response.ok) return ui.reject(response);
        return response.json();
      })
      .then(function (payload) {
        ui.adopt(payload);
      });
  }

  function watch(jobId) {
    stream = new EventSource("/api/agent/jobs/" + encodeURIComponent(jobId) + "/events");

    ["status", "text", "tool", "error", "done"].forEach(function (kind) {
      stream.addEventListener(kind, function (message) {
        var payload;
        try {
          payload = JSON.parse(message.data);
        } catch (e) {
          return;
        }
        note(payload.kind || kind, payload.text || "");
      });
    });

    stream.addEventListener("closed", function (message) {
      var state = "";
      try {
        state = (JSON.parse(message.data) || {}).state || "";
      } catch (e) {
        /* fall through to the generic message */
      }
      stream.close();
      stream = null;
      setBusy(false);

      if (state === "done") {
        note("status", "pulling the changes into this page…");
        collect()
          .then(function () {
            ui.toast("Claude updated this document");
            close();
          })
          .catch(function (err) {
            showError("It ran, but this page could not refresh: " + err.message);
          });
        return;
      }

      if (state === "stalled") {
        showError(
          "The runner stopped reporting. Nothing was retried — a Claude turn " +
            "is not safe to repeat automatically. Check `mdweave agent` on the devserver."
        );
        return;
      }
      showError("The job did not finish. See the log above.");
    });

    /* EventSource retries on its own; a visible error every time it does
     * would be noise, so only say something once it has actually given up. */
    stream.onerror = function () {
      if (stream && stream.readyState === 2) {
        note("error", "lost the progress stream — the job may still be running");
        setBusy(false);
      }
    };
  }

  function send() {
    var action = currentAction();
    if (!action) {
      showError("Pick something for Claude to do.");
      return;
    }
    var instruction = input.value.trim();
    if (action.needs_instruction && !instruction) {
      showError("Say what you want changed.");
      input.focus();
      return;
    }

    setBusy(true);
    error.hidden = true;
    log.innerHTML = "";
    note("status", "queued");

    fetch("/api/agent/jobs", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        document: DOC_ID,
        action: action.name,
        instruction: instruction,
      }),
    })
      .then(function (response) {
        if (response.status === 403) {
          return askForKey().then(function (again) {
            if (!again) throw new Error("the operator key is needed to queue work");
            return send();
          });
        }
        if (!response.ok) return ui.reject(response);
        return response.json().then(function (payload) {
          watch(payload.job.id);
        });
      })
      .catch(function (err) {
        setBusy(false);
        showError(err.message);
      });
  }

  function askForKey() {
    var typed = window.prompt(
      "This site asks for a separate key before it will run anything on the " +
        "devserver.\n\nOperator key:"
    );
    if (!typed) return Promise.resolve(false);
    rememberKey(typed.trim());
    return Promise.resolve(true);
  }

  button.addEventListener("click", open);
  picker.addEventListener("change", syncInstruction);

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (!busy) send();
  });

  cancel.addEventListener("click", function () {
    close();
  });

  /* While a job is running the dialog is a progress view, and Escape closing
   * it would leave the reader with no way back to it -- but the work carries
   * on regardless, so this only guards the moment before it is watchable. */
  dialog.addEventListener("cancel", function (event) {
    if (stream) event.preventDefault();
  });

  /* Only offer this when there is somewhere for the work to go. A button that
   * queues a job nobody will collect is worse than no button at all. */
  ui.api().then(function (health) {
    if (!health) return;

    fetch("/api/agent/status?document=" + encodeURIComponent(DOC_ID))
      .then(function (response) {
        // Read the body either way. A server with no bridge configured
        // answers 503 here, and returning without draining it leaves the
        // request in flight as far as the browser is concerned -- which is
        // invisible until something waits for the network to go quiet.
        return response.json().then(
          function (payload) {
            return response.ok ? payload : null;
          },
          function () {
            return null;
          }
        );
      })
      .then(function (status) {
        if (!status || !status.connected) return;

        actions = status.actions || [];
        if (!actions.length) return;

        picker.innerHTML = "";
        actions.forEach(function (action) {
          var option = document.createElement("option");
          option.value = action.name;
          option.textContent = action.label;
          picker.appendChild(option);
        });
        syncInstruction();

        var session = status.session || {};
        button.title = session.session_id
          ? "Claude session " +
            session.session_id.slice(0, 8) +
            " — join it with: claude --resume " +
            session.session_id
          : "Start a Claude session on this document";

        button.hidden = false;
      })
      .catch(function () {
        /* no bridge configured on this server: leave the button hidden */
      });
  });
})();
