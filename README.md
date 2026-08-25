# mdweave

Turning markdown into richly formatted, annotatable HTML: highlights, sticky-note
comments you can drag, and a VS Code style document sidebar.

This repo is the tool. It renders a *contents* directory that lives separately:

```
knowledge_base/
├── toolings/                  this repo (mdweaver)
│   ├── mdweave/               the renderer, server, and browser assets
│   ├── tests/
│   ├── mdweave_render.sh      shell helpers
│   └── pyproject.toml
└── contents/                  the documents (knowledge_base repo)
    ├── markdown_inputs/       source .md + .ann.json sidecars
    └── html_outputs/          generated .html + assets/
```

`markdown_inputs/` is the root of the sidebar. Its layout, including folders,
is the navigation -- add a subdirectory and it becomes a collapsible group.

## Setting up on a new machine

```bash
mkdir -p ~/AAI/knowledge_base && cd ~/AAI/knowledge_base
git clone git@github.com:mengdaliu98/mdweaver.git        toolings
git clone git@github.com:mengdaliu98/knowledge_base.git  contents

cd toolings
uv venv .venv && uv pip install --python .venv/bin/python -e .
.venv/bin/python -m pytest tests/          # expect 165 passing

echo 'source '"$PWD"'/mdweave_render.sh' >> ~/.zshrc && source ~/.zshrc
```

If the checkout is not at `~/AAI/knowledge_base`, set `MDWEAVE_HOME` to wherever
it is before sourcing the helper.

## Quick start

```bash
mdweave_render ~/notes/some_doc.md
```

That copies the file in, renders it, starts a local server if one is not
already up, and opens the page. `mdweave_stop` shuts the server down.

The helper restarts the server automatically when this repo's source has
changed underneath it -- a long-lived process keeps serving whatever it
imported at startup, which otherwise shows up as a new endpoint answering 501.

## The sidebar

Every page carries a VS Code style explorer over `contents/markdown_inputs`,
so any document is one click away. Folders are collapsible and remember their
state; the panel itself can be hidden and stays hidden across navigations.

**Importing.** The `+` in the panel header opens Finder; you can also drag
`.md` files onto the panel. Either way the file is copied into
`markdown_inputs/`, rendered, and opened — no restart, because every page is
regenerated so all their sidebars pick it up. Non-`.md` files are refused, and
a name clash asks before replacing. Filenames are reduced to something safe to
write and link to: any path is discarded down to the bare name, spaces become
underscores, and characters that break filenames or URLs are dropped.

Import needs the server, like commenting; over `file://` the button stays
hidden.

Labels drop the `.md` and read as prose: underscores become spaces and the
first letter is capitalised, with everything else left alone so deliberate
capitals survive.

| file                                    | sidebar label                      |
| --------------------------------------- | ---------------------------------- |
| `system_for_bio_literature_research.md` | System for bio literature research |
| `notes_about_CRAM_and_BAM.md`           | Notes about CRAM and BAM           |
| `ome-zarr-layout-planner.md`            | Ome-zarr-layout-planner            |

Hyphens are deliberately left alone — they read as part of a term (`OME-Zarr`)
rather than as a word separator. Change `humanize` in `mdweave/tree.py` if you
would rather they became spaces too.

A document's id is its path under the root without the suffix, so
`notes/weekly.md` is `notes/weekly`. That id is the sidebar link, the output
filename, and the `document` field the API takes.

## Adding comments in the browser

Select any text and a **Comment** pill appears; click it, type, and press
`Comment` (or `Cmd-Enter`). The comment is written straight into the
document's `.ann.json` sidecar and the HTML is regenerated, so a reload shows
exactly what you just made. Open a note and `Delete` removes it again.

This needs the page to be served, because a `file://` page has no API to write
to. `mdweave_render` handles that. By hand:

```bash
mdweave serve                      # http://127.0.0.1:8765/
```

Opened as a plain file the document is still perfectly readable — selecting
text just says it is read-only. The server binds to loopback and has no
authentication, so do not expose it.

Two things the server refuses, both with a message in the page: a selection it
cannot re-anchor, and one that overlaps an existing highlight. It verifies by
re-rendering the document before it writes anything, so a rejected comment
leaves no trace in the sidecar.

## How annotations work

A markdown file stays clean. Its annotations live beside it in a sidecar:

```
contents/markdown_inputs/
  system_for_bio_literature_research.md         <- untouched prose
  system_for_bio_literature_research.ann.json   <- highlights and comments
```

An annotation anchors to text by quoting it, in the style of the W3C Web
Annotation `TextQuoteSelector`:

```json
{
  "id": "kxejp",
  "kind": "comment",
  "color": "amber",
  "status": "open",
  "target": {
    "quote": "CRAM/BAM",
    "prefix": "minimal byte ranges for a genomic interval over",
    "suffix": "+ index. Scored on byte-exactness of the reasse"
  },
  "thread": [
    { "author": "me", "at": "2026-08-14T00:42:44.308Z", "body": "BAM and CRAM are ..." }
  ]
}
```

`prefix` and `suffix` only disambiguate a quote that appears more than once; if
they go stale the anchor degrades to a quote-only match rather than vanishing.
Anchors resolve against the *rendered* text, so a highlight survives the prose
being re-wrapped, and can sit inside a table cell or a syntax-highlighted code
block.

| field    | meaning                                                            |
| -------- | ------------------------------------------------------------------ |
| `id`     | stable identifier; also the DOM id (`hl-<id>`, `note-<id>`)         |
| `kind`   | `comment` renders a card; `highlight` is a bare highlight           |
| `color`  | a token name, or any raw CSS colour                                 |
| `status` | `open` or `resolved` (resolved fades the highlight and the note)    |
| `thread` | list of `{author, at, body}` — renders as a stacked conversation    |
| `tags`   | optional pills at the foot of the card                              |
| `offset` | `{dx, dy}` in px if the note was dragged; absent means "at anchor"  |

Unrecognised fields are preserved verbatim through a load/save round trip, so
new features do not need a schema migration.

## Changing colours

Six tokens — `amber`, `rose`, `mint`, `sky`, `violet`, `slate` — are defined in
one block at the top of `mdweave/theme/annotations.css`. Each has four values:

```css
--hl-amber-bg:     rgb(255 214  92 / 0.45);   /* fill behind the text   */
--hl-amber-edge:   rgb(202 138  4);           /* underline and note pin */
--note-amber-bg:   rgb(255 250 231);          /* card background        */
--note-amber-ink:  rgb( 66  47  6);           /* card text              */
```

Edit those and rebuild — every amber annotation follows. To recolour a single
annotation instead, put a raw colour in its sidecar entry; the fill, underline,
pin, and card tint are all derived from it with `color-mix()`:

```json
{ "color": "rgb(255 61 148)" }
```

`contents/markdown_inputs/mdweave_style_reference.md` renders all six tokens
plus a custom colour, and is the fastest way to preview a change.

## Reading the output

Comments are Preview.app-style sticky notes pinned to the upper-right corner
of the text they annotate: a small coloured square by default, click to open
the card, click again to close. The pin never moves when the card opens — the
card is a popover hanging off it, which flips to the other side or upwards when
it would run off the page.

Notes float above the page rather than occupying a column, so the text stays
centred and on a wide screen the cards land in the margin. Clicking highlighted
text opens its note; `Esc` closes everything. Printing lists the notes after the
document, since popovers cannot print.

**Drag a pin to move it.** The position is saved as a displacement from the
anchor, not an absolute coordinate, so a moved note still travels with its
highlight when the text around it changes. A moved note draws a faint dashed
line back to the text it belongs to, and gets a `Reset position` action in its
card. Dragging needs the server, same as commenting; over `file://` a drag
lasts only for the session.

## Commands

```bash
mdweave build <file.md|dir> -o <outdir>   # render; --strict fails on a lost anchor
                                          # a dir builds recursively; one file
                                          # still gets the full sidebar
mdweave serve [--port N] [--no-build]     # serve, and accept comments from the browser
mdweave extract <file.md> [--in-place]    # Obsidian inline comments -> sidecar JSON
mdweave fingerprint                       # hash of the installed source
```

From the shell helper: `mdweave_render <file.md>`, `mdweave_serve`,
`mdweave_stop`.

`extract` is a one-time migration for documents annotated with the Obsidian
`document-comments` plugin. `build` also reads that markup inline, so a
document renders correctly whether or not it has been migrated; the sidecar
wins on conflict.

## Extending it

- **A new annotation source** (a web UI, a database, another plugin format):
  add a module under `mdweave/sources/` that returns `list[Annotation]`.
  Nothing downstream changes.
- **A new visual treatment**: `mdweave/theme/*.css` and
  `mdweave/templates/document.html.j2`. The template is the only place HTML
  structure is decided.
- **Interactivity**: `mdweave/assets/` holds `ui.js` (toasts, the shared
  server probe), `sidebar.js` (tree state, importing), `notes.js` (sticky
  notes) and `annotate.js` (selecting and commenting). Plain ES5, no build
  step, copied verbatim next to the output and loaded in that order.
  `notes.js` publishes `window.mdweave` — `register`, `layout`, `setOpen`,
  `remove`, `resetPosition`, `setMoveHandler` — which is how `annotate.js` adds
  a note at runtime that behaves like a rendered one.
- **A new API endpoint**: `mdweave/serve.py`. `Workspace` owns all filesystem
  access and is the path-traversal guard; handlers never join a client string
  onto a path.

### Two invariants to respect

`annotate.js: selectorFor` is a hand-written mirror of `anchors.py:
selector_for` — the browser derives an anchor and Python has to find the same
span when it re-renders. Change one and you must change the other.
`tests/test_editing.py` guards this: `test_selector_round_trip_over_every_span`
checks every word-aligned span in seven documents survives the trip, and
`test_annotate_js_context_window_matches_python` catches the constant drifting.

Geometry constants are shared between CSS and JS by hand: `--pin` / `PIN` and
`--card-gap` / `CARD_GAP`. `layoutComposer` positions the comment editor in JS
where CSS positions a real note's card, so a drift there puts the editor in the
wrong place. `test_composer_and_note_cards_share_one_gap_value` catches it.
