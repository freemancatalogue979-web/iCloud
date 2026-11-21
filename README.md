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

`client.html` is one full-viewport `<iframe>` pointing at `/view`. Nothing
else — no spinner, no icon, no status text, no overlay. `/view` is the live
mirror of the real page.

The mirror is DOM streaming, not screenshots. `agent.js` is injected into
every frame the browser loads; it captures each document once, then a
`MutationObserver` sends only what changed:

| Patch | Meaning |
|---|---|
| `a` | an attribute changed |
| `t` | a text node changed |
| `h` | a subtree's children changed — coalesced to one entry per parent |

Changes are debounced at 30 ms and pushed over a WebSocket, so bursts of DOM
churn arrive as a few frames instead of hundreds. Patches are sent at any
size: re-seeding the whole document instead would be visible as a flash, and a
page that mutates constantly would loop on it. Re-seeds are rate-limited to one
per 2 s and only happen when the mirror's structure genuinely drifts.

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
live shadow root with no scripting in the mirror. Mutation paths cross shadow
boundaries with a `#` segment.

`<base>` is removed, as SingleFile's `cleanupPage()` does: a copied page does
not have the original's base URI, and a leftover `<base href="/">` silently
points every relative URL at localhost.

**Resources** are the one deliberate divergence. SingleFile inlines everything
as `data:` URIs because it produces a file; this produces a live stream, so
URLs are rewritten to `/res?url=<original>` and the server fetches them with
`ctx.request.get`, which carries the site's cookies and gets around CORS on
fonts. CSS bodies have their own `url()`s rewritten against the sheet's origin.
Scripts and `on*` handlers are dropped, and frames are sandboxed to
`allow-same-origin` only.

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
