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
from pathlib import Path
from urllib.parse import quote, urljoin

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from playwright.async_api import async_playwright
from pydantic import BaseModel

HERE = Path(__file__).parent

# The Sign In button on the icloud.com landing page.
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


async def on_emit(source, message):
    """Called from inside the real page via the Playwright binding."""
    global last_doc
    if message.get("kind") == "doc":
        last_doc = message
    await broadcast(message)


async def install_agent(context):
    """Give every document the mirror script and a way to send it out."""
    await context.expose_binding("__rbEmit", on_emit)
    await context.add_init_script(AGENT_JS)


async def request_resync():
    """Ask the page to re-send its whole document."""
    if page is None:
        return
    try:
        await page.evaluate("() => window.__rbResync && window.__rbResync()")
    except Exception as exc:
        log(f"resync failed: {str(exc).splitlines()[0]}")


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
    """Launch the browser, open iCloud, press Sign In."""
    global pw, ctx, page

    if state["status"] in ("running", "done"):
        log(f"/start ignored, already {state['status']}")
        return state

    state.update(status="running", error="")

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


@app.get("/res")
async def proxy(url: str):
    # Validate before anything else, so this can never be pointed at a local
    # file or an internal address.
    if not url.startswith(("http://", "https://")):
        return Response(status_code=400, content=b"only http(s) urls may be proxied")
    if ctx is None:
        return Response(status_code=503, content=b"no browser session")

    try:
        # Shares cookies with the driven browser, so authenticated assets work.
        response = await ctx.request.get(url)
    except Exception as exc:
        return Response(status_code=502, content=str(exc).splitlines()[0].encode()[:300])

    body = await response.body()
    content_type = response.headers.get("content-type", "application/octet-stream")

    if "text/css" in content_type.lower():
        body = rewrite_css(body.decode(errors="replace"), url).encode("utf-8")

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
