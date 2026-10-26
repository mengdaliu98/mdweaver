/* mdweave -- drawing ```mermaid fences as diagrams.
 *
 * The server leaves each diagram as its source, in a <pre class="mermaid">
 * (render.py), and the drawing happens here. Mermaid is a few megabytes, so it
 * is fetched only by a page that has a diagram on it, and only once: a page
 * whose prose is swapped in place (ui.adopt) calls `render` again and the new
 * blocks are drawn with the library already loaded.
 *
 * If the library cannot be fetched -- offline, a blocked CDN -- the source
 * stays on the page as a code block, which is still the diagram, just unread.
 *
 * Plain ES5, no build step, same as its neighbours.
 */
(function () {
  "use strict";

  var SRC = "https://cdn.jsdelivr.net/npm/mermaid@11.12.0/dist/mermaid.min.js";
  var PENDING = "pre.mermaid:not([data-processed])";

  var loading = null;
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");

  function load() {
    if (loading) return loading;
    loading = new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = SRC;
      script.async = true;
      script.onload = function () {
        if (window.mermaid) resolve(window.mermaid);
        else reject(new Error("mermaid did not load"));
      };
      script.onerror = function () {
        reject(new Error("could not fetch " + SRC));
      };
      document.head.appendChild(script);
    });
    // A failed fetch is not cached: the next swap may be back online.
    loading.catch(function () {
      loading = null;
    });
    return loading;
  }

  /* --- full size ---------------------------------------------------------
   *
   * A diagram is drawn to the width of the column, and a wide one -- a dozen
   * classes side by side, nine lifelines -- comes out with its labels a fifth
   * of their size. Scrolling it at full size instead would be five screens
   * wide. So the page shows the overview and a button opens it full size.
   *
   * A button, not a click on the diagram: a double click is how a block is
   * opened for editing (edit.js), and its first click would open this. */

  var dialog = null;

  function openFull(svg) {
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.className = "diagram-dialog";
      dialog.setAttribute("aria-label", "Diagram, full size");
      // A click on the backdrop lands on the dialog itself.
      dialog.addEventListener("click", function (event) {
        if (event.target === dialog) dialog.close();
      });
      document.body.appendChild(dialog);
    }

    var copy = svg.cloneNode(true);
    // Mermaid sizes it to its container; here it gets its own width back.
    copy.style.maxWidth = "none";
    copy.setAttribute("width", svg.viewBox.baseVal.width);
    copy.setAttribute("height", svg.viewBox.baseVal.height);
    // Ids are unique per page, and the copy's styles are scoped by one.
    copy.id = svg.id + "-full";
    var style = copy.querySelector("style");
    if (style) style.textContent = style.textContent.split("#" + svg.id).join("#" + copy.id);

    dialog.innerHTML = "";
    dialog.appendChild(copy);
    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function addExpand(pre) {
    if (pre.querySelector(".diagram-expand")) return;
    var svg = pre.querySelector("svg");
    if (!svg) return;

    // The glyph is the stylesheet's, so the block's text -- which the editor
    // measures offsets in -- is the diagram's alone.
    var button = document.createElement("button");
    button.type = "button";
    button.className = "diagram-expand";
    button.title = "Open full size";
    button.setAttribute("aria-label", "Open diagram full size");
    button.addEventListener("click", function () {
      openFull(svg);
    });
    pre.appendChild(button);
  }

  function render() {
    var pending = document.querySelectorAll(PENDING);
    if (!pending.length) return Promise.resolve();

    return load()
      .then(function (mermaid) {
        mermaid.initialize({
          startOnLoad: false,
          theme: dark && dark.matches ? "dark" : "default",
          // "strict" escapes the labels and refuses click handlers: the
          // source is a document someone else may have written.
          securityLevel: "strict",
        });
        // Asked again rather than reusing `pending`: another call may have
        // drawn some of them while the library was loading.
        return mermaid.run({
          nodes: document.querySelectorAll(PENDING),
          suppressErrors: true,
        });
      })
      .then(function () {
        Array.prototype.forEach.call(
          document.querySelectorAll("pre.mermaid[data-processed]"),
          addExpand
        );
        if (window.mdweave && window.mdweave.layout) window.mdweave.layout();
      })
      .catch(function (error) {
        if (window.console) console.warn("mdweave: diagrams left as source:", error);
      });
  }

  window.mdweaveMermaid = { render: render };
  render();
})();
