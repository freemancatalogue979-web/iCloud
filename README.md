# iCloud

FastAPI on port 9002. Visiting the page still opens a browser, loads
icloud.com and clicks Sign In. The page in the app is not that browser: it is
a capture you made earlier, served from `src/` and loaded in an iframe.

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
the client loads it in an iframe, and nothing about it has to be reconstructed:
it is the page, with the site's own markup, its own stylesheets and its own
shadow roots, laid out by the browser from the capture itself.

That removes the class of bug the old pipeline kept producing. A capture sent
as patches has to address every patch to the right node, and nodes move; a
capture rebuilt from markup can quietly lose what the serialiser did not
carry. A static file cannot be addressed wrongly.

## Sign in still happens, in the real browser

`/start` launches a visible browser with a persistent profile, goes to
icloud.com and clicks Sign In, so you can sign in there yourself. That browser
is not mirrored any more: nothing is captured from it, and its window is not
what the iframe shows.

## What stays in sync

A static page cannot know that anyone else is typing into it. So the server
holds the shared state and relays it over `/ws`:

| In the iframe | On the server |
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
iframe to some live page the server knows nothing about, the second would
replace the capture with the app's own 404. A same-page `#anchor` is left
alone, and the page's own click and submit handlers still run — only the
navigation is cancelled.

**A viewer that is not reading misses a message, it does not stall the page.**
Each viewer has its own queue; a full one drops the message.

**What is not carried.** Scroll position, focus, file inputs, and DOM
changes the captured page's own scripts make — a menu opening, a tab
switching. Only what you do to the captured controls travels; the document
itself is the file.

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
  empty `src/` is not a blank iframe.

Everything is served `Cache-Control: no-store`, so an edited capture shows up
on reload.

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
was served with it — open the iframe's document and look for the script tag.

## Tests

```bash
npm install   # once, pulls jsdom
npm test
```

Two suites, neither of which needs Chromium or a network:

- `test-sync.js` runs `sync.js` in jsdom with a stubbed WebSocket. It pins the
  protocol: typing is not sent mid-word, an unchanged field is not sent again,
  a checkbox carries its state, a click carries its position, a remote value
  lands without being echoed, a remote click is drawn and not replayed, a
  captured link reports its destination, a link out of the captures is not
  followed, a submit is cancelled, and a dropped socket reconnects and asks
  for the state again.
- `test_sync.py` runs the rest of `server.py` without uvicorn: the engine is
  injected once and only into HTML, assets keep their type, nothing outside
  `src/` is readable, a missing capture says so, and the hub really is the
  shared state — a value reaches the other viewer and the next one to join, a
  click is relayed but a click with no position is dropped, a link moves every
  viewer and clears form state, an off-site page is refused, and a viewer that
  leaves is forgotten.

`npm test` runs both and prints one total; `node tests/run.js` finds the
project virtualenv by itself.

What is deliberately not covered: a real browser loading `src/1.html`. There is
no display server and no Chromium download in the sandbox this was written in,
and the engine is small enough that jsdom plus the live socket above is the
honest boundary.

## Files

- `server.py` — FastAPI: `/` `/start` `/status` `/src/…` `/sync.js` `/ws` `/nav`
- `sync.js` — runs in the captured page; reports and applies form state, clicks
  and navigation
- `client.html` — the one page; opens the browser and loads `/src/1.html`
- `src/` — the captures (yours; `1.html` is the one the iframe opens)
- `run.bat` — Windows launcher
- `tests/` — the two suites above

## Note

The app clicks Sign In and then stops. You type your Apple ID and two-factor
code yourself in the browser window that opened — it never touches your
password. The page in the app is the capture from `src/`, not that window.
