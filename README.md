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

## The shape of it

There are three halves, and they are separate files on purpose:

| File | Where it runs | What it does |
|---|---|---|
| `agent.js` | in the real browser, in every frame | captures the page and tells the server when it changes |
| `server.py` | the server | holds the capture, serves it, forwards clicks and keys |
| `mirror.js` | in the mirror page | shows the capture, sends clicks and keys back |

## The mirror is the captured page

The mirror is not a picture of the page and it is not a replica of it. **It is
the captured page, loaded as a document of its own.**

The server writes the capture out as `1.html` — the page as the real browser
is showing it, with the site's own markup, its own stylesheets, its own
shadow roots. `client.html` loads that file in an `<iframe>`, laid out at the
size the page was captured at and scaled down to fit the window.

That is the whole point, and it is why the earlier design was thrown away. A
mirror built from *patches* has to address every patch to exactly the right
node. Nodes come and go, ids have to survive a page that keeps moving them,
and a patch that lands on the wrong node corrupts the mirror quietly instead
of failing. **A capture cannot be addressed wrongly — it is the page.** So a
change is answered with a fresh capture of the whole document, and the mirror
rebuilds itself from it.

The cost is sending the page again. On a page that is mostly still — a sign-in
form — that is almost nothing, and a capture that comes out identical is not
sent at all.

So the flow is:

1. The page changes.
2. `agent.js` notices (a `MutationObserver` is a *signal*, not a source of
   patches) and waits 250 ms for the dust to settle.
3. It captures the whole document again and sends it.
4. The server relays it; `mirror.js` rebuilds the page from it.

`mirror.js` re-parses the captured markup rather than importing it, because
that is what gives a declarative shadow root its shadow root. `innerHTML` does
not; `setHTMLUnsafe()` does.

**Form state stays a side channel.** Typing changes `.value`, which is not a
DOM change, so it never causes a capture, and it is far too frequent to want
one. It arrives as a small message of its own and is put straight onto the
element.

Nothing is page-specific: no selectors for any site's fields, and no stored
copy of any site's markup. It mirrors whatever is loaded.

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

**After the snapshot, every change is answered with a fresh capture.** The
capture is the truth; the mirror shows it.

Two things about the ordering are worth stating, because both were wrong
before:

- A change made *while* a capture is being taken must not be sent before the
  document it belongs to. The agent holds the change until the capture is out.
- A capture asked for *while* another capture is running must not be lost.
  The running one read the document before the request, so it may already be
  out of date. The request is repeated once the running one finishes;
  dropping it left the mirror stale with nothing to show for it.

The sign-in form may open in a **tab of its own**. When it does, that tab is
the page the user is looking at, so it is the page the mirror follows, and
the tab that was open before is ignored. Only the mirrored page is allowed to
speak for the mirror — the check that enforces it had a bug worth recording:
Playwright hands a binding its source as a *dict* now, and it used to be an
object, so `getattr(source, "page", None)` answered `None` every time and
every tab in the context was allowed to overwrite the capture.

## Clicks and keys

Your clicks and keystrokes in the mirror are forwarded to the real page.

**The mirror is laid out at the size the page was captured, and scaled to
fit.** The browser window is opened maximised, so the real page is as wide as
the screen - often wider than the window the mirror is shown in. If the
mirror reflowed the capture to its own width, two things would be wrong at
once: the page would be cut off where it did not fit, and a click would land
somewhere other than where it was clicked. The capture carries the size it
was taken at, the mirror lays it out at that size and scales it down, and a
click is sent as a fraction of the captured page. The server scales that
fraction to the window it is actually driving - not to a size guessed at
build time, which put the click in the wrong place on any window that was
not 1280x800.

**A click inside a frame is mapped out of the frame first.** The sign-in
form is a frame, and a frame reports a click in its own coordinates. Each
frame's offset is added as the frames are climbed out of, so the click ends
up in the captured page's coordinates - the only ones the server can use.
Without it, a click in the sign-in form landed somewhere else entirely.

**A frame cannot send a capture of its own.** The mirror addresses frames by
the id their parent gave them, and only the top frame's document is the one
the server hears. So a frame that changes says so, over `postMessage`, and
the top frame captures everything - itself and every frame under it. That is
the only path a change inside the cross-origin sign-in form can take, and it
works across the origin boundary because `postMessage` does.

**Input goes out one request at a time, in the order it was made.** A
keystroke is one character and it is sent as its own request, so two
requests in flight at once reach the browser in whatever order the network
delivers them, and the text comes out scrambled - the user types "hello"
and the field says "hleol". The mirror therefore sends one input request at
a time, and the server applies input to the browser one action at a time,
so a click and the characters around it cannot overtake each other.

**A frame's document is updated in place, never navigated.** Putting a
capture into a frame by setting `srcdoc` navigates the frame: the old
document is thrown away and a new one is built over several milliseconds,
and for those milliseconds there is no document in the frame that anything
is listening on. A keystroke that lands in that moment is lost - the
mirror showed `someone@iclud.com` for `someone@icloud.com`, and it lost
the same character every time, because the capture always arrives in the
same place in the typing. So the capture is written into the frame's
document with `document.open()` and `document.write()`. The document stays
the same document, so there is never a moment without one;
`document.open()` does take the document's listeners with it, so the input
is wired again straight afterwards, and because that is all in one go there
is no moment in between either.

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

**The capture comes in three parts.** The whole document, the head on its
own, and the body on its own. The mirror is the captured page, so it has to
put the parts back the way a parser would — and a parser is what gives a
declarative shadow root its shadow. Cutting the parts out of the finished
string with a regular expression is how the shadow content used to go
missing.

**Resolved styles travel as a fallback stylesheet, not as inline styles.**
The mirror's styling would otherwise depend entirely on `/res` fetching the
site's stylesheets, and that fails silently the moment a sheet is CORS-blocked,
session-scoped or redirected — leaving bare markup with no explanation. So every
element's computed style is captured too.

It is delivered as `:where([data-rb-nid="n7"]){…}` rules — **zero
specificity** — in a stylesheet of the mirror's own, rather than as an inline
`style` attribute. That distinction is the whole thing. An inline style beats
every stylesheet rule, so a mirror built on inline styles is frozen: the page
toggles a class, the mirror takes the new capture, and the snapshot's colour
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
the mirror draws it back. Chromium hides a canvas's intrinsic size in a
document that is not allowed to run scripts, which leaves an inline canvas
with no box at all; the mirror makes it an inline-block so the captured width
and height apply.

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

## Console output

The console reports what the mirror is actually doing, because a blank mirror and
a stale one need different fixes and look identical from the outside:

```
[icloud] capture: 2 frame(s) [0:292KB, 0.0:39KB]
[icloud] stream: 41 captures, 268 values, 0 resyncs in 10s
```

The capture line lists every frame by windowId with its size, so a missing
cross-origin frame shows up as one entry instead of two. The stream line is a
throttled tally - captures, values and resyncs - so "nothing is moving" is
distinguishable from "moving but wrong". A rising resync count means the mirror
keeps losing sync with the page.

## Tests

```bash
npm install   # once, pulls jsdom
npm test
```

`test-*.js` runs `agent.js` and `mirror.js` inside jsdom: serialisation, the
frame-tree handshake over postMessage, inline SVG and web components, and a
differential test that captures in one document, rebuilds in a second with the
shipped `mirror.js`, and checks that the two agree after every change.
`test_telemetry.py` and `test_res_cache.py` cover the server.

`test_fidelity.py` is the one that asks whether the mirror looks like the
page. It captures a realistic page with the real agent, rebuilds it in a
second real browser through the real `mirror.js`, and compares the two:
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
  runtime shadow root, and a rebuild that replaces the page rather than
  adding to it.
- `test_e2e.py` — stands up a throwaway HTTP origin playing the role of
  icloud.com and points the server at a real Chromium, then checks the Sign In
  locator really clicks the user's `<ui-button>`, that `/res` does a real
  `ctx.request.get`, that `url()`s in a real stylesheet are rewritten (quoted
  and unquoted), that the cache serves rewritten bytes, that `<base>` does not
  leak into the mirror, and that the mirrored `url()` serves the real image.
- `test_fidelity.py` — captures a realistic page with the real agent, rebuilds
  it in a second real browser through the real `mirror.js`, and compares the
  two: elements, computed styles, geometry, form state, the canvas's picture,
  the shadow root's content, and a class the live page toggles.
- `test_cross_origin.py` — two real origins, `www.icloud.com` and
  `idmsa.apple.com`, in a real Chromium. The sign-in form is a frame on the
  second one. It checks that the form is in the capture, that the real client
  page loads the captured page and shows the form in a frame, that a click in
  the mirror lands on the field inside that frame, that what is typed in the
  mirror arrives in it, and that the mirror lays the page out at the size it
  was captured rather than reflowing it.
- `test_server_flow.py` — runs `start()` itself, the function `/` triggers.
  Also covers the settle window (nothing reaches the mirror before the
  snapshot is taken, and the snapshot is of the page *after* the click), the
  external page (`/1.html` is the capture, with the engine in it), and the
  sign-in form opening a tab of its own.
  Substitutes only a headless context (a visible window needs a display) and a
  route handler answering `icloud.com` (no outbound access here);
  `install_agent`, `on_emit`, `broadcast`, `/ws`, `/click`, `/keys` and
  `request_resync` are the shipped code. It verifies the run reaches `done`,
  that Sign In was clicked in the page, that the capture reaches a real browser
  client over a real WebSocket, that a live change arrives as a fresh capture
  carrying the change, that a `resync` request yields a fresh capture, that
  `/click` lands on the right element at the right coordinates, and that
  `/keys` types into the focused field.

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
not, dropping the snapshot from a capture, or leaving the light DOM out of a
shadow host each makes the matching assertion in `test_fidelity.py` fail.
removing the `shadowrootmode` attribute, the text-node childList guard, the CSS
`url()` rewrite, the `<base>` filter, the `broadcast` of captures, or the
coordinates in `/click` each makes the matching assertion fail. Where a
mutation check did *not* fail, the test was wrong and was fixed. One is worth
recording: editing a text node with `li.firstChild.nodeValue = x` emits a
**characterData** record, while `li.textContent = x` emits a **childList**
record whose added and removed nodes are text nodes. Those are different code
paths, and only the second one was broken when the mirror stopped updating -
so a suite that tests the first tells you nothing about the second. Both are
now pinned separately.

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
the declarative-shadow-DOM template shifting an ordered id sidecar, and
Playwright changing the shape of a binding's source from an object to a dict.

## Files

- `server.py` — FastAPI: `/` `/start` `/status` `/1.html` `/mirror.js`
  `/capture` `/res` `/ws` `/click` `/keys` `/key` `/shot.png`
- `agent.js` — injected into every frame; captures the page and reports changes
- `mirror.js` — runs in the mirror page; shows the capture, forwards input
- `client.html` — the one page; calls `/start` on load, then loads `/1.html`
- `1.html` — the capture, written out by the server as it changes (generated)
- `run.bat` — Windows launcher

## Note

The app clicks Sign In and then stops. You type your Apple ID and two-factor
code yourself in the browser window that opened — it never touches your
password.
