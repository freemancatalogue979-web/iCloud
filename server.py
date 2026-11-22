"""FastAPI server on port 9002.

Visiting http://localhost:9002 serves client.html. That page calls /start,
which opens a browser, loads icloud.com and clicks Sign In.

    python server.py
"""

import asyncio
import json
import logging
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, urljoin

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from playwright.async_api import async_playwright
from pydantic import BaseModel

HERE = Path(__file__).parent

# The Sign In button on the icloud.com landing page.
SIGNIN_BUTTON = "ui-button.sign-in-button"

# How long the page is given to settle after Sign In is pressed, before the
# first capture is taken.
#
# The first capture is a snapshot of the DOM, not a stream of changes, so it
# has to be taken when the page is the page the user is looking at. Right
# after the click the sign-in form is still arriving - it is a navigation,
# and the form itself is a cross-origin frame that has not answered yet - and
# a snapshot taken then is a half-built page that the mirror never recovers
# from on its own. Once the snapshot has been taken, the DOM is kept up to
# date by the mutation stream.
SETTLE_SECONDS = 4

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


class Click(BaseModel):
    x: float
    y: float


class Keys(BaseModel):
    text: str


class Key(BaseModel):
    key: str

pw = None
ctx = None
page = None

# The first capture is a snapshot taken after the page has settled. Until it
# is taken nothing is sent to the mirror: a viewer would seed itself from a
# half-loaded page and would then have to be told to start again.
_capture_ready = False

# Set once Sign In has been pressed, so that a tab opened by the sign-in flow
# can be recognised as the page to mirror.
_signin_clicked = False

# Injected into every page the browser loads. Mirrors the live DOM out to us.
AGENT_JS = (HERE / "agent.js").read_text(encoding="utf-8")

# Connected /ws viewers, each with its own queue.
clients: set[asyncio.Queue] = set()
last_doc: dict | None = None

# Frame identity is the agent's job, not the server's. Each frame is told its
# windowId by its parent over postMessage, the way SingleFile does it, and puts
# that id on every message. The server just relays.


async def broadcast(message: dict):
    for queue in list(clients):
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass  # a slow viewer drops frames rather than stalling the page


# Mirror telemetry. Without this a broken page is indistinguishable from a
# working one: the console shows only HTTP lines, and "the mirror is blank" and
# "the mirror is stale" need different fixes. All of it is throttled, because a
# live page mutates constantly and a log line per change buries everything.
_stats = {"ops": 0, "muts": 0, "resyncs": 0, "last_beat": 0.0}


def _describe(frames) -> str:
    """One line summarising a capture: enough to see a missing frame."""
    if not frames:
        return "0 frames"
    parts = []
    for frame in frames:
        size = len(frame.get("html") or "")
        parts.append(f"{frame.get('windowId')}:{size // 1024}KB")
    return f"{len(frames)} frame(s) [{', '.join(parts)}]"


async def on_emit(source, message):
    """Called from inside the real page via the Playwright binding."""
    global last_doc
    kind = message.get("kind")

    # Only the page being mirrored speaks for the mirror. Another tab in the
    # same context has its own agent and sends a document of its own under
    # the same windowId.
    if page is not None:
        source_page = getattr(source, "page", None)
        if source_page is not None and source_page is not page:
            return

    deliver = True

    if kind == "doc":
        if not _capture_ready:
            # The capture is deferred until the page has settled. An early one
            # would be a half-loaded page.
            return
        last_doc = message
        frames = message.get("frames") or []
        log(f"capture: {_describe(frames)}")
    elif kind == "mut":
        _stats["muts"] += 1
        _stats["ops"] += len(message.get("ops") or [])
        if not _capture_ready:
            # Nothing to apply them to. The snapshot taken at the end of the
            # settle window already contains these changes.
            deliver = False

    if deliver:
        await broadcast(message)

    now = time.monotonic()
    if now - _stats["last_beat"] >= 10.0 and _stats["muts"]:
        elapsed = now - _stats["last_beat"] if _stats["last_beat"] else 10.0
        log(
            f"stream: {_stats['muts']} updates, {_stats['ops']} ops, "
            f"{_stats['resyncs']} resyncs in {elapsed:.0f}s"
        )
        _stats.update(muts=0, ops=0, resyncs=0, last_beat=now)


async def install_agent(context):
    """Give every document the mirror script and a way to send it out."""
    await context.expose_binding("__rbEmit", on_emit)
    await context.add_init_script(AGENT_JS)


async def capture_now():
    """Take a snapshot of the page and send it to the mirror.

    The agent answers by emitting a document of its own, which is what the
    mirror seeds itself from. This is the deliberate first capture, not a
    recovery from a desync, so it is not counted as a resync.
    """
    if page is None:
        return
    try:
        await page.evaluate("() => window.__rbResync && window.__rbResync()")
    except Exception as exc:
        log(f"capture failed: {str(exc).splitlines()[0]}")


async def request_resync():
    """Ask the page to re-send its whole document."""
    if page is None:
        return
    _stats["resyncs"] += 1
    try:
        await page.evaluate("() => window.__rbResync && window.__rbResync()")
    except Exception as exc:
        log(f"resync failed: {str(exc).splitlines()[0]}")


async def follow_new_tab(new_page):
    """The sign-in form may open in a tab of its own.

    When it does, that tab is the page the user is looking at, so it is the
    page the mirror shows. The tab that was open before stays open, and its
    agent keeps sending documents - they are ignored, because they are not
    the page being mirrored.
    """
    global page
    if not _signin_clicked or new_page is page:
        return
    page = new_page
    log("the sign-in form opened in a new tab - mirroring that")
    try:
        await new_page.bring_to_front()
    except Exception:
        pass


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
            ctx = await pw.chromium.launch_persistent_context(
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

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        log(f"browser open ({label})")
        return ctx, page

    raise RuntimeError(" | ".join(reasons) or "no browser could be launched")


@app.get("/")
async def index():
    # no-store, so an edit to client.html shows up on refresh instead of
    # serving the browser's cached copy.
    return FileResponse(HERE / "client.html", headers={"Cache-Control": "no-store"})


@app.get("/start")
async def start():
    """Launch the browser, open iCloud, press Sign In, then capture.

    The capture is a snapshot, taken once the page has settled. Everything
    after that is a stream of changes.
    """
    global pw, ctx, page, _capture_ready, _signin_clicked

    if state["status"] in ("running", "done"):
        log(f"/start ignored, already {state['status']}")
        return state

    state.update(status="running", error="")
    _capture_ready = False
    _signin_clicked = False

    try:
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

        await install_agent(ctx)

        # The sign-in form may open in a tab of its own. From here on, a tab
        # that opens is the page the user is looking at.
        ctx.on("page", follow_new_tab)

        log("going to icloud.com")
        await page.goto("https://www.icloud.com/", wait_until="domcontentloaded")
        await page.bring_to_front()

        log("clicking Sign In")
        _signin_clicked = True
        await page.locator(SIGNIN_BUTTON).first.click(timeout=15000)

        # Let the page settle before taking the snapshot. The sign-in form is
        # a navigation and a cross-origin frame that has to arrive and
        # answer; a snapshot taken now would be of a half-built page.
        log(f"waiting {SETTLE_SECONDS}s for the page to settle")
        await asyncio.sleep(SETTLE_SECONDS)

        # The first capture: a snapshot of the settled page. From here on the
        # DOM is kept up to date by the mutation stream.
        _capture_ready = True
        await capture_now()

        state["status"] = "done"
        log("done - sign in yourself in the browser window")

    except Exception as exc:
        state["status"] = "error"
        state["error"] = str(exc).splitlines()[0]
        log(f"FAILED: {state['error']}")

    return state


@app.get("/status")
async def status():
    return state


# --------------------------------------------------------------------------
# Remote view: stream the real rendered page, forward input straight to it.
#
# Nothing here knows what page is loaded. There are no page-specific selectors
# and no copy of any site's markup - the client sees the live screenshot and
# its clicks and keystrokes land on whatever actually has focus.
# --------------------------------------------------------------------------
@app.get("/view")
async def view():
    return FileResponse(HERE / "view.html", headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------
# Resource proxy.
#
# The mirror is served from localhost, so it cannot just point at the real
# origin: fonts need CORS headers, and anything session-scoped needs the
# site's cookies. Fetching here through the browser context's own request
# client solves both, and lets us rewrite url()s inside proxied stylesheets so
# their relatives resolve against the sheet they came from.
# --------------------------------------------------------------------------
CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""")


def proxy_path(absolute: str) -> str:
    return "/res?url=" + quote(absolute, safe="")


def rewrite_css(css: str, base: str) -> str:
    """Point every url() in a proxied stylesheet back through the proxy."""

    def replace(match):
        quote_char = match.group(1)
        raw = match.group(2).strip()
        if raw.startswith(("data:", "blob:", "#", "/res?")):
            return match.group(0)
        absolute = urljoin(base, raw)
        return f'url("{proxy_path(absolute)}")'

    return CSS_URL_RE.sub(replace, css)


# Proxied assets are static in practice, and one page asks for the same font or
# sprite from several rules at once. Without a cache every mirror load re-fetches
# every asset through the browser's request API, and concurrent requests for the
# same URL each pay for it separately.
_RES_TTL = 600.0
_RES_MAX_ENTRIES = 256
_RES_MAX_BYTES = 8 * 1024 * 1024
_res_cache: "OrderedDict[str, tuple[float, bytes, str]]" = OrderedDict()
_res_inflight: dict[str, "asyncio.Future[tuple[bytes, str]]"] = {}


def _res_get(url: str):
    hit = _res_cache.get(url)
    if not hit:
        return None
    at, body, content_type = hit
    if time.monotonic() - at > _RES_TTL:
        _res_cache.pop(url, None)
        return None
    _res_cache.move_to_end(url)  # least recently used falls off first
    return body, content_type


def _res_put(url: str, body: bytes, content_type: str):
    if len(body) > _RES_MAX_BYTES:
        return  # one huge asset should not evict everything else
    _res_cache[url] = (time.monotonic(), body, content_type)
    _res_cache.move_to_end(url)
    while len(_res_cache) > _RES_MAX_ENTRIES:
        _res_cache.popitem(last=False)


async def _res_fetch(url: str) -> tuple[bytes, str]:
    """Fetch through the browser context and rewrite CSS url()s."""
    response = await ctx.request.get(url)
    body = await response.body()
    content_type = response.headers.get("content-type", "application/octet-stream")
    if "text/css" in content_type.lower():
        body = rewrite_css(body.decode(errors="replace"), url).encode("utf-8")
    return body, content_type


@app.get("/res")
async def proxy(url: str):
    # Validate before anything else, so this can never be pointed at a local
    # file or an internal address.
    if not url.startswith(("http://", "https://")):
        return Response(status_code=400, content=b"only http(s) urls may be proxied")
    if ctx is None:
        return Response(status_code=503, content=b"no browser session")

    cached = _res_get(url)
    if cached is None:
        # Several rules can ask for the same asset in the same instant; let the
        # first one fetch and the rest wait on it. A failure resolves the shared
        # future to None rather than raising or cancelling: CancelledError
        # descends from BaseException, so an `except Exception` in the waiter
        # would not catch it and it would escape the request handler.
        inflight = _res_inflight.get(url)
        if inflight is None:
            inflight = asyncio.get_running_loop().create_future()
            _res_inflight[url] = inflight
            try:
                fetched = await _res_fetch(url)
            except Exception as exc:
                reason = str(exc).splitlines()[0].encode()[:300]
                _res_inflight.pop(url, None)
                if not inflight.done():
                    inflight.set_result(None)
                return Response(status_code=502, content=reason)
            _res_inflight.pop(url, None)
            _res_put(url, *fetched)
            if not inflight.done():
                inflight.set_result(fetched)
            cached = fetched
        else:
            cached = await inflight
            if cached is None:
                return Response(status_code=502, content=b"upstream fetch failed")

    body, content_type = cached
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.websocket("/ws")
async def stream(websocket: WebSocket):
    """Push DOM documents and mutation patches to the mirror."""
    await websocket.accept()

    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    clients.add(queue)

    async def push():
        while True:
            message = await queue.get()
            await websocket.send_json(message)

    task = asyncio.create_task(push())

    try:
        # A viewer that joins late still gets the current document at once.
        if last_doc is not None:
            await websocket.send_json(last_doc)

        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("cmd") == "resync":
                await request_resync()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log(f"viewer disconnected: {str(exc).splitlines()[0]}")
    finally:
        task.cancel()
        clients.discard(queue)


@app.get("/shot.png")
async def shot():
    if page is None:
        return Response(status_code=404)
    try:
        png = await page.screenshot()
    except Exception as exc:
        log(f"screenshot failed: {str(exc).splitlines()[0]}")
        return Response(status_code=503)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/click")
async def click(where: Click):
    if page is None:
        return {"ok": False, "error": "no browser"}
    await page.mouse.click(where.x, where.y)
    return {"ok": True}


@app.post("/keys")
async def keys(body: Keys):
    """Type into whatever currently has focus."""
    if page is None:
        return {"ok": False, "error": "no browser"}
    await page.keyboard.type(body.text)
    return {"ok": True}


@app.post("/key")
async def key(body: Key):
    """A named key: Enter, Tab, Backspace, ArrowDown, ..."""
    if page is None:
        return {"ok": False, "error": "no browser"}
    await page.keyboard.press(body.key)
    return {"ok": True}


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools():
    """Chrome DevTools probes this on localhost pages. 204 keeps it quiet."""
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    log("server on http://localhost:9002")
    # One capture carries every frame's document in a single message, so the
    # default 1 MB WebSocket frame is far too small.
    uvicorn.run(app, host="0.0.0.0", port=9002, ws_max_size=64 * 1024 * 1024)
