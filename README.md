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

**Resolved styles are inlined.** The mirror's styling would otherwise depend
entirely on `/res` fetching the site's stylesheets, and that fails silently the
moment a sheet is CORS-blocked, session-scoped or redirected — leaving bare
markup with no explanation. Each element carries its computed style instead, so
the page looks right whether or not any of that arrives. A curated list of ~85
properties is used rather than all 300+: PhantomStream found iterating
everything cost 45 s on a heavy page. No-op values (`auto`, `none`, `normal`,
`0px`) and inherited values the parent already carries are dropped, which takes
an element from ~1.3 KB to ~200 bytes. `url()`s inside the resolved text are
proxied like any other.

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

## Tests

```bash
npm install   # once, pulls jsdom
npm test
```

Four suites run `agent.js` and `view.html` inside a real DOM: serialisation,
the frame-tree handshake over postMessage, inline SVG and web components, and a
differential test that captures in one document, rebuilds in a second, and
applies the ops using the applier lifted out of the shipped `view.html` rather
than a copy of it.

They exist because no browser can be installed in the development sandbox. Every
serious bug here was found by jsdom, not by reasoning: text nodes being deleted
from patches, `ShadowRoot.cloneNode()` throwing by spec, a lockstep walk
iterating live `childNodes`, `event.source` being null, and the
declarative-shadow-DOM template shifting an ordered id sidecar.

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
