# Captured pages

Put the HTML files you captured in this folder. Once the browser is open and
Sign In has been clicked, the app opens `src/1.html` as its own document; every
page here is reachable at `/src/<name>.html`.

What the server does with them: **three things, and all three are so that the
page can be used.**

```html
<script src="/sync.js"></script>
```

The tag is how typing, clicks and navigation reach the server. It goes in
before `</head>` (or after `<body>` when the page has no head), and the file
on disk is never written to.

The other two are needed because a capture is a copy of a page that had its
own restrictions, and those restrictions were written for a page with no
engine in it:

- **The page's own `Content-Security-Policy` is extended, not edited.** A
  SingleFile capture carries `default-src 'none'`, which refuses the engine
  and its socket. `script-src` gains `'self'` and `connect-src` is added;
  everything the capture forbade stays forbidden, so the page still renders
  exactly as it was captured. The policy of the nested document inside an
  `<iframe srcdoc>` is left alone.
- **A frame the capture carries is given the page's own origin**
  (`allow-same-origin` is added to its sandbox). The sign-in form in a
  SingleFile export lives in such a frame, and without this the page cannot
  reach it at all — not its fields, not its buttons, nothing.

No meta is rewritten, no script is stripped, and the file on disk is never
written to.

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
   ```

3. Elements → right-click `<html>` → **Copy → Copy outerHTML**, paste it into
   `1.html` under `<!DOCTYPE html>`.
4. Copy in the `1_files/` folder from a "Webpage, Complete" save.

What is left is the markup exactly as the browser had rendered it, with nothing
in it that can re-run and disturb it. `/sync.js` is then the only script on the
page, and nothing is blocked from loading it.

There is no need to remove the `Content-Security-Policy` meta any more: the
server extends that policy for the engine and leaves the rest of it alone.

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

## And the browser behind the page

The page is also wired to the browser the server opened, by id: the field with
the id `account_name_text_field` here is the field with that id there.

| In the page | In the browser |
|---|---|
| a value typed into a field | typed into the field with the same id |
| a value typed into a field with no id | typed into the same place — the third input is the third input |
| a button pressed | pressed in the browser too |
| what the browser is showing | put back into the page's fields |

The value is typed, key by key, not assigned: the widget watches for keys,
and a page nobody has touched does not move on. The letters that arrive
while you are still typing are held back a moment and typed as one, with
the cursor at the end of what is already there.

And what the browser says, the capture says: when the details are wrong,
the complaint — *"Check the account information you entered and try
again."* — shows in the capture too, in the browser's own words, with the
browser's own link and colours.

So a capture is not a copy you work in — it is the controls for the session
the browser holds. What it cannot do is show a page it has no capture of.

## The flow between the captures

`flow.json` in this folder says which capture leads to which:

```json
{
  "steps": [
    {"page": "/src/1.html", "press": "sign-in", "go": "/src/4.html"},
    {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
     "live": true}
  ]
}
```

`page` is the capture, `press` is the id of the control that was pressed
(`"*"` for any), `go` is the capture the app moves to, and `live` says
whether the press also happens in the browser. A step without `live` leaves
the browser alone — pressing Continue before the form is filled in would
submit an empty form to the site.

Edit it to change the order. A press it does not mention still happens in
the browser, as it always did.
