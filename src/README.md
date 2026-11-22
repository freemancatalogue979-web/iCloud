# Captured pages

Put the HTML files you captured in this folder. Once the browser is open and
Sign In has been clicked, the app opens `src/1.html` as its own document; every
page here is reachable at `/src/<name>.html`.

What the server does with them: **nothing, except add one tag.**

```html
<script src="/sync.js"></script>
```

That is the whole contract: the file is served byte for byte, with that tag
before `</head>`, and the tag is how typing, clicks and navigation reach the
server. No meta is rewritten, no script is stripped, no policy is edited — and
the file on disk is never written to.

**Everything else is served as-is**, so keep stylesheets, scripts, images and
fonts next to the page that references them (relative URLs resolve against
`/src/…`, the same shape the capture had).

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
