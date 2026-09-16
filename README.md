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

# optional: the browser suite, which needs a real browser to drive
uv pip install --python .venv/bin/python -e '.[browser]'
.venv/bin/playwright install chromium      # ~190MB, into ~/.cache/ms-playwright

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
a document that has gone. Dragging is the only thing that moves a document
now; the pencil names the row instead — see below.

Order is not a property of the filesystem, so it is written down:
`markdown_inputs/.mdweave-order.json` maps a folder path — `""` for the root —
to the order of its children. Anything not listed falls back to the old sort,
folders first and then alphabetical, *after* the names that are listed; a
document someone else adds appears at the end rather than in the middle of an
arrangement it was never part of. Delete the file and the tree goes back to
being sorted.

**Folders.** A folder row is draggable like a document row: dropping it
between two rows reorders it, dropping it onto another folder nests it inside.
A folder cannot be dropped into itself or anything beneath it.

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
keeps whatever title its own `# heading` gives it. That is a default rather
than a verdict — the pencil overrides any of it, see **Naming a row** below —
and `humanize` in `mdweave/tree.py` is where the rule itself lives.

**Folders.** The `+`-in-a-folder button makes one: on a folder row it nests
inside, on the strip below the tree it lands at the top level — which is the
only way to get a first folder in a knowledge base that has none. An empty
folder is still drawn, because a folder you cannot see is one you cannot drop
anything into. Renaming a folder carries every document under it, and so
changes their ids; deleting one refuses unless it is empty or the request says
`recursive`.

**Naming a row.** The pencil edits the *caption*, not the filename. The box
opens on what you can see — `Ome zarr layout planner` — and whatever you type
is stored literally, in `markdown_inputs/.mdweave-labels.json`, keyed by
document id or folder path.

That file holds only the exceptions. `humanize` still answers for everything
not in it, so nothing has to be seeded and a caption typed back to what the
heuristic would produce is removed rather than written. The heuristic is a
good rule — `system_for_bio_literature_research` really is "System for bio
literature research" — but it flattens every hyphen and underscore to a space
and lowercases the rest, and it cannot know when the punctuation mattered.
`Ome-Zarr`, or a date like `AI Career Research 2026-09-06`, are only reachable
this way.

The file keeps its name, which is the point: the id is the path, and ids are
what links, pages, sidecars and annotations all hang off. A caption can be
anything, including a slash, because it never becomes one. Labels follow a
document or folder through a move — everything underneath it too — and are
forgotten when the row is deleted.

**A new name is a path, though.** The prompts ask for one name, so a `/` in
what you type is escaped rather than obeyed: `Research/Papers` makes a single
folder, `Research_Papers`. Where it goes is the row the button hangs off, and
never something you typed. Typing a slash used to answer `unknown folder:
'Research'` — the server reads its `path` field as a path, which is right for a
drag, where both halves name rows that exist, and wrong for a typed name, where
the reader never named a parent at all.

**Colours.** The panel is `#D1C7B7` and the reading pane `#F2EFE4`, as
`--sidebar-bg` and `--paper` in `mdweave/theme/base.css`. Every label,
chevron and icon in the panel is flat black (`--sidebar-ink`): the page's
three-step ink ramp was de-emphasising against near-white, and on `#D1C7B7`
its faint end reads muddy rather than quiet, so hierarchy here is weight and
indentation instead. The row for the document on screen is painted in
`--paper`, the pane's own colour, so the panel reads as having a piece cut out
of it — it is the only light shape on a darker panel and needs no accent to be
found. The panel carries no rule down its right-hand edge, so that row runs
into the prose with no seam; a child cannot paint over an ancestor's border,
so the border is what had to go. They are deliberately
not `--surface`, which is still white: that token is also the colour of the
text on a dark chip and the fill of the floating re-open button, neither of
which wants warm paper. Code blocks, table heads and hairlines were warmed to
match, or they read as patches of a different page. The dark scheme keeps its
own two tones — warm paper is a light-scheme idea.

**Two writers, one branch.** This machine and the deployed container both
commit to `main`, so a push can lose a race — and once it does, `git` answers
`non-fast-forward` to that attempt and to every attempt after it, because
nothing about the situation changes on its own. A whole session's work sits on
one machine looking saved. So a rejected push now merges rather than giving up:
generated pages conflict every time, since each side rebuilds them from its own
prose, and they are resolved by taking ours and re-rendering over the prose
that just arrived. A conflict in `markdown_inputs` is writing, and only the
person who wrote it knows what was meant, so the merge is aborted and the
checkout left exactly as it was found.

`mdweave reconcile` is the same thing on demand, and the container runs it at
boot — where the `pull --ff-only` it replaced would abort on a divergence and
then serve a stale commit for the rest of the deployment.

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

## Links

A bare URL in the prose becomes a link. `https://example.com/x` is clickable
without being wrapped in `<>` or `[](…)`, which is most of what a pasted
research note contains — 321 of them across nine documents when this was
turned on.

**A scheme is required**, and that is the whole of the rule. linkify's fuzzy
matching takes anything ending in a known TLD, and `.md` is Moldova: "see
README.md for details" came out as a link to a domain, in a knowledge base
whose prose is largely about `.md` files. Bare email addresses go the same way.
So `www.example.org` is left alone too — there is no switch for `www` on its
own — and writing `https://` is how you say you meant an address.

Code is untouched, in spans and in blocks alike: a URL there is a string being
shown, not an address being offered. `javascript:` and friends are refused by
markdown-it before they can reach an `href`, and a test pins that so a future
change to the parser options cannot quietly undo it.

## Editing the prose in the browser

**Double click** a paragraph, or press and hold it, and it becomes a small
box holding *its own* markdown — just that block. Click away and it turns back
into rendered prose.

It used to be a single click, and that was wrong: a click is what you do on
the way to almost everything else here — placing a cursor, dismissing a note,
starting a selection that ends up empty because the drag was a pixel wide.
Every one of those opened an editor nobody asked for, and the way out was to
click away again. A double click is never accidental, and a long press is the
same intent with a finger. A press that moves more than a few pixels is a
selection and cancels the hold, because selecting is the gesture this most has
to stay out of the way of.

A document with nothing in it but its heading gets a **Start writing…** box
underneath, which opens on a single click — it is a control whose only purpose
is to be pressed, so a click there cannot mean anything else. Before it, the
only way into an empty document was to open the heading and type past it,
which nobody guesses. The rest of
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

## Co-writing with Claude

The **Claude** button in the top right hands this document to a Claude session
and waits. It asks what you want done, runs a turn, and swaps the rewritten
prose into the page — without a reload, like every other edit here.

The session is not in the browser and not in the container. It is on a
devserver, in a checkout of this knowledge base, with a shell and everything
else a session normally has. That machine has no public address, so the
deployed site can never call it; the traffic all goes the other way.

```
browser ──POST /api/agent/jobs──▶ the site
                                     ▲  │  POST /api/agent/claim
                                     │  │   (held open until there is work)
                                     │  ▼
                                     │  mdweave agent, on the devserver
                                     │      claude -p --resume <session>
                                     │      edits markdown_inputs/
                                     │      git push
                                     │
                                POST …/events, …/done, …/reconcile
```

**One conversation per document.** Which session owns a document is recorded
beside it, in `<doc>.session.json` — the same convention `.ann.json` uses, and
it travels the same way: a move renames it, a delete removes it, a checkpoint
carries it. Each press resumes that conversation rather than starting a new
one, so *"tighten the section you just added"* means something.

Resuming reuses the same session id, which is what makes the last part work:

```bash
claude --resume $(jq -r .session_id markdown_inputs/metabridge-design-review.session.json)
```

That is the *same* conversation the buttons are driving, open in your terminal.
The button's tooltip prints the command. Nothing is being mirrored or replayed
— there is one session, and two ways to talk to it.

**The buttons are yours.** An action is a name, a label and a prompt, and they
live in `markdown_inputs/.mdweave-agent.json`, beside `.mdweave-theme.json` and
for the same reason: both machines already have the knowledge base, so adding a
button is a commit to your notes rather than a redeploy of the tool.
`mdweave actions --write-default` drops the shipped three in to be edited, and
`mdweave actions` lists what a checkout currently offers. A missing or mangled
file costs a button and never a document.

**Getting the result back.** Claude edits the markdown on the devserver, so
four things have to happen before you can see it: re-render, commit, push, and
— the one that is easy to leave out — tell the site to pull. Without that last
step everything reports success and the page keeps serving the prose it booted
with, because the container only ever pulled at startup. `/api/agent/reconcile`
is that step, and it is the same merge `mdweave reconcile` does.

**Why it polls rather than streams.** The obvious design is a connection held
open from the devserver to the site. Railway closes an HTTP request after five
minutes of silence and caps every one of them at fifteen, so "held open" would
mean reconnecting four times an hour whatever it was called — and a stream that
dies without a `FIN` leaves the reader blocked on a socket that will never
speak again, believing it is connected. So the runner asks, and the *site*
holds the question open for twenty-five seconds before answering "nothing".
A press still reaches the devserver the instant it happens, and every cycle is
a whole request that either completed or timed out. There is no third state to
get wrong.

The browser's end of it *is* SSE, because there `EventSource` reconnects on its
own and says where it got to, and the replay buffer that makes `Last-Event-ID`
mean anything is a few lines in the broker.

**Nothing is retried.** A job whose runner goes quiet is marked `stalled` and
left there. At-least-once delivery is the usual default and it is the wrong one
here: the work is a language model editing files, so a redelivery is not a
retry, it is a second and different edit. Press the button again if you meant
to.

**No new secret, and one check doing the work.** Your existing
`MDWEAVE_PASSWORD` gates the page, and that is the whole of the
authentication. There is no separate key for the runner and none for you.

The thing that makes that safe is one line: every request body must declare
`Content-Type: application/json`. Without it, a hostile page you happened to
visit could queue a job — browsers attach HTTP Basic credentials to *any*
request to an origin they have them for, including one begun by somebody
else's site, and there is no SameSite for Basic auth the way there is for
cookies. A cross-site form cannot send that content type, and a script that
tries forces a preflight this server does not answer. **That check is a
security boundary, not tidiness.** Two credentials used to back it up and
neither does now; removing it reopens the hole on its own.

**So the page password is an execute-code-here credential** whenever a runner
is connected. Anyone who can read your notes can also make a Claude session
run on the devserver. That is the trade: one secret instead of three, and the
reading password carries the weight of all of it. Set it to something long.
Run the runner with `--permission-mode acceptEdits` if you would rather the
sessions could write prose and not run commands.

The runner's own endpoints — claim, events, done, reconcile — take no
credential at all. Collecting a job and reporting on it lead nowhere; what
starts something is the POST above. The cost is that a stranger who found the
URL could claim your jobs and read the instruction text, which for a personal
knowledge base is a fair price for having nothing to copy to the devserver and
nothing to restore after a reboot.

There were briefly two secrets here, `MDWEAVE_AGENT_TOKEN` for the runner and
`MDWEAVE_AGENT_PASSWORD` (later `MDWEAVE_OPERATOR_KEY`) for the browser. Both
are gone. The first guarded nothing that led to execution; the second was
made redundant by the content-type check, and an authentication path that is
not load-bearing is a thing to maintain and explain rather than a defence.

## Adding comments in the browser

Select any text and a menu appears with two rows — **Comment** and
**Highlight** — each showing the six slots of the active scheme. The colour *is* the button, so
either is one click rather than "make it, then recolour it". A highlight has
nothing to type and so skips the composer entirely; a comment opens one,
already in the colour you picked.

Pressing a colour means *make this selection that colour*, whatever is already
underneath, with no exceptions — so re-colouring a highlight, or painting over
a patch of mixed ones, both leave a single clean highlight.

Removing one is the **eraser** at the end of the Highlight row. It used to be
a second press of the colour the text already was, which made one gesture mean
two things depending on state the reader could not reliably see: press a
colour on text that happened to be it and the highlight vanished. A press
paints; the eraser erases.

Text carrying a **comment** is never absorbed — its highlight is the handle for
a thread, and no colour press should mean *delete that*. Recolour it from its
own card instead. The comment is written straight into the
document's `.ann.json` sidecar and the HTML is regenerated, so a reload shows
exactly what you just made. Open a note and `Delete` removes it again.

The swatches beside the buttons choose the colour, before or after the fact —
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

## Testing the browser

Most of the suite reasons about the browser code without running it: a
function is lifted out of its file and driven under `node` against a stub
DOM, or the source is read and asserted about. That is fast, and it is enough
for logic — but it is not enough for anything about real events, real layout,
or the wiring between them, and three bugs that reached the reader proved it.
Dragging a folder threw `Cannot set properties of null` while every
source-level test around it passed. Creating a folder answered `unknown
folder` from a server that was self-consistent right up until something used
it. A slash in a typed name quietly made a hierarchy.

`tests/test_browser.py` drives a real page: real clicks, real drags, and
`pageerror` wired up so an uncaught exception in the panel fails the test
rather than sitting in a console nobody reads. It also asserts on
`getComputedStyle` rather than on the stylesheet, because a token can be
perfectly correct and still be overridden by something later.

It found one on its first run: an **empty** folder could not be reordered.
`children_of` derived the sidebar's rows from document ids, and an empty
folder has none, so the order endpoint dropped its name — while a folder with
a document in it moved perfectly well, which is why every existing test
missed it.

The file skips, rather than fails, when the browser is not installed, so
`pytest tests/` still works on a checkout that never ran `playwright install`.

## Colours

Six, and which six is up to you. The gear beside **Documents** opens a
floating window — moved by its title bar, not a modal, because the point is to
watch the prose change while you pick.

A scheme is the left panel, the document background, and six highlight
colours. Six is fixed: it is the width of the picker, the width of the
selection menu, and the number a reader can tell apart at a glance. Only the
*fill* is chosen; the pin, the card and the card's text are derived from it by
hue, and darkened until they clear WCAG AA — asking anyone to pick four
colours that stay legible together, six times over, is asking them to do
arithmetic. A fill too dark for the text on it is reported, never refused;
the reader picked it, and a veto is someone else's taste in an error message.

**Preview** paints the page with exactly the custom properties Apply would
write, into a single `<style>` element. Nothing reaches the disk until Apply,
and closing the window takes it back. The browser therefore derives the same
four values as the server does — two implementations of one piece of
arithmetic, paid for on purpose so dragging a picker does not mean a round
trip per keystroke, and on the condition that a test compares them, which is
`test_the_preview_paints_exactly_what_apply_would_write`.

### A colour is a slot

This is the idea the rest of it hangs on. An annotation does not store a hue.
It stores **which of the six** — `"color": 3` — and the scheme says what three
looks like. Two consequences, both of them the reason:

- **Switching schemes** moves every highlight to the same position in the new
  palette and rewrites nothing. Storing a hue instead would mean renaming
  yellow silently restyled every note that mentioned it, and switching to a
  scheme with no yellow left those notes resolving to nothing.
- **Reordering the six**, by dragging the swatches, is the opposite. The
  colours move, so every annotation is renumbered to *stay the colour it was*:
  drag the fifth colour to the front and everything wearing 5 becomes 1. The
  page looks identical, which is the point — you are arranging the palette,
  not restyling your notes. It is the one edit here that touches a sidecar.

  A reorder contributes nothing visible, on top of whatever else the same
  Apply does. Rearrange the scheme you are using and nothing changes at all;
  rearrange one you are switching to and you get exactly the switch you would
  have got without the drag.

Slots are positional in the markup too: `hl--c3`, never `hl--yellow`, because
a hue in a class name is a lie the moment the reader recolours it. The API
sends `color_token` alongside the stored `color` for exactly this: the file
may say `3`, or `amber`, or a raw CSS colour, and turning that into a class is
the server's job once rather than the browser's again. Handing the browser the
raw value made it write `hl--3` where the renderer writes `hl--c3` — the
highlight was placed and matched no rule, so it looked absent until a reload.

Schemes live in `markdown_inputs/.mdweave-theme.json`, beside the prose rather
than beside the tool: they are the reader's, and they follow the knowledge
base to the next machine. A missing or mangled file falls back to the palette
that shipped, so a broken dotfile costs a colour and never a document.

**The palette before this one.** Sidecars in the wild say `pink`, `yellow`,
`amber`, `slate` and the rest. Every one of them resolves to the slot that
colour occupied at the time, on the way to the page, and the file is left
exactly as it is. `LEGACY_SLOTS` in `mdweave/scheme.py` is the map.

A single annotation can still opt out of the palette entirely: put a raw CSS
colour in its sidecar entry and the fill, pin and card tint are derived from
it with `color-mix()`. That is a hand-edit only — the API refuses anything
that is not a slot, because a raw colour reaches the page inside a `style`
attribute and is not something to take from a browser.

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

And the two for driving Claude sessions from the deployed site:

```bash
mdweave agent --remote https://<app>.up.railway.app
                                          # take jobs from the site and run them here
mdweave actions [--write-default]         # what buttons this knowledge base offers
```

`agent` is the long-running one: it belongs on the machine with the checkouts
and the sessions, not in the container. `--once` takes a single job and exits,
which is the way to try it without leaving anything running.

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
- **A new agent button**: `markdown_inputs/.mdweave-agent.json` in the
  knowledge base — no code, and no deploy. `mdweave/agent/actions.py` holds the
  shipped set and the fallback.
- **The bridge**: `mdweave/agent/`. `protocol.py` is the shapes both ends
  agree on, `broker.py` the queue and its leases on the container,
  `daemon.py` the loop on the devserver, and `runner.py` the one place that
  knows what `claude --output-format stream-json` emits.
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
