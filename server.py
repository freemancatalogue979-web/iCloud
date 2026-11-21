"""FastAPI server on port 9002.

Visiting http://localhost:9002 serves client.html. That page immediately calls
/start, which opens a browser, loads icloud.com and clicks Sign In.

    python server.py
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from playwright.async_api import async_playwright

HERE = Path(__file__).parent

# The Sign In button on the icloud.com landing page.
SIGNIN_BUTTON = "ui-button.sign-in-button"

app = FastAPI()

state = {
    "status": "idle",      # idle | running | done | error
    "step": "",
    "url": "",
    "error": "",
}

pw = None
browser = None
page = None


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
        state["step"] = "launching browser"
        pw = await async_playwright().start()

        # A persistent profile means you sign in once, not every run.
        browser = await pw.chromium.launch_persistent_context(
            str(HERE / "profile"), headless=False
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        state["step"] = "opening icloud.com"
        await page.goto("https://www.icloud.com/", wait_until="domcontentloaded")
        state["url"] = page.url

        state["step"] = "clicking Sign In"
        await page.locator(SIGNIN_BUTTON).first.click(timeout=15000)

        await page.wait_for_timeout(3000)
        state["url"] = page.url
        state["status"] = "done"
        state["step"] = "Sign In clicked - now sign in yourself in the browser window"

    except Exception as exc:
        state["status"] = "error"
        state["step"] = "failed"
        state["error"] = str(exc).splitlines()[0]

    return state


@app.get("/status")
async def status():
    if page is not None:
        try:
            state["url"] = page.url
        except Exception:
            pass
    return state


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9002)
