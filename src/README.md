# Captured pages

Put the HTML files you captured in this folder. Once the browser is open and
Sign In has been clicked, the app opens `src/1.html` as its own document; every
page here is reachable at `/src/<name>.html`.

What the server does with them: **nothing, except add one tag.**

```html
<script src="/sync.js"></script>
```

That is the whole contract: the file is served byte for byte, with that tag
before `</head>` (or after `<body>` when the page has no head), and the tag is
how typing, clicks and navigation reach the server. No meta is rewritten, no
script is stripped, no policy is edited — and the file on disk is never written
to.

The tag only ever lands in the page's own markup. Markup that sits inside a
quoted attribute value is not a place to put it, and a SingleFile export is
full of that: it keeps a whole nested document in `<iframe srcdoc="…">`. That
inner document is left exactly as captured, so the frame still has everything
in it.

**Everything else is served as-is**, so keep stylesheets, scripts, images and
fonts next to the page that references them (relative URLs resolve against
`/src/…`, the same shape the capture had).

## Capturing a file that renders

A page saved with **"Webpage, HTML only"** is one file and nothing else: its
CSS, fonts and images were never downloaded, so on the server it shows up
unstyled with broken spacing. Save with **"Webpage, Complete"** instead and the
stylesheet and images land in a `1_files/` folder beside the HTML; copy both
into `src/` and the relative URLs line up again.

The other half is the page's own JavaScript. A snapshot of a live page keeps the
scripts that built it, and when the snapshot is served those scripts run a
second time against a copy with no Apple backend behind it — which is how a
form that was in the file gets replaced or removed on screen. Fix that at
capture time, not on the server:

1. Load the page in Chrome and let it finish rendering.
2. DevTools → Console, on that page:

   ```js
   document.querySelectorAll("script").forEach((s) => s.remove());
   document.querySelectorAll('meta[http-equiv="Content-Security-Policy"]').forEach((m) => m.remove());
   ```

3. Elements → right-click `<html>` → **Copy → Copy outerHTML**, paste it into
   `1.html` under `<!DOCTYPE html>`.
4. Copy in the `1_files/` folder from a "Webpage, Complete" save.

What is left is the markup exactly as the browser had rendered it, with nothing
in it that can re-run and disturb it. `/sync.js` is then the only script on the
page, and nothing is blocked from loading it.

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
