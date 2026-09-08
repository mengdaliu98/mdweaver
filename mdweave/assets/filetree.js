/* mdweave -- rearranging the documents from the sidebar.
 *
 * Creating, importing, renaming, deleting, and dragging a row somewhere else.
 * Every one of them ends in the same place: the server rewrites the files and
 * regenerates every page, and this side takes a fresh one. There is no way to
 * patch the tree in place -- each page bakes its own copy of the sidebar, so a
 * sidebar that had been edited by hand would disagree with the next click.
 *
 * Loads after sidebar.js, which keeps the tree's own state and owns importing;
 * this file borrows `window.mdweaveSidebar` rather than uploading twice.
 *
 * Plain ES5, no build step, same as its neighbours.
 */
(function () {
  "use strict";

  var ui = window.mdweaveUI;
  var sidebar = document.getElementById("sidebar");
  if (!ui || !sidebar) return;

  var CURRENT = document.body.dataset.document || "";
  var PREFIX = document.body.dataset.prefix || "";

  /* A private drag type. `dataTransfer.getData` is off-limits until the drop,
   * so the only way to tell a row being dragged from a file off the desktop
   * mid-drag is to look at which types are on offer. */
  var ROW_TYPE = "application/x-mdweave-row";

  /* How much of a folder row counts as "before" or "after" it rather than
   * "into" it. A quarter each end leaves the middle half as the target you are
   * actually aiming for. */
  var EDGE = 0.28;

  var dragged = null; // {id, name, parent} while a row is in flight
  var marked = null; // the row currently drawn as the drop target

  /* --- talking to the server ---------------------------------------------- */

  function post(path, payload) {
    // `page` tells the server which document is on screen, so it can hand back
    // that page's freshly rendered panel -- it has no other way to know.
    payload.page = CURRENT;
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (response) {
      if (!response.ok) return ui.reject(response);
      return response.json();
    });
  }

  /* Show a tree change without reloading. Falls back to a reload when the
   * server sent no panel -- which happens when the page being read is the one
   * that just stopped existing. */
  function settle(payload) {
    if (!ui.adoptSidebar(payload && payload.sidebar)) window.location.reload();
    busy(false);
  }

  function busy(on) {
    sidebar.classList.toggle("sidebar--busy", on);
  }

  function fail(error) {
    busy(false);
    ui.toast(error.message, "error");
  }

  /* Every mutation rebuilds every page, so the only honest thing to do is take
   * one. If the document being read is the one that moved, its URL moved with
   * it and a plain reload would land on a 404. */
  function land(docId, payload) {
    // The page being read moved, so its address did too -- that one is a real
    // navigation. Every other row change is just the panel.
    if (payload && payload.document && docId === CURRENT) {
      window.location.href = PREFIX + payload.document.href;
      return;
    }
    settle(payload);
  }

  function join(folder, name) {
    return folder ? folder + "/" + name : name;
  }

  /* --- reading the tree ---------------------------------------------------- */

  /* The folder a row belongs to: itself if it is a folder, its parent if it is
   * a document. One rule, which is exactly what "in that folder, or beside
   * that file at its level" means. */
  function folderAt(node) {
    var details = node && node.closest ? node.closest(".tree__folder") : null;
    return details ? details.dataset.path || "" : "";
  }

  function listFor(path) {
    var lists = sidebar.querySelectorAll(".tree");
    for (var i = 0; i < lists.length; i += 1) {
      if ((lists[i].dataset.path || "") === path) return lists[i];
    }
    return null;
  }

  /* The name of one row, as the server knows it -- a path segment, not the
   * humanized label the reader sees. */
  function itemName(item) {
    var first = item.firstElementChild;
    if (!first) return "";
    if (first.classList.contains("tree__folder")) return first.dataset.folder || "";
    var link = first.querySelector(".tree__row--file");
    return link ? link.dataset.name || "" : "";
  }

  function namesIn(path) {
    var list = listFor(path);
    if (!list) return [];
    return Array.prototype.map.call(list.children, itemName);
  }

  /* --- the per-row controls ------------------------------------------------ */

  function fileOf(node) {
    var wrap = node.closest(".tree__file");
    return wrap ? wrap.querySelector(".tree__row--file") : null;
  }

  function labelOf(link) {
    var label = link.querySelector(".tree__label");
    return label ? label.textContent : link.dataset.name || "";
  }

  function create(button) {
    var folder = folderAt(button);
    var name = window.prompt("Name for the new document", "");
    if (name === null) return;
    name = name.trim();
    if (!name) return;

    busy(true);
    post("/api/documents/create", { path: join(folder, name) })
      .then(function (payload) {
        window.location.href = PREFIX + payload.document.href;
      })
      .catch(fail);
  }

  function rename(button) {
    var link = fileOf(button);
    if (!link) return;

    var was = link.dataset.name || "";
    var name = window.prompt("Rename this document", was);
    if (name === null) return;
    name = name.trim();
    if (!name || name === was) return;

    busy(true);
    post("/api/documents/move", {
      from: link.dataset.doc,
      to: join(link.dataset.parent || "", name),
    })
      .then(function (payload) {
        land(link.dataset.doc, payload);
      })
      .catch(fail);
  }

  function remove(button) {
    var link = fileOf(button);
    if (!link) return;

    /* Destructive and not undoable, so it asks -- and says what goes, since
     * the comments and the rendered page are not visible in the row. */
    var sure = window.confirm(
      'Delete "' +
        labelOf(link) +
        '"?\n\nThe document, its comments, and its page are removed. This cannot be undone.'
    );
    if (!sure) return;

    var docId = link.dataset.doc;
    busy(true);
    post("/api/documents/delete", { document: docId })
      .then(function (payload) {
        // There is no page left to go back to when it was this one; the root
        // hands out whatever document is first.
        if (docId === CURRENT) window.location.href = "/";
        else settle(payload);
      })
      .catch(fail);
  }

  /* --- folders -------------------------------------------------------------
   *
   * A folder row's buttons act *inside* it; the root's act at the top level.
   * `folderAt` already draws that distinction, so these read the same as their
   * document counterparts. */

  function newFolder(button) {
    var parent = folderAt(button);
    var name = window.prompt("Name for the new folder", "");
    if (name === null) return;
    name = name.trim();
    if (!name) return;

    busy(true);
    post("/api/folders/create", { path: join(parent, name) })
      .then(settle)
      .catch(fail);
  }

  /* The folder this button belongs to, or null at the root -- which has no
   * name to change and nothing to delete. */
  function folderRow(button) {
    var details = button.closest ? button.closest(".tree__folder") : null;
    return details && details.dataset.path ? details : null;
  }

  function renameFolder(button) {
    var details = folderRow(button);
    if (!details) return;

    var was = details.dataset.folder || "";
    var name = window.prompt("Rename this folder", was);
    if (name === null) return;
    name = name.trim();
    if (!name || name === was) return;

    var path = details.dataset.path;
    busy(true);
    post("/api/folders/rename", {
      from: path,
      to: join(path.split("/").slice(0, -1).join("/"), name),
    })
      .then(function (payload) {
        // Every document under it has a new id, so the page being read is at
        // an address that no longer exists; the root hands out another.
        if (CURRENT.indexOf(path + "/") === 0) window.location.href = "/";
        else settle(payload);
      })
      .catch(fail);
  }

  function removeFolder(button) {
    var details = folderRow(button);
    if (!details) return;

    var path = details.dataset.path;
    var inside = details.querySelectorAll(".tree__row--file").length;
    var warning = inside
      ? '"' + path + '" holds ' + inside + " document(s).\n\nDeleting it removes " +
        "them, their comments, and their pages. This cannot be undone."
      : 'Delete the empty folder "' + path + '"?';
    if (!window.confirm(warning)) return;

    busy(true);
    post("/api/folders/delete", { folder: path, recursive: inside > 0 })
      .then(function (payload) {
        if (CURRENT.indexOf(path + "/") === 0) window.location.href = "/";
        else settle(payload);
      })
      .catch(fail);
  }

  var ACTIONS = {
    create: create,
    "new-folder": newFolder,
    import: function (button) {
      var api = window.mdweaveSidebar;
      if (api) api.importInto(folderAt(button));
    },
    rename: rename,
    "rename-folder": renameFolder,
    delete: remove,
    "delete-folder": removeFolder,
  };

  function onClick(event) {
    var button = event.target.closest ? event.target.closest(".tree__action") : null;
    if (!button) return;

    // Inside a <summary>, an unhandled click folds the folder underneath it.
    event.preventDefault();
    event.stopPropagation();

    var action = ACTIONS[button.dataset.action];
    if (action) action(button);
  }

  /* --- dragging a row somewhere else --------------------------------------- */

  function isRowDrag(event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    return !!types && Array.prototype.indexOf.call(types, ROW_TYPE) !== -1;
  }

  /* Where the drop would land: which folder, and at which position among its
   * children. The ends of a row mean "between", the middle of a folder means
   * "inside", and anywhere off the tree means the top level. */
  function placeFor(event) {
    var hit = event.target.closest
      ? event.target.closest(".tree__row, .tree__file")
      : null;
    var row =
      hit && hit.classList.contains("tree__file")
        ? hit.querySelector(".tree__row--file")
        : hit;
    if (!row) return { parent: "", index: namesIn("").length, row: null, where: "root" };

    var box = row.getBoundingClientRect();
    var y = event.clientY - box.top;
    var folder = row.classList.contains("tree__row--folder");

    if (folder) {
      var edge = box.height * EDGE;
      if (y > edge && y < box.height - edge) {
        var path = row.parentNode.dataset.path || "";
        return { parent: path, index: namesIn(path).length, row: row, where: "into" };
      }
    }

    var item = row.closest(".tree__item");
    var list = item.parentNode;
    var parent = list.dataset.path || "";
    var at = Array.prototype.indexOf.call(list.children, item);
    var below = y >= box.height / 2;
    return {
      parent: parent,
      index: below ? at + 1 : at,
      row: row,
      where: below ? "after" : "before",
    };
  }

  var MARKS = ["tree__row--into", "tree__row--before", "tree__row--after"];

  function unmark() {
    if (marked) marked.classList.remove.apply(marked.classList, MARKS);
    sidebar.classList.remove("sidebar--root-drop");
    marked = null;
  }

  function mark(place) {
    unmark();
    if (!place.row) {
      sidebar.classList.add("sidebar--root-drop");
      return;
    }
    marked = place.row;
    marked.classList.add("tree__row--" + place.where);
  }

  /* A folder cannot become its own descendant. The server refuses it too, but
   * offering the drop and then failing it is a worse way to say so. */
  function swallowsItself(moved, place) {
    if (!moved.folder) return false;
    var target = place.parent;
    return target === moved.id || target.indexOf(moved.id + "/") === 0;
  }

  function apply(moved, place) {
    if (swallowsItself(moved, place)) {
      ui.toast("A folder cannot be moved inside itself", "error");
      return;
    }

    var names = namesIn(place.parent);
    var before = names.join("\n");
    var at = names.indexOf(moved.name);
    var index = place.index;

    if (place.parent === moved.parent) {
      if (at === -1) return; // the row is not where the DOM says it is
      names.splice(at, 1);
      // Removing it shifted everything after it up by one.
      if (at < index) index -= 1;
    } else if (at !== -1) {
      ui.toast(
        'There is already a "' + moved.name + '" in that folder', "error"
      );
      return;
    }
    names.splice(index, 0, moved.name);
    if (place.parent === moved.parent && names.join("\n") === before) return;

    // A move and a reorder are two writes, and either can be the whole story:
    // dropping between two rows of the folder you are already in is only an
    // order, and dropping onto a folder is a move whose order happens to be
    // the one the drop implies.
    busy(true);
    var moving =
      place.parent === moved.parent
        ? Promise.resolve(null)
        : post(
            moved.folder ? "/api/folders/rename" : "/api/documents/move",
            { from: moved.id, to: join(place.parent, moved.name) }
          );

    moving
      .then(function (payload) {
        return post("/api/tree/order", {
          folder: place.parent,
          order: names,
        }).then(function (ordered) {
          // Moving a row up or down inside its own folder is an order and
          // nothing else, so there was no move and no earlier payload: the
          // order response is the whole answer.
          if (!payload) return ordered;

          // Otherwise two writes, so two panels came back. The move's was
          // rendered before the arrangement was applied and is already out of
          // date; keep its `document`, which says where the page went, and
          // take the later panel.
          payload.sidebar = ordered.sidebar;
          return payload;
        });
      })
      .then(function (payload) {
        // Moving a folder renames every document under it, so the page being
        // read may have just changed address -- and unlike a document move
        // there is no `payload.document` naming where it went.
        if (moved.folder && CURRENT.indexOf(moved.id + "/") === 0) {
          window.location.href = "/";
          return;
        }
        land(moved.id, payload);
      })
      .catch(fail);
  }

  function onDragStart(event) {
    var row = event.target.closest
      ? event.target.closest(".tree__row--file, .tree__row--folder")
      : null;
    if (!row) return;

    var isFolder = row.classList.contains("tree__row--folder");
    dragged = {
      folder: isFolder,
      // A folder's id is its path, a document's is its path without the
      // suffix. Both are "where it lives", which is all a move needs.
      id: isFolder ? row.parentNode.dataset.path : row.dataset.doc,
      name: row.dataset.name,
      parent: row.dataset.parent || "",
    };
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData(ROW_TYPE, dragged.id);
    event.dataTransfer.setData("text/plain", dragged.id);
    row.classList.add("tree__row--dragging");
  }

  function onDragEnd() {
    var rows = sidebar.querySelectorAll(".tree__row--dragging");
    Array.prototype.forEach.call(rows, function (row) {
      row.classList.remove("tree__row--dragging");
    });
    dragged = null;
    unmark();
  }

  function enableDrag() {
    sidebar.addEventListener("dragstart", onDragStart);
    sidebar.addEventListener("dragend", onDragEnd);

    sidebar.addEventListener("dragover", function (event) {
      if (!isRowDrag(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      mark(placeFor(event));
    });

    sidebar.addEventListener("dragleave", function (event) {
      // dragleave fires for every child crossed; only the one that leaves the
      // panel altogether should take the indicator with it.
      if (!sidebar.contains(event.relatedTarget)) unmark();
    });

    sidebar.addEventListener("drop", function (event) {
      if (!isRowDrag(event)) return;
      event.preventDefault();

      var place = placeFor(event);
      var moved = dragged;
      onDragEnd();
      if (moved) apply(moved, place);
    });
  }

  /* All of this writes to disk, so none of it exists over `file://`. The
   * controls ship hidden and the class is what reveals them. */
  ui.api().then(function (health) {
    if (!health) return;
    sidebar.classList.add("sidebar--manageable");
    sidebar.addEventListener("click", onClick);
    enableDrag();
  });
})();
