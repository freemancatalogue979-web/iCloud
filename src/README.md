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
- **The capture's own `<script>` tags are removed.** The DOM they would
  build is already in the file, and run against a copy they can blank it
  (Apple's sign-in app replaces its own captured form) or navigate away.
  `/sync.js` is the only script that runs. If a page needs its scripts back,
  put an empty file beside it named `<page>.keep-scripts` (for example
  `1.html.keep-scripts`), or open it with `?scripts=1` to try it for one
  request.
- **A captured Content-Security-Policy `<meta>` is removed**, because it was
  written for the site's origin and here it would block `/sync.js` and the
  socket.
- **The standard `mobile-web-app-capable` meta is added** if it is missing,
  before the Apple one.

`?raw=1` serves a page untouched (scripts and CSP included) — use it to see
what the file itself does when the served page looks different.

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
