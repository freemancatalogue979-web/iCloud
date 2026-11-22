# iCloud

FastAPI on port 9002. Visit the page and it opens a browser, loads icloud.com
and clicks Sign In.

## Run

```bash
pip install -r requirements.txt
python server.py
```

Then open <http://localhost:9002>

On Windows, `run.bat` does the whole thing: creates `.venv`, installs the
requirements, installs Chromium, starts the server.

Chromium is downloaded automatically on the first visit if it isn't already
there. To do it up front instead:

```bash
python -m playwright install chromium
```

## The page

`client.html` is a full-viewport `<iframe>` pointing at `/view`, with a loading
overlay on top: a spinner while the browser starts, the Apple mark once it is
up, and gone entirely the moment `/view` reports it has a real document to
show. A failure turns the mark red — no text anywhere on the page.

The mirror is DOM streaming, not screenshots. `agent.js` is injected into
every frame the browser loads; it captures each document once, then a
`MutationObserver` sends granular ops:

| Op | Meaning |
|---|---|
| `add` | a node was inserted — one op per node, with its insertion point |
| `rm` | a node was removed |
| `attr` | an attribute changed |
| `text` | a text node's data changed |
| `children` | a parent's own children rewritten, used only when bare text nodes are involved |
| `value` | live form state — `.value` / `.checked` |

Every node gets a stable id held in a WeakMap and written onto the serialised
clone as `data-rb-nid`. Ops address nodes by that id, so inserting a node at the
start of a container cannot shift its siblings' addresses. Replacing a whole
subtree's `innerHTML` instead — the obvious shortcut — was tried and is wrong
three ways over: one class toggle re-sends a whole menu of inline SVGs, it
destroys scroll position and focus inside the replaced range, and index paths
drift as soon as anything is inserted.

Mutations are batched to the paint cadence with `requestAnimationFrame` and
pushed over a WebSocket. Re-seeds are rate-limited to one per 2 s and only
happen when the mirror's structure genuinely drifts; patches are never dropped
in favour of one, because a full re-seed is visible as a flash.

`value` is a side channel rather than a mutation: typing into an `<input>`
changes `.value`, which is not a DOM mutation, so no `MutationObserver` will
ever report it. Without `input`/`change` listeners a sign-in form mirrors as
permanently empty.

Nothing is page-specific: no selectors for any site's fields, and no stored
copy of any site's markup. It mirrors whatever is loaded.

Your clicks and keystrokes in the mirror are forwarded to the real page; the
resulting DOM changes come straight back.

## The first capture, and everything after it

The mirror is built in two stages, the way the user asked for it.

**The first capture is a snapshot of the page, taken 4 seconds after Sign In
is pressed.** Not a stream of changes from the moment the page loaded — a
snapshot, once the page has settled. Right after the click the sign-in form
is still arriving: it is a navigation, and the form itself is a cross-origin
frame on `idmsa.apple.com` that has not answered yet. A snapshot taken then
is a half-built page, and the mirror never recovers from one on its own. So
the server waits (`SETTLE_SECONDS` in `server.py` — one number, easy to
change), then takes the snapshot and hands it to the mirror.

Until that snapshot is taken, **nothing is sent to the mirror at all**. A
viewer that connects early is not given a half-loaded page to start from, and
the changes that happened while the page was settling are not streamed
either: the snapshot already contains them.

**After the snapshot, the DOM is kept up to date by the mutation stream.**
That part is unchanged — granular ops, addressed by stable node ids — and it
is what the user sees from then on.

Two things about that ordering are worth stating, because both were wrong
before:

- A change made *while* a capture is being taken must not be sent before the
  document it belongs to, or the mirror drops it — it has no document to
  apply it to yet. The agent holds the batch until the capture is out.
- A capture asked for *while* another capture is running must not be lost.
  The running one read the document before the request, so it may already be
  out of date. The request is repeated once the running one finishes;
  dropping it left the mirror stale with nothing to show for it.

The sign-in form may open in a **tab of its own**. When it does, that tab is
the page the user is looking at, so it is the page the mirror follows, and
the tab that was open before is ignored.

## How the capture works

Modelled on the [SingleFile](https://github.com/gildas-lormeau/singlefile)
extension (`single-file-core`'s `content-frame-tree.js`), because it solves the
two problems a plain `outerHTML` dump does not:

**Frames.** The sign-in form is in a cross-origin iframe on `idmsa.apple.com`,
which the parent document cannot read. So the top frame mints a positional id
for every frame element — parent's id plus its index, `0`, `0.1`, `0.1.0` — and
`postMessage`s an init request into each frame's `contentWindow`. That works
cross-origin. Each frame serialises itself and replies to `event.source`, and
the parent matches the reply to its element by the id it already wrote on it.
No URL matching. Frames that never answer time out after 1.2 s.

**Shadow roots.** Web components like `<ui-button>` keep their visible markup
in a shadow root, which `outerHTML` does not contain. They are serialised as
declarative `<template shadowrootmode="open">`, which Chromium parses into a
live shadow root with no scripting in the mirror. Shadow roots are also
observed, so components that re-render stay in sync.

`<base>` is removed, as SingleFile's `cleanupPage()` does: a copied page does
not have the original's base URI, and a leftover `<base href="/">` silently
points every relative URL at localhost.

**Resolved styles travel as a fallback stylesheet, not as inline styles.**
The mirror's styling would otherwise depend entirely on `/res` fetching the
site's stylesheets, and that fails silently the moment a sheet is CORS-blocked,
session-scoped or redirected — leaving bare markup with no explanation. So every
element's computed style is captured too.

It is delivered as `:where([data-rb-nid="n7"]){…}` rules — **zero
specificity** — in a stylesheet of the mirror's own, rather than as an inline
`style` attribute. That distinction is the whole thing. An inline style beats
every stylesheet rule, so a mirror built on inline styles is frozen: the page
toggles a class, the mirror takes the attribute op, and the snapshot's colour
and size still win, so the page looks correct and stale. A `:where()` rule
loses to every rule the page's own CSS has, so the page's CSS decides and the
snapshot only fills in what could not be loaded.

It also leaves the element's own `style` attribute alone. The snapshot used to
overwrite it.

A curated list of ~85 properties is used rather than all 300+: PhantomStream
found iterating everything cost 45 s on a heavy page. The values that mean
"nothing was set" are dropped — but the list of them is read off the browser's
own defaults rather than written by hand, because a hand-written list is wrong
in ways nobody can see: `flex-grow`, `flex-shrink`, `order`, `outline-width`
and four border colours were leaking onto every element on the page, which is
most of the capture and says nothing. An element's width and height are not
captured when it is a flex or grid item: those numbers are the layout's
answer, and pinning them changes the base size and lays the row out
differently. `url()`s inside the resolved text are proxied like any other.

**Canvases keep their picture.** A `<canvas>` holds pixels, not markup, so a
clone of one is an empty box. The capture ships the picture as a data URL and
the mirror draws it back. In the mirror's sandbox — no `allow-scripts`, so the
captured page's scripts never run — Chromium hides a canvas's intrinsic size,
which leaves an inline canvas with no box at all; the mirror makes it an
inline-block so the captured width and height apply.

**A shadow root is addressable.** Mutations inside one used to be dropped
silently, because a shadow root is not an element and had no id to address. It
is addressed now by the host's id plus `/shadow`, which both sides can work
out from the ids already in the document. A shadow root's snapshot goes into
the shadow's own last `<style>` element if it has one, rather than into a new
element: an extra element in a shadow tree is not invisible, because
`:first-child` and `:last-child` stop matching what they matched.

**Light DOM shown through a slot is kept.** The light children of a shadow
host are not rendered once a shadow root exists — unless the shadow root has a
slot, in which case the light DOM is what the slot shows. Dropping it then
blanks content the real page displays.

**Resources** are the other deliberate divergence. SingleFile inlines everything
as `data:` URIs because it produces a file; this produces a live stream, so
URLs are rewritten to `/res?url=<original>` and the server fetches them with
`ctx.request.get`, which carries the site's cookies and gets around CORS on
fonts. CSS bodies have their own `url()`s rewritten against the sheet's origin.
`href` is split by element — proxied on `<link>`, only absolutised on `<a>`,
since an anchor names a destination and not a resource. Scripts and `on*`
handlers are dropped, and frames are sandboxed to `allow-same-origin` only.

The mutation model follows [PhantomStream](https://github.com/fullselfbrowsing/PhantomStream),
which reaches the same conclusions from the same problem — including its
documented bug where gating the childList branch on element nodes silently
drops `textContent =` edits, reproduced and fixed here as the `children` op.

## Console output

The console reports what the mirror is actually doing, because a blank mirror and
a stale one need different fixes and look identical from the outside:

```
[icloud] capture: 2 frame(s) [0:292KB, 0.0:39KB]
[icloud] stream: 41 updates, 268 ops, 0 resyncs in 10s
```

The capture line lists every frame by windowId with its size, so a missing
cross-origin frame shows up as one entry instead of two. The stream line is a
throttled tally - updates, ops and resyncs - so "nothing is moving" is
distinguishable from "moving but wrong". A rising resync count means the mirror
keeps losing sync with the page.

## Tests

```bash
npm install   # once, pulls jsdom
npm test
```

`test-*.js` runs `agent.js` and `view.html` inside jsdom: serialisation, the
frame-tree handshake over postMessage, inline SVG and web components, and a
differential test that captures in one document, rebuilds in a second, and
applies the ops using the applier lifted out of the shipped `view.html` rather
than a copy of it. `test_telemetry.py` and `test_res_cache.py` cover the server.

`test_fidelity.py` is the one that asks whether the mirror looks like the
page. It captures a realistic page with the real agent, rebuilds it in a
second real browser through the real `view.html`, and compares the two:
which elements survived, what the computed styles are, where the boxes ended
up, what the form controls are holding, whether the canvas still has its
picture, and whether a class the live page toggles changes the mirror. It is
the suite that would have caught the frozen mirror, and it is the reason the
other suites being green did not mean the mirror was right.

**jsdom is not a browser.** It has no layout, no style resolution, no
shadow-DOM parser, and it is more permissive than Chromium about how built-in
methods are called. Two suites therefore run against a real Chromium and skip
cleanly when none is installed:

- `test_real_browser.py` — capture, declarative shadow DOM, computed styles
  actually applied in the mirror, SVG namespaces, both mutation shapes
  (characterData and childList carrying text nodes), a mutation inside a
  runtime shadow root, and refusal of a mis-indexed `text` op.
- `test_e2e.py` — stands up a throwaway HTTP origin playing the role of
  icloud.com and points the server at a real Chromium, then checks the Sign In
  locator really clicks the user's `<ui-button>`, that `/res` does a real
  `ctx.request.get`, that `url()`s in a real stylesheet are rewritten (quoted
  and unquoted), that the cache serves rewritten bytes, that `<base>` does not
  leak into the mirror, and that the mirrored `url()` serves the real image.
- `test_fidelity.py` — captures a realistic page with the real agent, rebuilds
  it in a second real browser through the real `view.html`, and compares the
  two: elements, computed styles, geometry, form state, the canvas's picture,
  the shadow root's content, and a class the live page toggles.
- `test_server_flow.py` — runs `start()` itself, the function `/` triggers.
  Also covers the settle window (nothing reaches the mirror before the
  snapshot is taken, and the snapshot is of the page *after* the click) and
  the sign-in form opening a tab of its own.
  Substitutes only a headless context (a visible window needs a display) and a
  route handler answering `icloud.com` (no outbound access here);
  `install_agent`, `on_emit`, `broadcast`, `/ws`, `/click`, `/keys` and
  `request_resync` are the shipped code. It verifies the run reaches `done`,
  that Sign In was clicked in the page, that the capture reaches a real browser
  client over a real WebSocket, that a live change arrives as granular ops
  addressed by ids from that capture, that a `resync` request yields a fresh
  capture, that `/click` lands on the right element at the right coordinates,
  and that `/keys` types into the focused field.

What remains unverified, and why: the real `https://www.icloud.com/` load and
the real visible window. This sandbox has no route to Apple and no display
server, so those two are exercised only through the substitutions above.

To run them, point `CHROME_PATH` at any Chromium:

```bash
CHROME_PATH="/path/to/chrome" npm test
```

After `python -m playwright install chromium` they are found automatically.

The suites are not decorative. Each was mutation-checked rather than trusted:
putting the snapshot back on the element as an inline style, reading the
browser's defaults from a hand-written list, capturing the canvas's pixels or
not, addressing a shadow root or not, dropping the snapshot from an op, or
leaving the light DOM out of a shadow host each makes the matching assertion
in `test_fidelity.py` fail.
removing the `shadowrootmode` attribute, the text-node childList guard, the CSS
`url()` rewrite, the `<base>` filter, the mis-indexed-`text` refusal, the
`broadcast` of mutation ops, or the coordinates in `/click` each makes the
matching assertion fail. Where a mutation check did *not* fail, the test was
wrong and was fixed. One is worth recording: editing a text node with
`li.firstChild.nodeValue = x` emits a **characterData** record, while
`li.textContent = x` emits a **childList** record whose added and removed nodes
are text nodes. Those are different code paths, and only the second one was
broken when the mirror stopped updating - so a suite that tests the first tells
you nothing about the second. Both are now pinned separately.

**A correction worth recording.** `test-browser-semantics.js` was written on the
claim that calling `requestAnimationFrame` with no receiver throws `Illegal
invocation` in Chromium. Measured against real Chromium 153, it does not — the
unbound call is accepted. That suite still passes and is still worth keeping
(calling a `Window` method as a method of `Window` is correct regardless, and it
pins that the scheduler re-arms), but it does not evidence a bug that was fixed.
A stub is not a browser, and neither is an assumption about one.

Everything else here was found by running code rather than reasoning about it:
text nodes being deleted from patches, `ShadowRoot.cloneNode()` throwing by
spec, a lockstep walk iterating live `childNodes`, `event.source` being null,
and the declarative-shadow-DOM template shifting an ordered id sidecar.

## Files

- `server.py` — FastAPI: `/` `/start` `/status` `/view` `/res` `/ws` `/click` `/keys` `/key` `/shot.png`
- `agent.js` — injected into every frame; capture + MutationObserver → server
- `client.html` — the one page; calls `/start` on load, then iframes `/view`
- `view.html` — the mirror: seeds the frames, applies patches
- `run.bat` — Windows launcher

## Note

The app clicks Sign In and then stops. You type your Apple ID and two-factor
code yourself in the browser window that opened — it never touches your
password.
