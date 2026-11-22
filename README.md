# iCloud

FastAPI on port 9002. Visiting the page still opens a browser, loads
icloud.com and clicks Sign In. The page in the app is not that browser: once
Sign In has been clicked it becomes a capture you made earlier, served from
`src/` as a document of its own.

But it is not a picture of a page. The capture is wired to the browser the
server opened: what you type into it is written into the real sign-in form,
a button you press is pressed there as well, and what the browser is
showing is put back into the capture. The capture is the controls; the
browser is the session. That is what makes it remote browsing.

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

Nothing else changes on the way out: the capture is served exactly as it was
captured, plus the sync script (see below).

## Sign in still happens, in the real browser

`/start` launches a visible browser with a persistent profile, goes to
icloud.com and clicks Sign In, so you can sign in there yourself. That browser
is not mirrored any more: nothing is captured from it, and its window is not
what the app shows.

## The capture talks to the browser

A capture is a copy of a moment; the browser holds the session. The two are
tied together by their ids: a capture keeps the ids of the page it was taken
from, so the field with the id `account_name_text_field` in the capture is
the field with that id in the real page — in whichever frame of the browser
happens to hold it.

| In the capture | In the browser |
|---|---|
| a value typed into a field | the field with the same id, typed into, in whichever frame has it |
| a button pressed | the button with the same id, pressed |
| what the browser is showing | put back into the capture's fields |

Three things had to be true for any of this to work, and none of them were.

**The capture's policy has to let the engine in.** A SingleFile capture
carries the policy of the page it was taken from, and that policy is as
tight as policies get: `default-src 'none'`, so nothing loads and nothing
runs unless it is named. The engine is a script of ours, from our own
origin, and it opens a socket to our own origin — and the policy said no to
both, because it was written for a page that has no engine in it. So the
policy is served *extended*, not as it was: `script-src` gains `'self'`, and
`connect-src` is added. Everything the capture forbade stays forbidden, so
the page still renders exactly as it was captured. The nested document
inside an `<iframe srcdoc>` has a policy of its own, and that one is left
alone.

**The form has to be reachable.** The sign-in form is not in the capture's
own document: SingleFile keeps it inside an `<iframe srcdoc="…">`, and that
frame is sandboxed without `allow-same-origin`, so it is a frame of an
origin of its own and the page cannot reach it — not its fields, not its
buttons, nothing. So a frame the capture carries is served with
`allow-same-origin` added to its sandbox. Nothing else about it changes: it
keeps its sandbox, and it keeps its scripts.

**The engine has to look into the frame.** It does: the engine binds the
page and every frame it can reach, and an element in a frame is named by the
frame it is in first, so the same selector in two documents is two different
elements.

The engine asks where the browser is whenever it binds a document, so a
frame that has just loaded is told the state even when it was sent before
the frame was there. A field someone is typing into is never overwritten by
that state — their keystrokes are the newer truth.

**The value goes in as keys.** The sign-in widget is a widget: it watches
for keys, not for values. So what you type in the capture is *typed* into
the browser — clicked into, then typed, key by key — and the page reacts as
it would if a person were there. A value assigned behind the widget's back
sits in the field and the page is not moved by it. Where typing cannot
happen — the control is not on screen — the value is written the old way,
so that something is better than nothing.

**The typing carries on.** The letters that arrive while the person is
still typing are held back a moment and typed as one, with the cursor at
the end of what is already there. A field that is emptied and filled again
for every letter is a field the widget never gets to the end of — and a
field that is clicked into every time puts the cursor in the middle of it,
so the address comes out as `someone@exomample.c`.

**What the browser says, the capture says.** When the details are wrong,
the browser says so — *"Check the account information you entered and try
again."* The capture says it too, in the browser's own words, with the
link the browser carries, in the browser's own colours: the capture does
not carry the stylesheet for a message that was not there when it was
taken, so the way the browser shows it is read as well and put on the
message in the capture.

**A press says what it did.** Apple's Continue stays disabled until the
form is filled in, and a click on a disabled button does nothing at all —
which looks, from the outside, like the bridge is not working. So the
console says `sign-in is disabled - the browser's form is not filled in
yet` rather than pretending it pressed something. And when the browser is
further behind than the capture — the capture shows the form, the browser
still shows the landing page — pressing Continue takes the sign-in
further, by pressing the browser's own sign-in button.

**A control with no id is named by its place.** Apple's verification code
boxes have no ids at all: six inputs and a hidden one. They are the third
input of the page, the fourth input of the page, and so on — and they are
written only when the live page has the same number of them. If it does
not, the right box cannot be told from the wrong one, and a code typed
into the wrong box is worse than no code at all, so nothing is written and
the console says so.

**The capture is the copy; the browser is the session.** A capture holds
the same ids as the page it was taken from, so a capture opened in the
same browser as the session would be written into instead of the session
— the value would go back into the copy on the screen. The pages the app
serves are therefore left out of the way.

**A value that has nowhere to go yet is kept.** The sign-in widget is a
frame on another origin, and Apple loads it a second or two after Sign In
is clicked. A value typed in that window has no field to go into, so it is
kept and tried again: the person does not have to type it twice. A press is
kept for a shorter while — a button pressed long after the person pressed
it is not what they asked for.

**What the browser is showing is read from every frame, not the first one.**
The sign-in form is in a frame and the document above it has no fields at
all, so reading only the top document is how a capture keeps a stale value
that the browser has already cleared.

**What this is not.** The capture can drive the browser; it cannot show a
page it has no capture of. The iCloud app after the sign-in is not a file in
`src/`, so the capture of the sign-in screen is all it can show. For a live
view of the browser itself, the mirror — an agent in the real browser that
streams the live document — is the other design, and it is a different
branch.

## The flow

Which capture leads to which is the user's, and it is said in a file next to
the captures: `src/flow.json`.

```json
{
  "steps": [
    {"page": "/src/1.html", "press": "sign-in", "go": "/src/4.html",
     "live": true},
    {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
     "live": true, "wait": true}
  ]
}
```

`page` is the capture the press happened on, `press` is the id of the
control that was pressed (`"*"` for any control on that page), `go` is the
capture the app moves to, `live` says whether the press also happens in the
browser, and `wait` says that the app moves on when the *browser* has, not
when the button was pressed.

**Everyone moves, including the viewer that pressed.** A button press is not
a link, so nothing else would take the viewer that pressed it to the next
capture while everybody else went on without it.

**A step without `live` does not touch the browser.** Pressing Continue on a
capture that has not asked for a password yet would submit an empty form to
the site. A step with `live` presses it in the browser as well — which is
what you want on the step where the form is filled in.

A press the flow does not mention still happens in the browser, as it always
did. There is no flow file, there is no flow.

The captures that are in `src/` are steps of the sign-in, and the flow file
that comes with them is:

| Capture | What it is | Pressing Continue |
|---|---|---|
| `1.html` | the sign-in form | moves the app to `4.html` and presses Continue in the browser |
| `4.html` | the sign-in form, with "remember me" | presses Continue in the browser, and moves the app to `2.html` **when the browser has taken the details** — if they are wrong, the app stays here and says what is wrong |
| `2.html` | the verification code step | — |

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

One URL, one rule: **the file, plus the three things the engine needs.**

```html
<script src="/sync.js"></script>
```

That tag is the first of them. The second is the capture's own
`Content-Security-Policy`, extended with the sources the engine needs, and
the third is `allow-same-origin` on the frames the capture carries — both
explained in [the capture talks to the browser](#the-capture-talks-to-the-browser).

Nothing else is added: no meta is rewritten, no script is stripped, no
policy is removed, and the file on disk is never written to. The insertion
point is found by reading tags, not by searching for text, so it can never
land inside an attribute value: a SingleFile export keeps a whole nested
document in `<iframe srcdoc="…">`, and that one is left untouched.

- **Everything else is served as bytes**, with a content type from the file
  name. Keep a capture's stylesheets, scripts, images and fonts next to it.
- **Nothing outside `src/` is reachable.** A path that resolves elsewhere is
  answered as a missing page, not as a file read.
- **A missing page explains itself.** `/src/nope.html` is a 404 with a note
  saying where captures go and what is there now, so a fresh clone with an
  empty `src/` is not a blank page.

Everything is served `Cache-Control: no-store`, so an edited capture shows up
on reload.

## The socket

One socket per viewer at `/ws`, plus `POST /nav` for the beacon.

Client to server:

```json
{"t": "hello", "page": "/src/1.html", "from": "http://localhost:9002/"}
{"t": "value", "key": "#account", "value": "someone@example.com", "checked": false}
{"t": "field", "id": "account_name_text_field", "value": "someone@example.com", "checked": false}
{"t": "press", "id": "sign-in"}
{"t": "click", "key": "#go", "x": 120, "y": 340}
{"t": "nav",   "page": "/src/2.html"}
{"t": "where"}
```

Server to client:

```json
{"t": "state", "page": "/src/1.html", "values": {"#account": {"value": "…", "checked": false}}}
{"t": "browser", "url": "https://idmsa.apple.com/signin", "title": "…", "fields": {"account_name_text_field": {"value": "…", "checked": false}}}
{"t": "value", "key": "#account", "value": "…", "checked": false}
{"t": "click", "key": "#go", "x": 120, "y": 340}
{"t": "nav",   "page": "/src/2.html"}
```

`field` and `press` are the bridge to the browser. `where` asks where the
browser is and `browser` is the answer: where it is, what it is called, and
what its fields hold. The server sends `browser` on its own when the browser
has moved on, and when a viewer asks.

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

## Who the browser belongs to

Every visitor gets their own browser, their own profile and their own
capture. Two people using the app at the same time do not share a session,
do not share a page, and never see each other's typing.

The visitor is named by a cookie — a name, not a secret — and the name is
also the name of their profile: `profiles/<name>/` is that browser's
memory. It is what makes the session a session: the cookies, the "trust
this device" answer, the half-typed address are all still there when they
come back, because it is the same browser with the same profile.

The name is kept in two places, because it is worth keeping. The visitor's
own storage (`localStorage`) is how they are *remembered* — a person who
comes back is recognised, even if the cookie has gone, and lands in the
same session again. The cookie is how the server hears about it on every
later request, and the page says the name with the request as well
(`/start?visitor=…`), so the two agree.

The profile is kept where the base branch keeps it — `visitors/<name>/` —
so a browser that has already signed in is not asked to sign in again.

Two details matter more than they look:

- The cookie has to be a real `Set-Cookie`. A header called
  `icloud_session` with a name in it is only a header: the browser keeps no
  cookie, every request looks like a new visitor, and one person becomes
  twenty — twenty sessions, twenty browsers, twenty profiles.
- A route that only *reads* must only look the session up. `/status` is
  polled every two seconds; if a poll made a session of its own, it would
  give the same person a new browser every two seconds.

The browser is opened when the visitor asks for it (`/start`), and shut
down a quarter of an hour after they have gone away. The profile is kept
either way — that is the session.

## The app does not move on until the browser has

A step in the flow can say `"wait": true`, and then the capture does not
move to the next one because a button was pressed — it moves when the
browser has taken the details and gone on to the next step. Until then the
capture stays where it is, and what the browser says about the details —
*"Check the account information you entered and try again."* — is shown on
it.

A person who has typed the wrong password should not be carried off to the
next page as if they had not.

## Console output

```
[icloud] launching system chrome
[icloud] clicking Sign In
[icloud] done - sign in yourself in the browser window
[icloud] browser: account_name_text_field written
[icloud] browser: sign-in pressed
[icloud] page: /src/2.html
```

System Chrome is the one that is tried first: it is the browser the machine
already has, it is the one the person is used to, and it is there without a
download. The bundled Chromium is the fallback for a machine with no Chrome.

The `browser:` lines are the bridge: a value written into the browser, and a
button pressed in it. When a control is not there yet, the line says so and
says what the browser *does* have:

```
[icloud] browser: account_name_text_field not found yet - the browser has no controls yet - the sign-in widget may still be loading
```

And when the button is there but will not go down — Apple's Continue stays
disabled until the form is filled in — the line says that instead:

```
[icloud] browser: sign-in is disabled - the browser's form is not filled in yet
```

That is the difference between a control that is there under another name
and a browser that has not loaded the widget yet, which are two very
different problems and used to look the same.

The `page:` line is the hub following a viewer to another captured page. If a
captured page stops responding to typing, the question is whether `/sync.js`
was served with it — open the page's source and look for the script tag.

## Tests

```bash
npm install   # once, pulls jsdom
npm test
```

Five suites:

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
  for the state again. It also pins the bridge: typing is sent to the browser
  as well, a button pressed is pressed in the browser, and what the browser is
  showing is put back into the capture.
- `test_sync.py` runs the rest of `server.py` without uvicorn: the engine is
  injected once and only into HTML, the capture's policy is extended rather
  than replaced, the frames a capture carries are made reachable, the nested
  document inside a frame is left alone, assets keep their type, nothing
  outside `src/` is readable, a missing capture says so, and the hub really
  is the shared state — a value reaches the other viewer and the next one to
  join, a click is relayed but a click with no position is dropped, a link
  moves every viewer and clears form state, an off-site page is refused, a
  viewer that leaves is forgotten, and `/` is the launcher until the browser
  is up and the capture afterwards.
- `test_live.py` runs the bridge without a browser: the browser is a stub, so
  the scripts the bridge runs in the browser are the shipped ones while the
  browser answering them is not real. It pins that a value reaches the field
  with the same id — in whichever frame holds it, and in whichever tab — that
  a button is pressed there too, that an id that is not an id is refused,
  that a value with nowhere to go yet is kept and written when its field
  turns up, and that what the browser is showing reaches the viewers.
- `test_browser.py` runs the whole way round in a real Chromium: a capture
  with the policy a SingleFile capture carries, and the form in a frame that
  is sandboxed without `allow-same-origin`. It pins that the engine runs at
  all, that the policy does not refuse it, that the form inside the frame is
  reachable, and that typing and pressing in the capture reach the browser —
  and come back. It also runs the three scripts the bridge sends into the
  browser, in a page, as they are run in the browser: a stub that answers for
  them proves nothing about whether they work. It is skipped when there is
  no browser to be had.

`npm test` runs all of them and prints one total; `node tests/run.js` finds
the project virtualenv by itself.

## Files

- `server.py` — FastAPI: `/` `/start` `/status` `/src/…` `/sync.js` `/ws` `/nav`
- `sync.js` — runs in the captured page; reports and applies form state, clicks
  and navigation, and talks to the browser through the server
- `live.py` — the bridge to the browser the server opened: finds a control by
  its id, writes a value into it, presses it, and reads back what the browser
  is showing
- `client.html` — the launcher; opens the browser, then becomes `/src/1.html`
- `src/` — the captures (yours; `1.html` is the one the app opens) and
  `src/flow.json`, which says which capture leads to which
- `run.bat` — Windows launcher
- `tests/` — the suites above

## Note

The app clicks Sign In and then stops. You type your Apple ID and two-factor
code yourself in the browser window that opened — it never touches your
password. The page in the app is the capture from `src/`, not that window, and
it does not wait for you to finish signing in: it appears as soon as Sign In
has been clicked.

What you type into the capture *is* typed into the browser, so the capture is
not a copy you work in: it is the controls for the session the browser holds.
The password is not read back — it is written once, by you, and it is never
broadcast to another viewer or kept in the server's memory.
