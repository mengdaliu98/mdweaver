# mdweave

Turning markdown into richly formatted, annotatable HTML: highlights, sticky-note
comments you can drag, and a VS Code style document sidebar.

This repo is the tool. It renders the documents of a *contents* repo that is
checked out separately, as its sibling:

```
<parent>/
├── mdweaver/                  this repo, the tool
│   ├── mdweave/               the renderer, server, and browser assets
│   ├── tests/
│   └── pyproject.toml
└── knowledge_base/            the documents
    ├── markdown_inputs/       source .md + .ann.json sidecars
    └── html_outputs/          generated .html + assets/
```

`markdown_inputs/` is the root of the sidebar. Its layout, including folders,
is the navigation -- add a subdirectory and it becomes a collapsible group.

## Setting up on a new machine

```bash
cd <parent>                    # e.g. /data/users/mengda
git clone git@github.com:mengdaliu98/mdweaver.git
git clone git@github.com:mengdaliu98/knowledge_base.git

cd mdweaver
uv venv .venv && uv pip install --python .venv/bin/python -e .
.venv/bin/python -m pytest tests/          # expect 181 passing

ln -s "$PWD/.venv/bin/mdweave" ~/.local/bin/mdweave    # or add .venv/bin to PATH
```

Nothing needs sourcing into your shell. `mdweave` finds the documents by
looking for `knowledge_base` next to its own checkout, so the command works
from any directory. If the two repos are not siblings, point `MDWEAVE_CONTENTS`
at the contents checkout.

## Quick start

Two commands:

```bash
mdweave start        # render everything, serve it, open it
mdweave stop         # shut the server down
```

`start` renders every document, brings up a local server if one is not already
running, and opens the knowledge base in a browser. There is no file argument:
the sidebar on every page is how you reach the other documents.

It is safe to run repeatedly. Rendering happens every time, so a document added
by hand shows up on the next `start`; the server is left alone if it is already
serving current code. When it is *not* — when this repo's source has changed
underneath it — `start` restarts it, because a long-lived process keeps serving
whatever it imported at startup, which otherwise shows up as a new endpoint
answering 501.

The page opens with `open` on macOS and `xdg-open` on Linux. On a headless box
neither can do anything, so `start` says so and prints the URL — forward port
8765 to your laptop and open it there.

`--port` moves both commands off 8765, and `--no-open` prints the URL without
reaching for a browser.

## The sidebar

Every page carries a VS Code style explorer over `knowledge_base/markdown_inputs`,
so any document is one click away. Folders are collapsible and remember their
state; the panel itself can be hidden and stays hidden across navigations.

**Managing the files.** Hover a row — a folder or a document — and four
controls appear on its right: `+` makes a new document, the tray arrow imports
one from disk, and on a document row the pencil renames and the bin deletes.
Where they act is the row they hang off: *in* that folder, or *beside* that
document at its level. They are ordinary buttons, so `Tab` reaches them too;
they are laid over the label rather than taking width from it, which is why
they are faded out rather than hidden.

Delete asks first. It takes the prose, the `.ann.json` beside it, and the
rendered page, and none of that comes back.

**Importing.** The tray arrow on any row opens Finder; you can also drag `.md`
files onto the panel, and the folder under the cursor lights up as the one they
will land in — an expanded folder covers its whole subtree, so dropping on a
document inside it puts the file beside that document. Either way the file is
copied into `markdown_inputs/`, rendered, and opened — no restart, because
every page is regenerated so all their sidebars pick it up. A name clash asks
before replacing. Filenames are reduced to something safe to write and link to:
any path is discarded down to the bare name, spaces become underscores, and
characters that break filenames or URLs are dropped.

**Rearranging.** Drag a document row onto a folder to move it in, onto the
empty space below the tree to bring it back out to the top level, or between
two rows to put it exactly there. A document's id is its path, so moving it
renames it: the `.md`, its `.ann.json`, and its generated `.html` all travel
together, and the page at the old address is removed rather than left to serve
a document that has gone. Renaming from the pencil is the same operation, and
keeps the row's position rather than sending it to the bottom of the folder.

Order is not a property of the filesystem, so it is written down:
`markdown_inputs/.mdweave-order.json` maps a folder path — `""` for the root —
to the order of its children. Anything not listed falls back to the old sort,
folders first and then alphabetical, *after* the names that are listed; a
document someone else adds appears at the end rather than in the middle of an
arrangement it was never part of. Delete the file and the tree goes back to
being sorted.

Anything that is not a `.md` is refused with **only markdown files are
supported**, centred on the page rather than tucked into the corner a toast
lives in — mid-drag your eye is on the cursor, not down there. Click it,
press `Esc`, or wait, and it goes. The check is on both sides: the browser
filters the drop, and `/api/documents` refuses the same thing again.

All of this writes to disk, so it needs the server, like commenting. Over
`file://` the row controls never appear and a dragged row goes nowhere.

Labels drop the `.md` and read as prose, in sentence case: underscores *and*
hyphens become spaces, the first letter goes up, and the rest goes down.

| file                                    | sidebar label                      |
| --------------------------------------- | ---------------------------------- |
| `system_for_bio_literature_research.md` | System for bio literature research |
| `ome-zarr-layout-planner.md`            | Ome zarr layout planner            |
| `notes_about_CRAM_and_BAM.md`           | Notes about cram and bam           |

The last row is the cost of the rule: an acronym in a *filename* loses its
capitals, because nothing distinguishes it from an ordinary word. The document
keeps whatever title its own `# heading` gives it. `humanize` in
`mdweave/tree.py` is the one place to change if that trade stops being worth it.

**Folders.** The `+`-in-a-folder button makes one: on a folder row it nests
inside, on the strip below the tree it lands at the top level — which is the
only way to get a first folder in a knowledge base that has none. An empty
folder is still drawn, because a folder you cannot see is one you cannot drop
anything into. Renaming a folder carries every document under it, and so
changes their ids; deleting one refuses unless it is empty or the request says
`recursive`.

**Cost.** A tree change alters the navigation on every page and none of their
prose, so only the one region that changed is rewritten: `render_sidebar`
produces the panel and `splice_sidebar` swaps it between the markers each page
carries, skipping the markdown parse and the syntax highlighting entirely. The
browser is handed its own page's new panel and swaps that in rather than
reloading. Rebuilding all of it took about two seconds to move one row; it is
now about a fifth of a second, and nothing on screen flickers.

**Resizing.** Drag the panel's right edge. The width persists across
navigations, is clamped to 150–600px, and takes the arrow keys once the handle
has focus (`Shift` for bigger steps). Double-click it to go back to the
stylesheet's `--sidebar-w`. The handle hides itself when the panel is hidden,
and on narrow screens where the panel is an overlay rather than a column.

A document's id is its path under the root without the suffix, so
`notes/weekly.md` is `notes/weekly`. That id is the sidebar link, the output
filename, and the `document` field the API takes.

## Editing the prose in the browser

Click a paragraph and it becomes a small box holding *its own* markdown —
just that block. Click away and it turns back into rendered prose. The rest of
the page never changes, and at no point are you looking at a screen of raw
markdown. Headings, lists, quotes, tables and code blocks all work the same
way. `Esc` abandons the edit; `Cmd-Enter` saves without moving the mouse.

Select across several paragraphs and press `Delete`, or cut with `Cmd-X`, and
the selection goes — again without the markup ever appearing. This one has to
cross back over the rendering: the browser sends offsets into the text it can
*see*, and the server maps them onto the source that produced it.

```
you selected:  characters 2..6 of "a bold word"
stored as:     a **bold** word
after the cut: a  word
```

Every top-level block is rendered carrying the source lines it came from:

```html
<p data-src-start="14" data-src-end="18">…</p>
```

That is what makes "the paragraph you clicked" addressable — `SourceMappedRenderer`
in `render.py` writes it, `edits.py` maps it back, and `edit.js` is the only
part that needs to know about the mouse.

**What it does not do yet.** Two people (or two tabs) editing one document will
clobber each other; there is no version check. Annotations anchor to quoted
text, so editing the words a comment is attached to will orphan it silently.
And every save re-renders the whole set, which is a second or two — fine for a
Save, too slow to do on every keystroke.

Cutting is conservative about inline syntax. Delete a whole bolded phrase and
the stranded `**` is swept up; delete half of one and the emphasis correctly
survives. Deleting *part* of a link's text leaves the link, which is usually
right and occasionally not.

## Copying

Select prose and press `Cmd-C` and what lands on the clipboard is the
**markdown**, not the flattened text — so a paste keeps the bold, the links,
the bullets and the code. Same trick as cutting, run backwards: the browser
sends the offsets it can see and the server returns the source underneath
them.

Select a whole block and you get that block verbatim, syntax and all: `##`
before a heading, `-` before each list item, the fences around a code block.
Select part of one and the rule is *all of a construct or none of it*:

| selected in `a **bold** word` | copied         |
| ----------------------------- | -------------- |
| `bold`                        | `**bold**`     |
| `a bold`                      | `a **bold**`   |
| `bo`                          | `bo`           |

Nothing half-formed ever reaches the clipboard — no stranded `**`, no link
whose `](https://…)` has spilled out into the text. When the markdown cannot
be preserved the plain words are copied instead, which is exactly what the
browser would have done. `tests/test_copying.py` sweeps every span of several
documents asserting those are the only two outcomes.

The clipboard has to be written while the `copy` event is being handled, and
the markdown is a fetch away, so `copy.js` writes the rendered text first —
the copy the browser was about to make — and replaces it a moment later.
Everything that can go wrong therefore degrades to that plain copy rather than
to an empty clipboard: no server, no `navigator.clipboard`, a selection in a
note card or an open block editor, a request that fails.

Two things it does not preserve. A footnote reference inside a *partial*
selection comes back as the `[1]` you can see rather than the `[^1]` behind
it, since the definition is not in the selection either. And a selection that
covers the whole of a code block brings the fences with it, which is right for
pasting into a document and a nuisance for pasting into a shell.

## Refreshing

This server is not the only thing that writes to `markdown_inputs/` — an
editor, a `git pull`, another machine — and the rendered page next to it goes
stale silently. The circular arrow in the top right asks the server to render
again and swaps the result in, keeping your scroll position.

A plain browser reload would not do: it would re-serve the same stale HTML.
The rebuild has to happen server-side first, which is what the button is for.

Everything is rebuilt, not just the page you are on, since a pull can touch
several files. If a document has appeared or disappeared, the sidebar baked
into this page is wrong too, so the button falls back to a real reload rather
than leaving you with a navigation that lies. It also waits for an edit that
is still saving, instead of replacing the prose out from under it.

## Saving on its own

Set `MDWEAVE_AUTOCOMMIT` to a number of seconds and the server commits and
pushes by itself once the writing stops. Off unless it is set — committing on
someone's behalf is not a default worth assuming.

Nothing waits on it. A write arms a timer and returns; the git work happens
later on the timer's own thread, so an edit costs the same whether this is on
or off. Each further write pushes the deadline back, so a burst of nine
paragraph edits is one commit rather than nine.

Unlike **Checkpoint**, which is scoped to one article, this commits the whole
of `markdown_inputs` and `html_outputs` — which is the only way the tree
arrangement gets saved at all. A folder, a move and `.mdweave-order.json`
belong to no article, so no per-document button can ever capture them.

Failures are recorded rather than raised: the commit is already made, and the
next run pushes it, so an unreachable remote costs nothing but a delay. The
last outcome is on `/api/health`, and a failed push is mentioned once in the
page rather than left to the logs.

The two are complementary. Auto-commit means work is never only on one
machine; Checkpoint is for when you want to say *why* in the message.

## Checkpointing

**Checkpoint** in the top right commits and pushes the document you are
looking at. It asks for a message, then runs the commit with it.

The commit is scoped to that one article — its markdown, its `.ann.json`
sidecar, and its rendered page. Not `html_outputs/assets/`, which is shared and
would otherwise drag every other document's rebuild into a commit meant for
this one; and not another article that happens to be dirty at the same moment.
The pathspec is repeated on `commit` as well as `add`, so anything already
sitting in the index stays out too.

If there was nothing to commit it still pushes, since the usual reason to be in
that state is a commit that did not reach the remote last time. When git
refuses, its own words come back into the dialog and the message you typed
stays put.

The message reaches git through argv, never a shell, so it is data and cannot
turn into arguments.

## Adding comments in the browser

Select any text and a **Comment** pill appears; click it, type, and press
`Comment` (or `Cmd-Enter`). The comment is written straight into the
document's `.ann.json` sidecar and the HTML is regenerated, so a reload shows
exactly what you just made. Open a note and `Delete` removes it again.

Five swatches beside the buttons choose the colour, before or after the fact —
see [Colours](#colours).

This needs the page to be served, because a `file://` page has no API to write
to. `mdweave start` handles that.

Opened as a plain file the document is still perfectly readable — selecting
text just says it is read-only.

By default the server binds to loopback and has no authentication. Set
`MDWEAVE_PASSWORD` and every route asks for it over HTTP Basic — which is what
makes it safe to put somewhere else. Without that password `serve` refuses to
bind to anything but loopback, since an open port here means an open editor and
a git push. See [DEPLOY.md](DEPLOY.md).

Two things the server refuses, both with a message in the page: a selection it
cannot re-anchor, and one that overlaps an existing highlight. It verifies by
re-rendering the document before it writes anything, so a rejected comment
leaves no trace in the sidecar.

## How annotations work

A markdown file stays clean. Its annotations live beside it in a sidecar:

```
knowledge_base/markdown_inputs/
  system_for_bio_literature_research.md         <- untouched prose
  system_for_bio_literature_research.ann.json   <- highlights and comments
```

An annotation anchors to text by quoting it, in the style of the W3C Web
Annotation `TextQuoteSelector`:

```json
{
  "id": "kxejp",
  "kind": "comment",
  "color": "yellow",
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

## Colours

Five: **yellow**, **orange**, **green**, **pink**, **purple**. Pick one while
writing a comment — the swatches sit in the composer, next to `Comment` — and
the highlight and the card take it straight away. Open an existing note and the
same five are in its card; clicking one recolours it and saves. The swatches are
a radio group, so `Tab` reaches them as one stop and the arrow keys move
between them.

Each token is defined in one block at the top of
`mdweave/theme/annotations.css`, as four values:

```css
--hl-yellow-bg:     rgb(255 224 102 / 0.40);   /* fill behind the text   */
--hl-yellow-edge:   rgb(150 105   4);          /* underline and note pin */
--note-yellow-bg:   rgb(255 250 231);          /* card background        */
--note-yellow-ink:  rgb( 66  47   6);          /* card text              */
```

Edit those and rebuild — every yellow annotation follows. The four have to stay
legible together; `tests/test_colours.py` computes the contrast of each pair and
fails below WCAG AA, so a change that looks nice and reads badly does not get
through.

To recolour a single annotation instead, put a raw colour in its sidecar entry;
the fill, underline, pin, and card tint are all derived from it with
`color-mix()`:

```json
{ "color": "rgb(255 61 148)" }
```

That only works in a file you edit by hand. The API takes the five token names
and nothing else, because a raw colour reaches the page inside a `style`
attribute and is not something to accept from a browser.

**The palette before this one.** Sidecars written earlier say `amber`, `rose`,
`mint`, `sky`, `violet` or `slate`. Those files are left exactly as they are;
the names are resolved to the five when the page is built:

| was      | renders as |
| -------- | ---------- |
| `amber`  | yellow     |
| `rose`   | pink       |
| `mint`   | green      |
| `violet` | purple     |
| `sky`    | purple     |
| `slate`  | yellow     |

The last two lose something: `sky` and `violet` now look the same, and so do
`slate` and `amber`. Recolouring those notes in the browser is what gets the
distinction back — and writes a current token into the sidecar while it is
there. `LEGACY_COLOR_ALIASES` in `mdweave/model.py` is the map.

`knowledge_base/markdown_inputs/mdweave_style_reference.md` previews the tokens,
and still names the old six — it is a document in the contents repo, so edit it
there.

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
highlight when the text around it changes. A moved note gets a `Reset position`
action in its card. Dragging needs the server, same as commenting; over
`file://` a drag lasts only for the session.

## Commands

The two for everyday use:

```bash
mdweave start [--port N] [--no-open]      # render everything, serve it, open it
mdweave stop  [--port N]                  # shut the background server down
```

The pieces they are built from, useful on their own:

```bash
mdweave build <file.md|dir> -o <outdir>   # render; --strict fails on a lost anchor
                                          # a dir builds recursively; one file
                                          # still gets the full sidebar
mdweave serve [--port N] [--no-build]     # serve in the foreground, no browser
mdweave extract <file.md> [--in-place]    # Obsidian inline comments -> sidecar JSON
mdweave fingerprint                       # hash of the installed source
```

`serve` is what `start` puts in the background; run it directly when you want
the request log in front of you and Ctrl-C to stop it.

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
- **Interactivity**: `mdweave/assets/` holds `ui.js` (toasts, the centred
  notice, the shared server probe), `sidebar.js` (tree state, importing),
  `filetree.js` (the per-row controls and dragging rows about),
  `notes.js` (sticky notes), `annotate.js` (selecting and commenting),
  `edit.js` (editing prose in place), `copy.js` (copying it as markdown),
  `refresh.js` (re-render from disk) and `checkpoint.js` (commit and push).
  Plain ES5, no build step, copied verbatim next to the output and loaded in
  that order. `ui.adopt` is the one place that swaps new HTML into the page;
  both saving and refreshing go through it. `ui.blockRanges` is the one place
  a selection is turned into per-block offsets; cutting and copying share it.
  `notes.js` publishes `window.mdweave` — `register`, `layout`, `setOpen`,
  `remove`, `resetPosition`, `setMoveHandler` — which is how `annotate.js` adds
  a note at runtime that behaves like a rendered one.
- **A new API endpoint**: `mdweave/serve.py`. `Workspace` owns all filesystem
  access and is the path-traversal guard; handlers never join a client string
  onto a path.
- **Editing**: `mdweave/edits.py` turns "characters 4 to 12 of the block at
  lines 14-18" back into a slice of the markdown — `apply_cuts` throws that
  slice away, `extract_spans` keeps only it. The alignment between visible
  text and source is the delicate part; `rendered_text_in` resolves a block
  against the *whole* document, because a footnote reference renders
  differently without its definition and one character of drift puts every
  later offset in the wrong place.
- **Git**: `mdweave/checkpoint.py`. Every command goes through one `_git`
  helper that takes an argv list, so no caller can accidentally introduce a
  shell.
- **Process lifecycle**: `mdweave/daemon.py` — spawning the detached server,
  the health probe, the staleness check, and stopping it again. `stop` asks
  `/api/health` for the server's own pid rather than matching `ps` output,
  which would also match the shell that typed the command.

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
