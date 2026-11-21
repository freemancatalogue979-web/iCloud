# iCloud

FastAPI on port 9002. Visit the page and it opens a browser, loads icloud.com
and clicks Sign In.

## Run

```bash
pip install -r requirements.txt
python server.py
```

Then open <http://localhost:9002>

Chromium is downloaded automatically on the first visit if it isn't already
there — the page shows a spinner while that happens. To do it up front instead:

```bash
python -m playwright install chromium
```

## The page

A spinner while the browser starts, then a live mirror of the page it loaded.

The mirror is DOM streaming, not screenshots. `agent.js` is injected into
whatever document the browser loads; it serialises that document once, then a
`MutationObserver` sends only what changed:

| Patch | Meaning |
|---|---|
| `a` | an attribute changed |
| `t` | a text node changed |
| `h` | a subtree's children changed — coalesced to one entry per parent |

Changes are debounced at 30ms and pushed over a WebSocket, so bursts of DOM
churn arrive as a few frames instead of hundreds. If a patch grows past ~400KB,
or the mirror's structure drifts out of step, it resyncs by re-sending the
whole document.

Nothing is page-specific: no selectors for any site's fields, and no stored
copy of any site's markup. It mirrors whatever is loaded.

Your clicks and keystrokes in the mirror are forwarded to the real page; the
resulting DOM changes come straight back.

## Files

- `server.py` — FastAPI: `/` `/start` `/status` `/view` `/ws` `/click` `/keys` `/key` `/shot.png`
- `agent.js` — injected into the real page; MutationObserver → server
- `client.html` — the one page; calls `/start` on load, then iframes `/view`
- `view.html` — the mirror: seeds the document, applies patches

## Note

The app clicks Sign In and then stops. You type your Apple ID and two-factor
code yourself in the browser window that opened — it never touches your
password.
