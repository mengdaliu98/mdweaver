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
  var WIDTH_KEY = "mdweave.sidebar.width";

  var MIN_WIDTH = 150; // narrower than this and the labels are unreadable
  var MAX_WIDTH = 600; // wider and it starts eating the text column

  var sidebar = document.getElementById("sidebar");
  var collapse = document.getElementById("sidebar-collapse");
  var show = document.getElementById("sidebar-show");
  var handle = document.getElementById("sidebar-resize");
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

  /* --- panel width -------------------------------------------------------- */

  /* The width is one custom property, which the grid column and the handle's
   * own position both read -- so setting it moves everything at once. */
  function clampWidth(px) {
    return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.round(px)));
  }

  function applyWidth(px) {
    document.documentElement.style.setProperty("--sidebar-w", px + "px");
  }

  function settleWidth(px) {
    write(WIDTH_KEY, px);
    // Notes are positioned against the page, which just changed width. The
    // ResizeObserver in notes.js keeps up during the drag; this is the
    // final word once it stops.
    if (window.mdweave && window.mdweave.layout) window.mdweave.layout();
  }

  var storedWidth = read(WIDTH_KEY, null);
  if (typeof storedWidth === "number") applyWidth(clampWidth(storedWidth));

  if (handle) {
    handle.addEventListener("pointerdown", function (event) {
      // Stop the browser starting a text selection or a native drag.
      event.preventDefault();

      var width = clampWidth(event.clientX);
      document.body.classList.add("sidebar-resizing");
      if (handle.setPointerCapture) handle.setPointerCapture(event.pointerId);

      function move(moved) {
        width = clampWidth(moved.clientX); // the panel is flush left, so x is the width
        applyWidth(width);
      }

      function done() {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", done);
        handle.removeEventListener("pointercancel", done);
        document.body.classList.remove("sidebar-resizing");
        settleWidth(width);
      }

      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", done);
      handle.addEventListener("pointercancel", done);
    });

    /* Back to whatever the stylesheet says. */
    handle.addEventListener("dblclick", function () {
      document.documentElement.style.removeProperty("--sidebar-w");
      settleWidth(null);
    });

    handle.addEventListener("keydown", function (event) {
      var step = event.shiftKey ? 40 : 10;
      var current = sidebar.getBoundingClientRect().width;
      var next;

      if (event.key === "ArrowLeft") next = current - step;
      else if (event.key === "ArrowRight") next = current + step;
      else return;

      event.preventDefault();
      var width = clampWidth(next);
      applyWidth(width);
      settleWidth(width);
    });
  }

  /* Keep the selected document in view when the tree is long. */
  var active = sidebar.querySelector(".tree__row--active");
  if (active && active.scrollIntoView) {
    active.scrollIntoView({ block: "nearest" });
  }

  /* --- importing ---------------------------------------------------------- */

  var ui = window.mdweaveUI;
  var filePicker = document.getElementById("sidebar-file");
  var PREFIX = document.body.dataset.prefix || "";
  var DOCUMENTS_API = "/api/documents";
  var REJECTED = "only markdown files are supported";

  /* Which folder the next import lands in. The picker is one element shared by
   * every row's import button, so the destination is remembered here between
   * the click and the `change` it eventually produces. */
  var pendingFolder = "";

  function isMarkdown(file) {
    return /\.md$/i.test(file.name);
  }

  /* The folder a drop landed in: the innermost one enclosing the cursor.
   * An expanded folder's <details> spans its whole subtree, so dropping on a
   * document inside it puts the new file beside that document, not at the
   * top level -- which is what "into that folder" has to mean. */
  function detailsAt(target) {
    return target && target.closest ? target.closest(".tree__folder") : null;
  }

  function folderAt(target) {
    var details = detailsAt(target);
    return details ? details.dataset.path || "" : "";
  }

  /* Send one file. A name clash comes back as 409; ask, then retry with an
   * explicit replace rather than silently overwriting the reader's work. */
  function upload(file, folder, replace) {
    return file
      .text()
      .then(function (content) {
        return fetch(DOCUMENTS_API, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: file.name,
            content: content,
            folder: folder || "",
            replace: !!replace,
          }),
        });
      })
      .then(function (response) {
        if (response.status === 409 && !replace) {
          var again = window.confirm(
            '"' + file.name + '" already exists here. Replace it?'
          );
          return again ? upload(file, folder, true) : null;
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

  function importFiles(list, folder) {
    var files = Array.prototype.slice.call(list || []);
    if (!files.length) return;

    var markdown = files.filter(isMarkdown);
    if (markdown.length !== files.length) {
      ui.notice(REJECTED);
    }
    if (!markdown.length) return;

    sidebar.classList.add("sidebar--busy");

    // One at a time: each import re-renders every page, and a replace prompt
    // for two files at once would be a mess.
    markdown
      .reduce(function (chain, file) {
        return chain.then(function (done) {
          return upload(file, folder).then(function (doc) {
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

  /* Open the file picker on behalf of one row. */
  function importInto(folder) {
    if (!filePicker) return;
    pendingFolder = folder || "";
    filePicker.click();
  }

  if (filePicker) {
    filePicker.addEventListener("change", function () {
      importFiles(filePicker.files, pendingFolder);
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

  /* Files from the desktop, as opposed to a row being dragged within the tree
   * -- which filetree.js handles, and which must not put the panel into the
   * "drop a file here" state. */
  function hasFiles(event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    return !!types && Array.prototype.indexOf.call(types, "Files") !== -1;
  }

  /* Which folder row is lit as the destination. The drop lands wherever the
   * cursor is, so something has to say where that is -- a panel-wide highlight
   * would be a lie now that the top level is not the only answer. */
  var lit = null;

  function light(details) {
    var row = details ? details.firstElementChild : null;
    if (lit === row) return;
    if (lit) lit.classList.remove("tree__row--into");
    lit = row;
    if (row) row.classList.add("tree__row--into");
  }

  function enableDrop() {
    // dragenter/dragleave fire for every child element crossed, so count the
    // nesting rather than toggling on each one.
    var depth = 0;

    function setActive(on) {
      sidebar.classList.toggle("sidebar--drop", on);
      if (!on) light(null);
    }

    sidebar.addEventListener("dragenter", function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
      depth += 1;
      setActive(true);
    });
    sidebar.addEventListener("dragover", function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      light(detailsAt(event.target));
    });
    sidebar.addEventListener("dragleave", function (event) {
      if (!hasFiles(event)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) setActive(false);
    });
    sidebar.addEventListener("drop", function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
      depth = 0;
      setActive(false);
      importFiles(event.dataTransfer.files, folderAt(event.target));
    });
  }

  /* Importing writes to disk, so it only exists when a server is listening. */
  if (ui) {
    ui.api().then(function (health) {
      if (!health) return;
      sidebar.classList.add("sidebar--importable");
      enableDrop();
    });
  }

  /* filetree.js drives the per-row controls; importing stays here, so it hands
   * over the one entry point rather than a second copy of the upload dance. */
  window.mdweaveSidebar = { importInto: importInto, folderAt: folderAt };
})();
