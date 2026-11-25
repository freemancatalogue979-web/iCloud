"""What a browser will and will not do with a proxy.

This is the test that pins the reason the app speaks HTTP and not SOCKS5.

Chromium cannot use a SOCKS5 proxy with a password. Playwright says so
before the browser is even opened - "Browser does not support socks5 proxy
authentication" - and it says it for every browser, because the check is
made on the way in and not by the browser at all.

So a proxy that is authenticated by name and password has to be reached
over HTTP. Which is fine, and for a reason worth keeping: Chromium does not
resolve names locally when it has an HTTP proxy. It sends the *name*:

    CONNECT icloud.com:443

and the proxy resolves it. This machine's resolver is not asked, so the
DNS leak that made SOCKS5 look necessary is not there with HTTP either.

Both of those are proved here with a real browser, against a small proxy of
our own that records what it is sent. If Chromium ever stops sending the
name, or ever starts accepting a SOCKS5 password, this test will say so.

If there is no browser to be had, the suite says so and is skipped.
"""

import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   {name}")
    else:
        failed += 1
        print(f"FAIL {name} {detail}")


def browser_path():
    """A browser to test with, or None.

    Found by looking at the disk, not by asking Playwright: Playwright's
    own way of answering needs an event loop, and the test that asks for
    this is often already inside one.
    """
    roots = []
    given = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if given:
        roots.append(Path(given))
    roots.append(Path.home() / ".cache" / "ms-playwright")

    names = ("chrome-linux64/chrome", "chrome-linux/chrome",
             "chrome-headless-shell-linux64/chrome-headless-shell",
             "chrome-linux/headless_shell", "chrome", "headless_shell")
    # A Chrome of its own, if the machine has one.
    for system in ("/usr/bin/google-chrome", "/usr/bin/chromium",
                   "/usr/bin/chromium-browser",
                   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if Path(system).exists():
            roots.append(Path(system).parent)

    for root in roots:
        if not root.exists():
            continue
        for folder in sorted(root.glob("chromium*"), reverse=True):
            for name in names:
                found = folder / name
                if found.exists():
                    return str(found)
    return None


class Recorder(BaseHTTPRequestHandler):
    """A proxy that only writes down what it was sent."""

    seen = []

    def log_message(self, *args):
        return

    def do_CONNECT(self):
        Recorder.seen.append(("CONNECT", self.path))
        self.send_response(502)
        self.end_headers()

    def do_GET(self):
        Recorder.seen.append(("GET", self.path))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html>ok</html>")


async def run():
    from playwright.async_api import async_playwright

    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async with async_playwright() as pw:
        # ---- a SOCKS5 proxy with a password is refused ---------------
        refused = ""
        try:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
                proxy={"server": "socks5://gate.decodo.com:7000",
                       "username": "alice", "password": "the-secret"},
            )
            await browser.close()
        except Exception as exc:
            refused = str(exc)
        check("a SOCKS5 proxy with a password is refused",
              "socks5" in refused.lower() and "authentication" in refused.lower(),
              refused)

        # ---- and the same proxy over HTTP is accepted ---------------
        Recorder.seen.clear()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
            proxy={"server": f"http://127.0.0.1:{port}",
                   "username": "alice", "password": "the-secret"},
        )
        page = await browser.new_page()
        try:
            await page.goto("https://icloud.example.com/signin", timeout=15000)
        except Exception:
            pass
        await browser.close()

        check("an HTTP proxy with a password is accepted",
              True, "")
        check("...and the name is sent, not the address",
              any(kind == "CONNECT" and target.startswith("icloud.example.com")
                  for kind, target in Recorder.seen),
              Recorder.seen)


def main():
    executable = browser_path()
    if not executable or not Path(executable).exists():
        print("SKIPPED: no Chromium to test with")
        print(f"\n{passed} passed, {failed} failed")
        return 0

    asyncio.run(run())
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
