# iCloud

FastAPI on port 9002. Visiting the page opens a real browser, loads icloud.com
and clicks Sign In. Once the sign-in form is actually up, the app shows a
sign-in page you captured earlier, served from `src/`, in a frame of its own —
and everything typed into that page is written into the real one in the browser
window.

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

Put your captured pages in `src/`, starting with `src/1.html`. Images and
stylesheets next to a capture are served next to it, at the same relative
paths. The folder has its own [README](src/README.md), and `/src/` lists
whatever is in it.

> `requirements.txt` asks for `uvicorn[standard]`, not plain `uvicorn`. Without
> the extra there is no WebSocket implementation, and a run.bat that installs
> plain `uvicorn` gets a 404 on the next thing that asks for one.

## What the app is

Three things, and no more:

1. **The wait.** `/` is a black screen with the Apple mark breathing on it. No
   text, no caption, no spinner, no "opening this / doing that".
2. **The capture.** When the sign-in form is up in the browser, `src/1.html` is
   shown in a frame that fills the window. Its stylesheets, its images, its
   fonts and its layout are the capture's own; nothing of the app page reaches
   them, and nothing of the capture is rewritten.
3. **The mapping.** What is typed into the capture is written into the real
   page in the browser window: a field maps to the element with the same id,
   and a click on Continue is a click on the real Continue.

There is no mirror and no capture pipeline: no agent in the browser, no
document stream, no resource proxy, no engine rebuilding a page from markup.
`src/` is a folder of HTML files that were captured earlier, and they are
served as they are.

## How it gets to the capture

```
/            app page: the mark, then the frame
/start       open the browser, go to icloud.com, press Sign In,
             wait for #account_name_text_field to exist
/status      idle | running | done | error, and which page to show
/src/…       a captured file, byte for byte, or an image next to it
/map.js      the bridge the app page runs (not injected into the capture)
/map         one thing done in the capture, written into the real page
```

The app page asks `/start` when it loads, then polls `/status` twice a second.
`done` means the sign-in form is on screen in the browser — the field is really
there, not promised by a timer — and that is the moment the frame is pointed at
`/src/1.html`:

```js
frame.src = "/src/1.html";
```

If the form never turns up, the capture is shown anyway after the wait times
out (90 s), and a failed launch shows it immediately: a black screen that never
ends is worse than a capture that is a moment late. There is a 1.2 s fallback
in the page too, in case a frame never reports that it loaded.

The frame is deliberate. A capture carries its own `html`, `body` and reset
styles, so a frame is what keeps its styling its own; and because it is the
same origin, the app page can read and listen to the fields inside it.

## The mapping

The captures kept the ids of the page they were taken from, so there is nothing
to translate: `#account_name_text_field` in the capture belongs in
`#account_name_text_field` of the real page. That is the whole mapping.

| In the capture | In the browser window |
|---|---|
| typing into a field | the same field, set and announced with `input`/`change` |
| a select changing | the same select, same way |
| a click on a button, link or `role=button` | the same element clicked |
| a text field | never clicked: it is a value, not a button |
| a checkbox or radio | ignored: its state is not a value |
| a link or a form submit | stopped before it navigates the capture away from itself |

**Typing is sent after the word, not every letter.** `map.js` waits 120 ms after
the last `input` event and posts the value then; a burst of keystrokes becomes
one message with the last value.

**The value is written through the prototype setter.** `el.value = x` is
invisible to the widget's framework; the setter plus `input` and `change` is
what makes the real page agree that something was typed.

**The form is not in the page's own document.** icloud.com renders the sign-in
form in a widget iframe, so both sides look in every frame: the server looks in
every frame of every open page (the sign-in flow may open a tab of its own), and
`map.js` binds the frames inside the capture as well as the capture itself —
a SingleFile export keeps the form in `<iframe srcdoc="…">`, which is why the
frame inside the page has to be bound too.

**Nothing is injected into the capture to do any of this.** The frame's parent
is the app page, which is ours, so it watches the field and posts `/map`; the
server does the writing. A policy inside the capture therefore cannot stop the
mapping — it only ever applies to the capture's own scripts.

## Serving `src/`

One rule: **the file, byte for byte.**

- An HTML file is served as `text/html` with its bytes untouched: no script
  added, no meta rewritten, no policy touched, no comments removed.
- Any other file next to it — `1_files/site.css`, `1_files/apple.png` — is
  served with a type from its name. Relative URLs in the capture resolve
  against `/src/…`, the same shape the capture had.
- **Nothing outside `src/` is reachable.** A path that resolves elsewhere is
  answered as a missing page, not as a file read.
- **A missing page explains itself.** `/src/nope.html` is a 404 with a note
  saying where captures go and what is there now, so a fresh clone with an
  empty `src/` is not a blank page.

Everything is served `Cache-Control: no-store`, so an edited capture shows up on
refresh instead of serving the browser's cached copy.

## Sign in still happens, in the real browser

`/start` launches a visible browser with a persistent profile (`profile/`),
goes to icloud.com and clicks Sign In. That window is where the sign-in really
happens; it is not mirrored, and it is not what the app shows. The app shows the
capture, and the capture writes into that window through the mapping.

## Console output

`/status` is polled twice a second, which would bury everything else, so those
lines are filtered out. What is left is the story of the run:

```
[icloud] launching bundled chromium
[icloud] browser open (bundled chromium)
[icloud] going to icloud.com
[icloud] clicking Sign In
[icloud] waiting for #account_name_text_field in the browser
[icloud] the sign-in form is up
```

`state["step"]` is the same text, and `/status` returns it.

## When something is wrong

- **The browser does not open.** `/status` says `error` with the reason — no
  display, no Chromium. The capture is shown anyway.
- **The form never appears.** The wait times out after 90 s and says so; the
  capture is shown anyway. Confirm the Sign In button selector
  (`ui-button.sign-in-button`) still matches on icloud.com.
- **Typing does nothing in the browser window.** Check `/status` is `done`;
  then whether the browser has the form open at all. `/map` answers
  `{"ok": false}` when nothing in the browser has that id, which is exactly the
  case where the capture is ahead of the page.

## Tests

```bash
npm install   # once: pulls jsdom
npm test
```

None of it needs Chromium, a display or a network.

- `test-mapping.js` runs `map.js` in jsdom: what a keystroke, a burst of them, a
  select, a checkbox, a click on a label and a submit each send, that a frame
  inside the capture is bound too, and that detaching really detaches.
- `test-launcher.js` runs the app page with `/status`, `/start` and `/map`
  stubbed: that the wait is only the mark, that the frame is not loaded until
  the form is up, that it is shown when it is, that polling stops, and that a
  failed launch still shows the capture.
- `test_mapping.py` runs the rest of `server.py` without a browser: byte-for-byte
  serving (and images next to a capture), the traversal guard, the field →
  selector mapping, looking in every frame of every page, the wait for the form,
  and everything `/map` accepts and refuses.

## Files

- `server.py` — FastAPI: `/` `/start` `/status` `/src/…` `/map.js` `/map`
- `map.js` — the bridge the app page runs: watches the capture's fields, posts
  what changed, stops links and submits from navigating the capture
- `client.html` — the app page: the mark, the frame, the wait for `/status`
- `sync.js` — **gone.** Nothing runs inside the capture any more.
- `src/` — the captures, and their images and stylesheets
- `tests/` — the three suites above, run by `npm test`

## Note

The capture is a copy of a sign-in screen; the real sign-in happens in the
browser window. Keep `src/` and that window in step: if the page in the browser
moves on to another step, another capture is what should be in the frame.
