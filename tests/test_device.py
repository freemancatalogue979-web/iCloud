"""The browser, and what it says about the machine it is on.

This is the test that proves the device is real rather than written down:
it opens a browser the way the app opens one, with the identity the app
would give it, and asks the page what the browser thinks it is.

What it looks for:

  * the browser does not say it is being driven,
  * the language and the clock are the ones the device was given,
  * the pixels are the machine's, and
  * the name the browser calls itself is its own - not one that was put
    there, because the client hints would say otherwise.

Run with: python tests/test_device.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

scratch = tempfile.mkdtemp(prefix="icloud-device-")
os.environ["ICLOUD_PROFILES"] = os.path.join(scratch, "visitors")
os.environ["ICLOUD_STATE"] = os.path.join(scratch, "state")
os.environ.setdefault("ICLOUD_PROXY", "none")

import server  # noqa: E402
import identity as identity_module  # noqa: E402
import stealth  # noqa: E402

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + str(detail)) if detail else ""))


REPORT = """
() => ({
    webdriver: navigator.webdriver,
    language: navigator.language,
    languages: (navigator.languages || []).join(","),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    ratio: window.devicePixelRatio,
    screen: `${window.screen.width}x${window.screen.height}`,
    agent: navigator.userAgent,
})
"""


def _little_server() -> int:
    """A server of our own, so that a request carries real headers.

    There has to be something to ask: a page opened from nowhere sends no
    headers at all, and the client hints are on the request.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>ok</body></html>")

        def log_message(self, *args):
            return

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


async def main():
    from playwright.async_api import async_playwright

    session = server.Session("device-test")
    session.identity = identity_module.Identity(
        id="id_test",
        group="us-0",
        country="US",
        device=identity_module.Device(
            user_agent="",
            platform="Linux x86_64",
            screen={"width": 1440, "height": 900},
            dpr=1.25,
            locale="de-DE",
            timezone="Europe/Berlin",
            color_scheme="dark",
        ),
    )

    playwright = await async_playwright().start()
    session.pw = playwright
    # The identity is kept with the profile, which is where the app keeps
    # it too - so that what is learned about the browser is learned once.
    identity_module.save(session.profile(), session.identity)
    try:
        session.ctx, session.page = await server.launch(session)
    except Exception as exc:
        print("FAIL the browser did not open  " + str(exc).splitlines()[0])
        print(f"\n{passed} passed, {failed} failed")
        await playwright.stop()
        return 1

    try:
        await session.page.goto("about:blank")
        said = await session.page.evaluate(REPORT)

        check("the browser does not say it is being driven",
              said["webdriver"] is False, said["webdriver"])
        check("the language is the machine's",
              said["language"] == "de-DE", said["language"])
        check("the clock is the machine's",
              said["timezone"] == "Europe/Berlin", said["timezone"])
        check("the pixels are the machine's",
              abs(float(said["ratio"]) - 1.25) < 0.01, said["ratio"])
        check("the browser calls itself by its own name",
              "Chrome/" in said["agent"], said["agent"])

        # What the browser says about itself is kept with the profile.
        kept = identity_module.update_user_agent(session.profile(), said["agent"])
        check("the browser's own name is kept with the profile",
              kept.device.user_agent == said["agent"], kept.device.user_agent)

        # And the name it was given and the hints it sent agree - or the
        # panel is told that they do not. The headers are only sent on a
        # real request, so a small server of our own is asked.
        headers = {}
        session.page.on("request", lambda request: headers.update(
            {k.lower(): v for k, v in request.headers.items()}))
        server_on = _little_server()
        try:
            await session.page.goto(f"http://127.0.0.1:{server_on}/")
        except Exception:
            pass
        agreed = stealth.agree(headers, kept.device.user_agent)
        check("the name and the client hints do not contradict each other",
              agreed["ok"], agreed)
        check("the request carried a client hint at all",
              "sec-ch-ua" in headers, sorted(headers)[:8])
    finally:
        try:
            await session.ctx.close()
        except Exception:
            pass
        await playwright.stop()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
