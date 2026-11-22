"""FastAPI server on port 9002.

Visiting http://localhost:9002 serves client.html: a black screen with the mark,
and then the captured sign-in page in a frame of its own.

What the app does:

* it opens a real browser, goes to icloud.com and presses Sign In;
* it waits until the sign-in form is actually there - the account field - and
  only then shows the capture;
* it maps the capture onto that page: what is typed into the field in the
  frame is written into the real field in the browser, and a click on Continue
  is a click on the real Continue.

There is no mirror and no capture pipeline. ``src/`` holds the HTML that was
captured earlier, and it is served exactly as it is, byte for byte: no script
added, no meta rewritten, no policy touched. The mapping lives in the app page
and in this server, not in the capture.

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
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response

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
    "form_up": False,
}

# The real browser. It is opened so the user can sign in; it is not mirrored.
pw = None
ctx = None
page = None

# The element the capture is answering: the account field of the sign-in form.
# iCloud renders that form inside a widget iframe, so it is looked for in every
# frame of the browser, not just in the top document.
ACCOUNT_FIELD = "#account_name_text_field"

# How long to wait for the form before showing the capture anyway.
WAIT_FOR_FORM = 90.0

# A capture is served exactly as it was captured: nothing added, nothing
# removed, nothing rewritten. There is no write path for src/ at all.

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
    """The app: the mark, then the capture in a frame, mapped to the browser.

    It is the same document before and after the browser is up, so a reload is
    just a reload: no redirect, no second window, and the frame is the capture.
    """
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

    state.update(status="running", error="", browser_open=False, form_up=False)

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

        # The capture is the sign-in screen, so it is shown once the real one
        # is on screen - not on a timer.
        log(f"waiting for {ACCOUNT_FIELD} in the browser")
        if await wait_for_form(ACCOUNT_FIELD):
            state["form_up"] = True
            log("the sign-in form is up")
        else:
            log("the sign-in form did not appear - showing the capture anyway")

        state["status"] = "done"

    except Exception as exc:
        state["status"] = "error"
        state["error"] = str(exc).splitlines()[0]
        log(f"FAILED: {state['error']}")

    return state


@app.get("/status")
async def status():
    return {**state, "page": START_PAGE}


# --------------------------------------------------------------------------
# The captured pages
#
# src/ is served as it is. The file on disk is never written to, and the
# document is not rewritten: scripts, styles, metas, policy and markup all go
# out as they came in. What the app does with the page happens in the app page
# and in this server, never in the capture.
# --------------------------------------------------------------------------

_HTML_SUFFIXES = (".html", ".htm")


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
        "<p>The app opens <code>1.html</code> in a frame once the browser has "
        "the sign-in form. Each file is served exactly as it was captured; "
        "what is typed into it is written into the real page in the browser "
        "window.</p>"
        + page_links(folder)
    )
    return page_document("Captured pages", body)


def missing_page(path: str, folder: Path) -> str:
    where = (
        f"<code>src/{html_module.escape(path)}</code>" if path else "<code>src/</code>"
    )
    body = (
        f"<h1>Nothing captured at {where} yet</h1>"
        "<p>This route serves the HTML files that were captured earlier, byte "
        "for byte, along with any images or stylesheets next to them. Drop the "
        "capture into <code>src/</code> - starting with <code>1.html</code> - "
        "and reload.</p>"
        + page_links(folder)
    )
    return page_document("No captured page", body)


@app.get("/src")
@app.get("/src/{path:path}")
async def captured(path: str = ""):
    """Serve a captured page, or one of its images, exactly as it is."""
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
        # The file, byte for byte.
        return Response(
            content=target.read_bytes(),
            media_type="text/html",
            headers=NO_STORE,
        )

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return Response(
        content=target.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/map.js")
async def map_js():
    """The bridge the app page runs: it watches the frame, not the capture."""
    return FileResponse(
        HERE / "map.js",
        media_type="text/javascript",
        headers=NO_STORE,
    )


# --------------------------------------------------------------------------
# Mapping the capture onto the browser
#
# The capture is a copy of the sign-in screen; the real one is in the browser
# window. What is typed into the copy is written into the real page - the same
# element id, in whichever frame of the browser has it - so the two screens say
# the same thing.
#
# Nothing is injected into the capture to do this. It is shown in a frame of
# the app page, which is ours, so that page watches the field and posts what
# changed here; this side does the writing.
# --------------------------------------------------------------------------

_FIELD_ID = re.compile(r"^[A-Za-z][\w-]{0,80}$")

_FIND = "(sel) => !!document.querySelector(sel)"

# Written through the prototype setter, then announced with input and change:
# the widget's framework listens for those events and ignores a plain
# assignment, so `el.value = x` alone would leave the page looking empty.
_SET = """([sel, value]) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(proto.prototype, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}"""

_CLICK = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  el.click();
  return true;
}"""


def field_selector(field) -> str:
    """What a field in the capture maps to in the real page.

    The captures were taken from the real page and kept its ids, so a field
    maps to the element with the same id. That is the whole mapping.
    """
    if not isinstance(field, str) or not _FIELD_ID.match(field):
        return ""
    return "#" + field


def live_frames() -> list:
    """Every frame of every page the browser has open.

    The sign-in form is not in icloud.com's own document - it is a widget
    iframe - and the sign-in flow may open a tab of its own, so neither the
    top document nor the first page is necessarily where the field is.
    """
    pages = list(ctx.pages) if ctx is not None else ([page] if page is not None else [])
    frames = []
    for browser_page in pages:
        try:
            frames.extend(browser_page.frames)
        except Exception:
            continue
    return frames


async def in_frames(script: str, argument) -> bool:
    """Run a script in each frame until one of them answers True."""
    for frame in live_frames():
        try:
            if await frame.evaluate(script, argument):
                return True
        except Exception:
            # A frame that is navigating, detached or crashed is not an error
            # worth passing on: the field is somewhere, and the next frame
            # may be where it is.
            continue
    return False


async def field_is_there(selector: str) -> bool:
    return await in_frames(_FIND, selector)


async def write_field(selector: str, value: str) -> bool:
    return await in_frames(_SET, [selector, value])


async def click_field(selector: str) -> bool:
    return await in_frames(_CLICK, selector)


async def wait_for_form(selector: str, timeout: float = WAIT_FOR_FORM) -> bool:
    """Wait until the sign-in form is on screen in the real browser."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await field_is_there(selector):
            return True
        await asyncio.sleep(0.3)
    return False


@app.post("/map")
async def map_to_browser(request: Request):
    """One thing done in the capture, written into the real page."""
    try:
        message = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(status_code=400)

    if not isinstance(message, dict):
        return Response(status_code=400)

    selector = field_selector(message.get("field"))
    if not selector:
        return Response(status_code=400)

    if message.get("click"):
        mapped = await click_field(selector)
    else:
        value = message.get("value")
        mapped = await write_field(
            selector, value[:10000] if isinstance(value, str) else ""
        )

    return {"ok": mapped, "field": selector[1:]}


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools():
    """Chrome DevTools probes this on localhost pages. 204 keeps it quiet."""
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    log("server on http://localhost:9002")
    uvicorn.run(app, host="0.0.0.0", port=9002)
