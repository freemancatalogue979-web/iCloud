# Captured pages

Put the HTML files you captured in this folder. Once the browser is open and
Sign In has been clicked, the app opens `src/1.html` as its own document; every
page here is reachable at `/src/<name>.html`.

What the server does with them:

- **HTML is served with `/sync.js` injected** into its `<head>`. That script
  is how typing, clicking and navigation reach the server. The file on disk is
  left exactly as captured.
- **Everything else is served as-is**, so keep stylesheets, scripts, images
  and fonts next to the page that references them (relative URLs resolve
  against `/src/…`, the same shape the capture had).
- **Your page is served as captured** — scripts, styles and markup untouched.
  The only script the server adds is `/sync.js`.
- **A captured Content-Security-Policy `<meta>` is extended**, not removed:
  `script-src 'self'; connect-src 'self' ws: wss:` is appended so the engine
  and its socket can run. Everything the policy already forbade stays
  forbidden, so the page renders as it was captured.
- **The standard `mobile-web-app-capable` meta is added** if it is missing,
  before the Apple one.
- **If a capture damages itself when its scripts run** against a copy, put an
  empty file beside it named `<page>.strip-scripts` (for example
  `1.html.strip-scripts`), or open it with `?static=1` to try that for one
  request.

`?raw=1` serves a page byte for byte (no engine, no meta, no policy change) —
use it as the reference when a served page looks different.

`/src/` (no file name) lists what is here, and a missing page says so instead
of showing a bare 404, so a fresh clone with an empty `src/` still explains
itself.

## What stays in sync

| In the page | On the server |
|---|---|
| what you type into a field | the value, kept for the next viewer and the next reload |
| a checkbox, radio or select | its state, same as above |
| a click | relayed to the other viewers as a marker (not replayed) |
| following a link to another `/src/*.html` page | the shared page, so every viewer follows |

The state is in memory: restarting the server clears it. It is deliberately
not written back into these files — these are the captures.
