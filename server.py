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
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

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

app = FastAPI()

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

# The one script a captured page gets, put into it as it is served rather than
# written into the file on disk. The file stays exactly what was captured.
SYNC_TAG = '<script src="/sync.js"></script>'

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
    return {**state, "page": hub.page}


# --------------------------------------------------------------------------
# The captured pages
#
# src/ is served as it is, with one addition: every HTML page gets /sync.js in
# its head. That script is how what the user does in the page reaches the
# server. Without it the page is just a file and nothing leaves the tab.
# --------------------------------------------------------------------------

_HEAD_END = re.compile(r"</head\s*>", re.IGNORECASE)
_BODY_OPEN = re.compile(r"<body[^>]*>", re.IGNORECASE)
_HTML_SUFFIXES = (".html", ".htm")


def inject_sync(page_html: str) -> str:
    """Put the sync engine into a captured page, once."""
    if "/sync.js" in page_html:
        return page_html

    match = _HEAD_END.search(page_html)
    if match:
        return page_html[: match.start()] + SYNC_TAG + page_html[match.start():]

    # A capture with no head still has to run the engine. Before the body is
    # as good a place as any: the script defers its DOM work until the
    # document has been parsed.
    match = _BODY_OPEN.search(page_html)
    if match:
        return page_html[: match.end()] + SYNC_TAG + page_html[match.end():]

    return SYNC_TAG + page_html


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
    folder = SRC
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
        return HTMLResponse(inject_sync(source), headers=NO_STORE)

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
    """The page everyone is on, and what has been done to it."""

    def __init__(self):
        self.viewers: set[Viewer] = set()
        self.page = START_PAGE
        self.values: dict[str, dict] = {}

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

    def leave(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)
        viewer.close()

    def broadcast(self, message: dict, sender: Viewer | None = None) -> None:
        for viewer in list(self.viewers):
            if viewer is not sender:
                viewer.send(message)

    def navigate(self, page, sender: Viewer | None = None) -> bool:
        """Follow the viewer that moved to another captured page."""
        page = clean_page(page)
        if not page or page == self.page:
            return False
        self.page = page
        # Form values belong to the page they were typed on.
        self.values = {}
        log(f"page: {page}")
        self.broadcast({"t": "nav", "page": page}, sender=sender)
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


hub = Hub()


async def sync_socket(websocket: WebSocket):
    """The captured page's line to the server."""
    await websocket.accept()
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
