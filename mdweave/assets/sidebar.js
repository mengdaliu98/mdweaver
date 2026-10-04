/* mdweave -- sidebar state.
 *
 * The tree itself is plain HTML: <details> handles open/closed and keyboard
 * navigation natively. This file only remembers choices across page loads,
 * since clicking a document is a full navigation and would otherwise reset
 * every folder and re-open a panel you had just hidden.
 *
 * It is not the first to read that memory. This file is deferred, so by the
 * time it runs the page has already been painted a frame or two -- which used
 * to be a frame or two of the server's defaults, a 16rem panel with every
 * folder open, before this snapped it back. The inline script in the <head>
 * of document.html.j2 now reads the same three keys before the body is parsed
 * and so owns what the reader actually sees first. What is left here is the
 * half that needs the whole page -- the listeners, the controls outside the
 * panel, and the tree filetree.js re-renders later -- plus the same restore
 * as a fallback, for a browser the early pass could not run in.
 * Where the two overlap, this one checks before it writes, so a state the
 * head already put back is not put back a second time.
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
  // Both are repeated as literals by the pre-paint script in
  // document.html.j2. Change one and change the other: a stored width the two
  // clamp differently is applied twice, which is the jump that script exists
  // to remove.

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

  /* Restore each folder's open state and remember any change to it.
   *
   * A function rather than a one-off, because the panel's contents are
   * replaced whenever the tree changes: filetree.js delegates its listeners to
   * the panel itself and survives that, but a `toggle` handler bound to one
   * `<details>` goes with the element it was bound to. */
  function wireFolders() {
    sidebar.querySelectorAll(".tree__folder").forEach(function (details) {
      if (details.dataset.wired === "1") return;
      details.dataset.wired = "1";

      var path = pathOf(details);
      var open = closed.indexOf(path) === -1;
      // Only if it is wrong. The folders in the page as it loaded were shut
      // before their first paint by the script in the head; touching `open`
      // again here would be the second write the flash was made of, and a
      // `toggle` listener the panel grows later would see an event for a
      // change that did not happen. The rows filetree.js splices in after a
      // rename or a drag have had no such pass, and are what this is for.
      if (details.open !== open) details.open = open;

      details.addEventListener("toggle", function () {
        var at = closed.indexOf(path);
        if (details.open && at !== -1) closed.splice(at, 1);
        else if (!details.open && at === -1) closed.push(path);
        write(FOLDERS_KEY, closed);
      });
    });
  }

  wireFolders();

  /* --- panel visibility --------------------------------------------------- */

  function setHidden(hidden) {
    document.body.dataset.sidebar = hidden ? "hidden" : "shown";
    if (show) show.hidden = !hidden;
    if (collapse) collapse.setAttribute("aria-expanded", hidden ? "false" : "true");
    write(PANEL_KEY, hidden ? "hidden" : "shown");

    // Note positions are measured against the page, which just changed width.
    if (window.mdweave && window.mdweave.layout) window.mdweave.layout();
  }

  // The head script has already put `data-sidebar` on the body, which is the
  // half of this that CSS reads and so the half that had to beat the first
  // paint. The rest -- the floating re-open button, the collapse button's
  // `aria-expanded` -- is script-only and can wait for this.
  setHidden(read(PANEL_KEY, "shown") === "hidden");

  /* The two controls that live inside the panel, and so are replaced with it.
   * `show` sits outside and is wired once, below. */
  function wireHead() {
    if (collapse && collapse.dataset.wired !== "1") {
      collapse.dataset.wired = "1";
      collapse.setAttribute("aria-expanded", document.body.dataset.sidebar !== "hidden");
      collapse.addEventListener("click", function () {
        setHidden(true);
      });
    }
    if (filePicker && filePicker.dataset.wired !== "1") {
      filePicker.dataset.wired = "1";
      filePicker.addEventListener("change", function () {
        importFiles(filePicker.files, pendingFolder);
        filePicker.value = ""; // so re-picking the same file fires change again
      });
    }
  }

  // Not called here: `filePicker` is declared further down and would still be
  // undefined. The one call at load happens once both controls exist.
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

  // The head has already set this property to the same number of pixels, so
  // this is a fallback and not the thing the reader sees: it is what puts the
  // width back where the early pass could not run. Writing the identical
  // value changes nothing, so it is left unconditional rather than guarded
  // against a string the browser would have normalised anyway.
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
  var CURRENT = document.body.dataset.document || "";
  var DOCUMENTS_API = "/api/documents";
  var REJECTED = "only markdown files are supported";

  /* What counts as a document, applied to a filename and to a dropped path
   * alike -- the two have to agree, or a file the drop refuses could still be
   * linked by its path. */
  var MARKDOWN = /\.md$/i;

  /* Which folder the next import lands in. The picker is one element shared by
   * every row's import button, so the destination is remembered here between
   * the click and the `change` it eventually produces. */
  var pendingFolder = "";

  function isMarkdown(file) {
    return MARKDOWN.test(file.name);
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

  // Both in-panel controls, wired through the same function that re-wires them
  // after the panel's contents are replaced.
  wireHead();

  /* --- linking a file that already lives on the serving machine ------------
   *
   * An import copies. It has no choice: a plain HTML5 file drop hands over
   * `File` objects carrying a name and bytes and deliberately no path, so the
   * only way to get the content in is to write a second copy of it. That copy
   * then drifts. The author goes on editing the original where it belongs --
   * in the repository, beside the code it documents -- and the document here
   * quietly becomes last week's snapshot of it.
   *
   * Most of the places a file is dragged *from* say more than the drop API
   * does. VS Code's explorer, GNOME Files, Dolphin and nearly every other
   * Linux file manager also set `text/uri-list` to a `file:///absolute/path`
   * URI naming the thing being dragged. When that is on offer there is no
   * reason to copy: the server can put a symlink in the knowledge base
   * pointing at the original, and then there is one file, edits from either
   * side are the same edit, and the document stays live with its project.
   *
   * All of which only means anything when the browser and the server are the
   * same machine. mdweave is also deployed to Railway, where the page is
   * served by a container on the other side of the internet and a path off
   * the reader's laptop names nothing at all over there: the server looks,
   * finds no such file, and answers 400. That is a correct answer to a
   * question that should not have been asked, so `explain` below turns it
   * into the sentence the reader actually needs -- that this feature belongs
   * to a locally served knowledge base -- rather than a bare refusal.
   */
  var LINK_API = "/api/documents/link";
  var URI_LIST = "text/uri-list";

  /* One `file://` URI as a filesystem path, or "" for anything that is not
   * one.
   *
   * The host has to be read, not waved past. `file:///notes.md` names a
   * file here and `file://laptop/notes.md` names one on a machine called
   * laptop, and the whole difference between them is that empty host. Only an
   * empty host and the spelled-out `localhost` are ours.
   *
   * Percent-decoding is what turns `%20` back into the space the filename
   * really has. A malformed escape makes `decodeURIComponent` throw, and a
   * URI that cannot be read is one we decline to guess at. */
  function pathOfUri(uri) {
    if (!/^file:\/\//i.test(uri)) return "";

    var rest = uri.slice("file://".length);
    var slash = rest.indexOf("/");
    if (slash === -1) return "";
    var host = rest.slice(0, slash);
    if (host && host.toLowerCase() !== "localhost") return "";

    // Everything from a `#` on is a fragment. A `#` that is part of the
    // filename arrives as %23 and so survives this untouched.
    var path = rest.slice(slash);
    var hash = path.indexOf("#");
    if (hash !== -1) path = path.slice(0, hash);

    try {
      path = decodeURIComponent(path);
    } catch (err) {
      return "";
    }

    // Absolute or nothing: a relative path would be relative to a working
    // directory the browser has no idea about.
    return path.charAt(0) === "/" ? path : "";
  }

  /* Every absolute path a drag is carrying, in the order they were dragged.
   *
   * `text/uri-list` is RFC 2483 -- one URI per line, with `#` comment lines --
   * so blanks and comments have to be dropped rather than parsed. `text/plain`
   * is read only when that type is absent or empty: a source that sets both
   * sets them to the same thing, so merging them would link every file twice.
   */
  function pathsFrom(transfer) {
    if (!transfer || !transfer.getData) return [];

    var raw;
    try {
      raw = transfer.getData(URI_LIST) || transfer.getData("text/plain") || "";
    } catch (err) {
      return []; // reading the payload is only allowed on the drop itself
    }

    var paths = [];
    raw.split(/\r?\n/).forEach(function (line) {
      var path = pathOfUri(line.trim());
      if (path) paths.push(path);
    });
    return paths;
  }

  function baseOf(path) {
    return path.slice(path.lastIndexOf("/") + 1);
  }

  /* Whether this page could plausibly be coming from the machine the reader
   * is sitting at. A path means something only to the process that has to
   * open it, so a drop onto a sidebar served from anywhere else is lost
   * before it is sent, and saying so beats letting the server's 404-shaped
   * 400 stand on its own. */
  function servedLocally() {
    var host = location.hostname;
    return (
      host === "" ||
      host === "localhost" ||
      host === "127.0.0.1" ||
      host === "::1" ||
      host === "[::1]"
    );
  }

  function explain(message) {
    if (servedLocally()) return message;
    return (
      message +
      " — this page is served by " +
      location.hostname +
      " and the file is on this one. Linking only works when mdweave is " +
      "running on the machine you are dragging from."
    );
  }

  /* Ask the server for one symlink. Resolves to the payload, or to null when
   * the reader backed out of the name clash. */
  function link(path, folder, name) {
    var body = { path: path, folder: folder || "", page: CURRENT };
    if (name) body.name = name;

    return fetch(LINK_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (response) {
      /* Unlike an import, there is no "replace it?" to offer. A replace would
       * throw away a real document and leave a pointer where it used to be,
       * which is not a thing anyone means to ask for. Renaming the link is
       * the only way past a clash, so that is what gets offered -- and only
       * once, since a second clash on a name just typed is an answer in
       * itself. */
      if (response.status === 409 && !name) {
        var pick = window.prompt(
          'There is already a document called "' +
            baseOf(path) +
            '" there. Give the link a different name, or cancel.',
          ""
        );
        if (pick === null) return null;
        // A "/" in the answer is part of the name, exactly as it is for a new
        // document: the folder comes from where the file was dropped and
        // never from what was typed.
        pick = pick.trim().replace(/\s*[\\/]+\s*/g, "_");
        if (!pick) return null;
        if (!MARKDOWN.test(pick)) pick += ".md";
        return link(path, folder, pick);
      }

      if (!response.ok) {
        // 400 is the one that needs more than the server's own words: the
        // likeliest cause is that the file is on this side of the wire and
        // the server is on the other.
        var wrongMachine = response.status === 400;
        return ui.reject(response).catch(function (error) {
          throw new Error(wrongMachine ? explain(error.message) : error.message);
        });
      }

      return response.json();
    });
  }

  /* Say "linked", and say it loudly.
   *
   * A linked document and a copied one are the same row in the tree, and the
   * difference between them is the thing the reader has to walk away knowing:
   * typing here types into a file somewhere else, and the knowledge base now
   * leans on a path outside itself. The revision is named because the server
   * commits and pushes the symlink on the way out, and a push nobody
   * mentioned is a push someone discovers later. */
  function describe(linked) {
    var first = linked[0];
    var what =
      linked.length > 1
        ? linked.length + " documents"
        : '"' + first.document.label + '" to ' + first.linked;
    var revision = linked[linked.length - 1].revision;
    return (
      "Linked " +
      what +
      " — not copied, so edits flow both ways." +
      (revision ? " Committed and pushed as " + revision.slice(0, 7) + "." : "")
    );
  }

  function linkPaths(paths, folder) {
    if (!paths.length) return;
    sidebar.classList.add("sidebar--busy");

    // One at a time, for the same reasons an import is: each one rewrites the
    // tree and re-renders every page with it, and two rename prompts racing
    // each other would be a mess.
    paths
      .reduce(function (chain, path) {
        return chain.then(function (done) {
          return link(path, folder)
            .then(function (payload) {
              if (payload) done.push(payload);
              return done;
            })
            .catch(function (error) {
              ui.toast("Could not link " + path + ": " + error.message, "error");
              return done;
            });
        });
      }, Promise.resolve([]))
      .then(function (linked) {
        sidebar.classList.remove("sidebar--busy");
        if (!linked.length) return;

        // Take the panel the server rendered for *this* page before leaving
        // it. The navigation below brings a correct sidebar of its own, but
        // it is a page fetch away, and until it lands the tree on screen is
        // the one from before the link existed.
        var first = linked[0];
        ui.adoptSidebar(first.sidebar);
        ui.toast(describe(linked), "info");
        window.location.href = PREFIX + first.document.href;
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

  /* A drag that is offering paths. What it is offering them *as* cannot be
   * read yet -- `getData` stays shut until the drop, so that a page cannot
   * read what is merely being dragged across it -- and the list of types is
   * the whole of what a drag will say about itself mid-flight. That is enough
   * to accept the drop and to promise a link on the banner; whether the URIs
   * really are local files is settled when they land.
   *
   * Worth asking separately from `hasFiles` because the two do not always
   * travel together: a source that offers only a URI list would otherwise be
   * refused in `dragover` and never produce a drop at all. A row dragged
   * within the tree cannot be mistaken for one of these: filetree.js offers
   * its own private type and `text/plain`, never a URI list. */
  function hasPaths(event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    return !!types && Array.prototype.indexOf.call(types, URI_LIST) !== -1;
  }

  function wanted(event) {
    return hasFiles(event) || hasPaths(event);
  }

  /* The `effectAllowed` values that include linking. Asking for an effect the
   * source does not offer does not get a different cursor -- it sets the
   * effect to "none", and the drop then never happens at all. */
  var LINKABLE = /^(link|copyLink|linkMove|all|uninitialized)$/;

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

  /* The banner along the foot of the panel, told which of the two things is
   * about to happen while there is still time to let go somewhere else --
   * once the row is in the tree a link and a copy look identical. Looked up
   * each time and asked for its own original wording rather than given one
   * here: the template writes it, and the panel is replaced wholesale every
   * time the tree changes. */
  function banner(text) {
    var zone = sidebar.querySelector(".sidebar__dropzone span");
    if (!zone) return;
    if (!zone.dataset.said) zone.dataset.said = zone.textContent;
    zone.textContent = text || zone.dataset.said;
  }

  function enableDrop() {
    // dragenter/dragleave fire for every child element crossed, so count the
    // nesting rather than toggling on each one.
    var depth = 0;

    function setActive(on, linking) {
      sidebar.classList.toggle("sidebar--drop", on);
      sidebar.classList.toggle("sidebar--link-drop", !!(on && linking));
      banner(on && linking ? "Drop to link, not copy" : null);
      if (!on) light(null);
    }

    sidebar.addEventListener("dragenter", function (event) {
      if (!wanted(event)) return;
      event.preventDefault();
      depth += 1;
      setActive(true, hasPaths(event));
    });
    sidebar.addEventListener("dragover", function (event) {
      if (!wanted(event)) return;
      event.preventDefault();
      // "link" is the cursor the platform draws for a pointer rather than a
      // duplicate, which is exactly the distinction being made here -- but
      // the panel takes the drop either way, so the arrow gives way to the
      // drop rather than the other way round.
      if (event.dataTransfer) {
        var pointing =
          hasPaths(event) && LINKABLE.test(event.dataTransfer.effectAllowed);
        event.dataTransfer.dropEffect = pointing ? "link" : "copy";
      }
      light(detailsAt(event.target));
    });
    sidebar.addEventListener("dragleave", function (event) {
      if (!wanted(event)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) setActive(false);
    });
    sidebar.addEventListener("drop", function (event) {
      if (!wanted(event)) return;
      event.preventDefault();
      depth = 0;
      setActive(false);

      var folder = folderAt(event.target);
      var paths = pathsFrom(event.dataTransfer);
      var markdown = paths.filter(function (path) {
        return MARKDOWN.test(path);
      });

      /* A real path wins over the bytes every time: it is the same file
       * either way, and this way there is only one of it. Anything the parse
       * would not vouch for -- a drag with no file URI in it, a URI naming
       * another machine, an escape that would not decode -- falls through to
       * the copy-import that has always been here, which is also what keeps
       * a browser that says nothing about paths working exactly as before. */
      if (markdown.length) {
        // Some of what was dragged is not a document. Same refusal as an
        // import gives, and the rest still goes in.
        if (markdown.length !== paths.length) ui.notice(REJECTED);
        linkPaths(markdown, folder);
        return;
      }

      /* A directory or a .txt arrives here with its bytes as well as its
       * path, and `importFiles` turns both away with the centred notice, as
       * it always has. A drag carrying no files at all -- a link pulled off
       * another page, say -- would leave it nothing to answer, so answer
       * here instead of going quiet. */
      var files = event.dataTransfer.files;
      if (files && files.length) importFiles(files, folder);
      else ui.notice(REJECTED);
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
  /* Re-adopt the panel after its contents have been replaced. The collapse
   * button and the file input live inside it and are recreated with it, so the
   * module's references to them go stale along with their listeners. */
  function rewire() {
    collapse = document.getElementById("sidebar-collapse");
    filePicker = document.getElementById("sidebar-file");
    wireFolders();
    wireHead();
  }

  window.mdweaveSidebar = {
    importInto: importInto,
    folderAt: folderAt,
    rewire: rewire,
  };
})();
