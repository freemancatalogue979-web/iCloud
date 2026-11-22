# iCloud

FastAPI on port 9002. Visiting the page still opens a browser, loads
icloud.com and clicks Sign In. The page in the app is not that browser: once
Sign In has been clicked it becomes a capture you made earlier, served from
`src/` as a document of its own.

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

Put your captured pages in `src/`, starting with `src/1.html`. The folder has
its own [README](src/README.md), and `/src/` lists whatever is in it.

> `requirements.txt` asks for `uvicorn[standard]`, not plain `uvicorn`. The
> socket needs a WebSocket implementation; without one uvicorn treats the
> upgrade as an ordinary HTTP request and answers 404.

## The mirrored capture pipeline is gone

The old design had three moving parts: an agent in the real browser that
serialised the live document, a server that streamed it, and an engine in the
mirror that rebuilt it. All of it is gone — `agent.js`, `mirror.js`, the
document stream, the resource proxy, the screenshots, the click and keystroke
forwarding, and the `1.html` the server used to write out.

What replaced it is a file. A page that was captured earlier sits in `src/`,
the app opens it as a document of its own, and nothing about it has to be
reconstructed: it is the page, with the site's own markup, its own
stylesheets and its own shadow roots, laid out by the browser from the
capture itself.

That removes the class of bug the old pipeline kept producing. A capture sent
as patches has to address every patch to the right node, and nodes move; a
capture rebuilt from markup can quietly lose what the serialiser did not
carry. A static file cannot be addressed wrongly.

## How the app gets to the capture

There is no iframe. `/` is a splash screen and nothing else: a black screen
with the Apple mark breathing on it. No caption, no spinner, no links, no
"opening this / doing that". It opens the browser (`/start`) and, as soon as
`/status` answers `done` — the moment Sign In has been clicked — it replaces
itself with the captured page:

```js
location.replace("/src/1.html")
```

After that the capture is the *top-level document*. It is laid out at the
window's width, with its own scrollbars, fonts and title, instead of being a
page inside a frame inside a page. That was the thing an iframe got wrong: a
capture taken at 1440 px was being laid out at 900 px inside the frame, so it
reflowed, clipped and scrolled differently from the page it came from.

`/sync.js` does not change. It is injected into the page by the server and now
runs at the top level, so everything in the next section still holds.

Writing the capture into the launcher with `document.write` would have kept the
URL at `/`, and `sync.js` uses the URL to say which captured page is on screen
— every page would have called itself `/` and navigation between captures
would break. A navigation is the honest swap: same URL, same document, same
script behaviour as opening the file directly.

While the run is `done`, `/` redirects to the capture, so a reload comes back
to the page rather than to the splash. A browser that will not open does not
hold the page hostage either: on `error` the splash goes to the capture anyway,
and there is a 30 s cap in case the launch hangs. Nothing is ever shown about
it — the static capture is the point.

The one thing that does change on the way out is the markup: the capture keeps
its own `<meta name="apple-mobile-web-app-capable">`, and the standard
`<meta name="mobile-web-app-capable" content="yes">` is added if it is missing,
because Chrome warns about a page that has only the deprecated Apple name.

## Sign in still happens, in the real browser

`/start` launches a visible browser with a persistent profile, goes to
icloud.com and clicks Sign In, so you can sign in there yourself. That browser
is not mirrored any more: nothing is captured from it, and its window is not
what the app shows.

## What stays in sync

A static page cannot know that anyone else is typing into it. So the server
holds the shared state and relays it over `/ws`:

| In the captured page | On the server |
|---|---|
| what you type into a field | the value, kept for the next viewer and the next reload |
| a checkbox, radio or select | its state, the same way |
| a click | relayed to the other viewers as a marker |
| following a link to another `/src/*.html` page | the shared page, so every viewer follows |

The rules that matter, and why:

**A value is sent after the word, not every letter.** `sync.js` waits 70 ms
after the last `input` event and sends the value then. Checkboxes, radios and
selects are one-shot and go at once. A value that has not changed since the
last message is not sent again.

**An applied value is not sent back.** Setting `.value` does not fire `input`,
and the apply path is wrapped anyway. Without that, two viewers would bounce
one field between them forever.

**A click is drawn, not replayed.** Replaying it in another viewer would press
the same button a second time. So the other viewers get a marker where the
click landed — visible, harmless, and honest about what it is.

**Navigation is the server's decision.** A link click is reported twice: over
the socket, and with `sendBeacon` as the page unloads, because a socket message
races the navigation. The server refuses anything that is not `/src/*.html`, so
a page cannot send every viewer somewhere else. A viewer that loads a page the
server is not on is redirected to the server's page — the server is the truth
about what is on screen, which is also what makes a late viewer land in the
right place.

**Form state belongs to the page it was typed on.** Following a link clears it.

**A link that leaves the captures is not followed, and a form is not
submitted.** Neither has anything behind it any more: the first would take the
page to some live site the server knows nothing about, the second would replace
the capture with the app's own 404. A same-page `#anchor` is left
alone, and the page's own click and submit handlers still run — only the
navigation is cancelled.

**A viewer that is not reading misses a message, it does not stall the page.**
Each viewer has its own queue; a full one drops the message.

**What is not carried.** Scroll position, focus, file inputs, and changes
made by the capture's own scripts — a menu opening, a tab switching, the site
re-rendering itself. Only what you do to the captured controls is relayed: the
page itself is not mirrored.

The state is in memory. Restarting the server clears it, and it is never
written back into `src/` — those files are the captures.

## Serving `src/`

- **HTML gets `/sync.js` injected** into its `<head>` as it is served. The file
  on disk is untouched. A capture with no head gets the tag after `<body>`
  instead, and a page that already has it is left alone.
- **Everything else is served as bytes**, with a content type from the file
  name. Keep a capture's stylesheets, scripts, images and fonts next to it.
- **Nothing outside `src/` is reachable.** A path that resolves elsewhere is
  answered as a missing page, not as a file read.
- **A missing page explains itself.** `/src/nope.html` is a 404 with a note
  saying where captures go and what is there now, so a fresh clone with an
  empty `src/` is not a blank page.
- **The capture is served as it was captured.** Its own scripts are kept —
  the page that renders correctly is the one to trust. A few captures do
  damage themselves when their JavaScript runs against a copy (Apple's
  sign-in app can replace the form it was captured with), and for those the
  scripts can be taken out:

  | | |
  |---|---|
  | `touch src/1.html.strip-scripts` | that page only, permanent (marker file) |
  | `ICLOUD_STRIP_SCRIPTS=1 python server.py` | every page |
  | `/src/1.html?static=1` | one request, for trying it out |

  The `X-Capture-Scripts` response header says `kept` or `stripped`.
- **The capture's Content-Security-Policy is extended, not removed.** The
  policy was written for the site's origin, and on this one it blocks
  `/sync.js`, the socket, and Chrome's request to
  `/.well-known/appspecific/com.chrome.devtools.json` — the
  `default-src 'none'` violation in the console. So
  `script-src 'self'; connect-src 'self' ws: wss:` is appended to the policy
  and nothing else changes: whatever the capture forbade (its own scripts,
  remote styles) stays forbidden, which is why the page looks the same as
  `?raw=1` and not one thing more.

  A policy that already pins scripts with a `script-src` of its own is left
  alone — appending after it would be ignored and `/sync.js` would only log a
  violation — and the engine is not added: `X-Capture-Sync: blocked`.
- **The standard web-app meta is added** when the capture does not have it,
  as the first thing in the head — before the deprecated Apple meta, so Chrome
  never sees the Apple name without the standard one and has nothing to warn
  about.
- **`?raw=1` serves the file untouched** — no engine, no meta, no policy
  change. It is the reference: if it renders, the served page must render the
  same way, and now does.

Everything is served `Cache-Control: no-store`, so an edited capture shows up
on reload.

### If a capture does not look like the page

There are three versions of the same file to compare, cheapest first:

| URL | What it is |
|---|---|
| `/src/1.html?raw=1` | the file, byte for byte — nothing added, nothing removed |
| `/src/1.html` | the file with the engine, the standard meta and the policy extended |
| `/src/1.html?static=1` | the same, with the capture's own scripts taken out |

The first two must look the same. If they do not, the difference is the
engine, the meta, or the policy extension — nothing else is touched. If both
look wrong, the capture itself is missing something, usually the site's CSS: a
page that points at stylesheets which were never saved renders as bare markup.

`?static=1` is the third thing to try when a capture damages itself: if the
page only behaves with scripts gone, leave a `.strip-scripts` marker next to
it and stop thinking about it.

The console is the other place to look. `X-Capture-Sync: blocked` means the
capture's policy pins scripts, so the page runs none of the engine and none of
its own: a capture like that cannot be synced without changing its policy.

## The socket

One socket per viewer at `/ws`, plus `POST /nav` for the beacon.

Client to server:

```json
{"t": "hello", "page": "/src/1.html", "from": "http://localhost:9002/"}
{"t": "value", "key": "#account", "value": "someone@example.com", "checked": false}
{"t": "click", "key": "#go", "x": 120, "y": 340}
{"t": "nav",   "page": "/src/2.html"}
```

Server to client:

```json
{"t": "state", "page": "/src/1.html", "values": {"#account": {"value": "…", "checked": false}}}
{"t": "value", "key": "#account", "value": "…", "checked": false}
{"t": "click", "key": "#go", "x": 120, "y": 340}
{"t": "nav",   "page": "/src/2.html"}
```

`from` is the referrer: a hello whose referrer is another captured page is a
link being followed — the beacon may have been lost — and the hub moves to it.

`state` is what a viewer is given when it connects, or when it asks again after
reconnecting. Values and clicks are broadcast to the *other* viewers; the one
that sent them already has them.

An element is named by a CSS selector built from its id, or from its tag and
position when it has no id (`body > form > input:nth-of-type(2)`). Both copies
of a captured file have the same shape, which is all the name has to survive.

A dropped socket reconnects with backoff, 500 ms doubling to 5 s, and says
`hello` again when it does.

## Console output

```
[icloud] launching bundled chromium
[icloud] clicking Sign In
[icloud] done - sign in yourself in the browser window
[icloud] page: /src/2.html
```

The `page:` line is the hub following a viewer to another captured page. If a
captured page stops responding to typing, the question is whether `/sync.js`
was served with it — open the page's source and look for the script tag.

## Tests

```bash
npm install   # once, pulls jsdom
npm test
```

Three suites, none of which needs Chromium or a network:

- `test-launcher.js` runs `client.html` in jsdom with a stubbed `/status`. It
  pins the splash: the screen is only the mark — no text, no buttons, nothing
  that pops up — it navigates only once the run is over, it navigates on a
  failed launch too, and it stops polling after it goes.
- `test-sync.js` runs `sync.js` in jsdom with a stubbed WebSocket. It pins the
  protocol: typing is not sent mid-word, an unchanged field is not sent again,
  a checkbox carries its state, a click carries its position, a remote value
  lands without being echoed, a remote click is drawn and not replayed, a
  captured link reports its destination, a link out of the captures is not
  followed, a submit is cancelled, and a dropped socket reconnects and asks
  for the state again.
- `test_sync.py` runs the rest of `server.py` without uvicorn: the engine is
  injected once and only into HTML, the capture is otherwise served as
  captured, its CSP is extended rather than replaced (and the engine skipped
  when the policy pins scripts), scripts can be stripped on request, the
  standard web-app meta is added before the Apple one, `?raw=1` serves the
  file untouched, assets keep their type, nothing outside `src/` is readable,
  a missing capture says so, and the hub really is the
  shared state — a value reaches the other viewer and the next one to join, a
  click is relayed but a click with no position is dropped, a link moves every
  viewer and clears form state, an off-site page is refused, a viewer that
  leaves is forgotten, and `/` is the launcher until the browser is up and the
  capture afterwards.

`npm test` runs all three and prints one total; `node tests/run.js` finds the
project virtualenv by itself.

What is deliberately not covered: a real browser loading `src/1.html`. There is
no display server and no Chromium download in the sandbox this was written in,
and the engine is small enough that jsdom plus the live socket above is the
honest boundary.

## Files

- `server.py` — FastAPI: `/` `/start` `/status` `/src/…` `/sync.js` `/ws` `/nav`
- `sync.js` — runs in the captured page; reports and applies form state, clicks
  and navigation
- `client.html` — the launcher; opens the browser, then becomes `/src/1.html`
- `src/` — the captures (yours; `1.html` is the one the app opens)
- `run.bat` — Windows launcher
- `tests/` — the two suites above

## Note

The app clicks Sign In and then stops. You type your Apple ID and two-factor
code yourself in the browser window that opened — it never touches your
password. The page in the app is the capture from `src/`, not that window, and
it does not wait for you to finish signing in: it appears as soon as Sign In
has been clicked.
