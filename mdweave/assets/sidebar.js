/* mdweave -- sidebar state.
 *
 * The tree itself is plain HTML: <details> handles open/closed and keyboard
 * navigation natively. This file only remembers choices across page loads,
 * since clicking a document is a full navigation and would otherwise reset
 * every folder and re-open a panel you had just hidden.
 *
 * Loads before notes.js and does not depend on it.
 */
(function () {
  "use strict";

  var FOLDERS_KEY = "mdweave.folders.closed";
  var PANEL_KEY = "mdweave.sidebar";

  var sidebar = document.getElementById("sidebar");
  var collapse = document.getElementById("sidebar-collapse");
  var show = document.getElementById("sidebar-show");
  if (!sidebar) return;

  function read(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (err) {
      return fallback;
    }
  }

  function write(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (err) {
      /* private browsing, or the quota is full -- state just will not persist */
    }
  }

  /* --- folders ----------------------------------------------------------- */

  /* Closed folders are stored rather than open ones, so a newly added folder
   * shows up expanded by default. */
  var closed = read(FOLDERS_KEY, []);
  if (!Array.isArray(closed)) closed = [];

  function pathOf(details) {
    var parts = [];
    var node = details;
    while (node && node !== sidebar) {
      if (node.classList && node.classList.contains("tree__folder")) {
        parts.unshift(node.dataset.folder || "");
      }
      node = node.parentNode;
    }
    return parts.join("/");
  }

  sidebar.querySelectorAll(".tree__folder").forEach(function (details) {
    var path = pathOf(details);
    details.open = closed.indexOf(path) === -1;

    details.addEventListener("toggle", function () {
      var at = closed.indexOf(path);
      if (details.open && at !== -1) closed.splice(at, 1);
      else if (!details.open && at === -1) closed.push(path);
      write(FOLDERS_KEY, closed);
    });
  });

  /* --- panel visibility --------------------------------------------------- */

  function setHidden(hidden) {
    document.body.dataset.sidebar = hidden ? "hidden" : "shown";
    if (show) show.hidden = !hidden;
    if (collapse) collapse.setAttribute("aria-expanded", hidden ? "false" : "true");
    write(PANEL_KEY, hidden ? "hidden" : "shown");

    // Note positions are measured against the page, which just changed width.
    if (window.mdweave && window.mdweave.layout) window.mdweave.layout();
  }

  setHidden(read(PANEL_KEY, "shown") === "hidden");

  if (collapse) {
    collapse.addEventListener("click", function () {
      setHidden(true);
    });
  }
  if (show) {
    show.addEventListener("click", function () {
      setHidden(false);
    });
  }

  /* Keep the selected document in view when the tree is long. */
  var active = sidebar.querySelector(".tree__row--active");
  if (active && active.scrollIntoView) {
    active.scrollIntoView({ block: "nearest" });
  }

  /* --- importing ---------------------------------------------------------- */

  var ui = window.mdweaveUI;
  var importButton = document.getElementById("sidebar-import");
  var filePicker = document.getElementById("sidebar-file");
  var PREFIX = document.body.dataset.prefix || "";
  var DOCUMENTS_API = "/api/documents";

  function isMarkdown(file) {
    return /\.md$/i.test(file.name);
  }

  /* Send one file. A name clash comes back as 409; ask, then retry with an
   * explicit replace rather than silently overwriting the reader's work. */
  function upload(file, replace) {
    return file
      .text()
      .then(function (content) {
        return fetch(DOCUMENTS_API, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: file.name,
            content: content,
            replace: !!replace,
          }),
        });
      })
      .then(function (response) {
        if (response.status === 409 && !replace) {
          var again = window.confirm(
            '"' + file.name + '" already exists here. Replace it?'
          );
          return again ? upload(file, true) : null;
        }
        if (!response.ok) return ui.reject(response);
        return response.json().then(function (payload) {
          return payload.document;
        });
      })
      .catch(function (error) {
        ui.toast("Could not import " + file.name + ": " + error.message, "error");
        return null;
      });
  }

  function importFiles(list) {
    var files = Array.prototype.slice.call(list || []);
    if (!files.length) return;

    var markdown = files.filter(isMarkdown);
    var skipped = files.length - markdown.length;
    if (skipped) {
      ui.toast(
        skipped + (skipped === 1 ? " file was" : " files were") +
          " skipped — only .md can be imported",
        "warn"
      );
    }
    if (!markdown.length) return;

    sidebar.classList.add("sidebar--busy");

    // One at a time: each import re-renders every page, and a replace prompt
    // for two files at once would be a mess.
    markdown
      .reduce(function (chain, file) {
        return chain.then(function (done) {
          return upload(file).then(function (doc) {
            if (doc) done.push(doc);
            return done;
          });
        });
      }, Promise.resolve([]))
      .then(function (imported) {
        sidebar.classList.remove("sidebar--busy");
        if (!imported.length) return;

        // Opening the new document also refreshes the sidebar, since the
        // server has just regenerated every page.
        window.location.href = PREFIX + imported[0].href;
      });
  }

  if (importButton && filePicker) {
    importButton.addEventListener("click", function () {
      filePicker.click();
    });
    filePicker.addEventListener("change", function () {
      importFiles(filePicker.files);
      filePicker.value = ""; // so re-picking the same file fires change again
    });
  }

  /* --- drag and drop ------------------------------------------------------ */

  /* Dropping a file on a browser page normally navigates away from it, which
   * would look like the app crashing. Swallow it everywhere, then accept it
   * properly on the sidebar. */
  window.addEventListener("dragover", function (event) {
    event.preventDefault();
  });
  window.addEventListener("drop", function (event) {
    event.preventDefault();
  });

  function enableDrop() {
    // dragenter/dragleave fire for every child element crossed, so count the
    // nesting rather than toggling on each one.
    var depth = 0;

    function setActive(on) {
      sidebar.classList.toggle("sidebar--drop", on);
    }

    sidebar.addEventListener("dragenter", function (event) {
      event.preventDefault();
      depth += 1;
      setActive(true);
    });
    sidebar.addEventListener("dragover", function (event) {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    });
    sidebar.addEventListener("dragleave", function () {
      depth = Math.max(0, depth - 1);
      if (!depth) setActive(false);
    });
    sidebar.addEventListener("drop", function (event) {
      event.preventDefault();
      depth = 0;
      setActive(false);
      importFiles(event.dataTransfer && event.dataTransfer.files);
    });
  }

  /* Importing writes to disk, so it only exists when a server is listening. */
  if (ui) {
    ui.api().then(function (health) {
      if (!health) return;
      if (importButton) importButton.hidden = false;
      sidebar.classList.add("sidebar--importable");
      enableDrop();
    });
  }
})();
