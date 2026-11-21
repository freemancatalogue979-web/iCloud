"""FastAPI server on port 9002.

Visiting http://localhost:9002 serves client.html. That page calls /start,
which opens a browser, loads icloud.com and clicks Sign In.

    python server.py
"""

import asyncio
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from playwright.async_api import async_playwright

HERE = Path(__file__).parent

# The Sign In button on the icloud.com landing page.
SIGNIN_BUTTON = "ui-button.sign-in-button"

app = FastAPI()

state = {
    "status": "idle",      # idle | running | done | error
    "step": "",
    "error": "",
}

pw = None
browser = None
page = None


async def install_chromium():
    """Download Chromium. Runs once; Playwright skips it if already present."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(pick_reason(err.decode(errors="replace")))


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
    """Open a persistent browser so you only sign in once."""
    return await pw.chromium.launch_persistent_context(
        str(HERE / "profile"), headless=False
    )


@app.get("/")
async def index():
    return FileResponse(HERE / "client.html")


@app.get("/start")
async def start():
    """Launch the browser, open iCloud, press Sign In."""
    global pw, browser, page

    if state["status"] in ("running", "done"):
        return state

    state.update(status="running", error="")

    try:
        pw = await async_playwright().start()

        state["step"] = "launching browser"
        try:
            browser = await launch()
        except Exception as exc:
            # No browser downloaded yet - get it, then try once more.
            if "Executable doesn't exist" not in str(exc):
                raise
            state["step"] = "installing browser"
            await install_chromium()
            browser = await launch()

        page = browser.pages[0] if browser.pages else await browser.new_page()

        state["step"] = "opening icloud.com"
        await page.goto("https://www.icloud.com/", wait_until="domcontentloaded")

        state["step"] = "clicking Sign In"
        await page.locator(SIGNIN_BUTTON).first.click(timeout=15000)

        state["status"] = "done"

    except Exception as exc:
        state["status"] = "error"
        state["error"] = str(exc).splitlines()[0]

    return state


@app.get("/status")
async def status():
    return state


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools():
    """Chrome DevTools probes this on localhost pages.

    It is not part of the app. Answering 204 keeps it out of the log instead
    of throwing a 404 on every page load.
    """
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9002)
