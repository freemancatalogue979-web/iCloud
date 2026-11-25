"""The copy is asked the question, and asks the server what it is.

The copy of the pages that lives somewhere else is asked whether the
visitor is a person before a browser is opened, exactly as the server's
own pages are. What makes that possible without unpacking the zip again
is that the copy asks the server for the question when the page loads:
the site's key is public, and the server is the one that has it.

This is proved end to end, in a browser, across two hosts:

  * the copy is served from a host of its own;
  * the server is a stand-in that answers with a key, and records what it
    is asked;
  * Cloudflare is a stand-in that answers the question with a token.

What has to be true at the end: the copy asked the server for the
question, it used the key the server gave it, and it sent the answer back
with the request that opens the browser.

Run with: python tests/test_standalone_question.py
"""

import asyncio
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import standalone  # noqa: E402

passed = 0
failed = 0

SITE_KEY = "0x4AAAA-the-key-the-server-gives"
TOKEN = "a-good-token"

# The question asked of the copy, in place of Cloudflare's own.
CLOUDFLARE = """
window.turnstile = {
  render: function (where, options) {
    window.__rendered = { where: where, options: options };
    setTimeout(function () { options.callback("a-good-token"); }, 10);
    return "widget-id";
  }
};
"""


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + str(detail)) if detail else ""))


def serve(folder: Path):
    """A server of our own for the copy to be opened from."""
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(folder), **k)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


class TheServer:
    """The server the copy talks to: the question, and the answer's road home.

    It is a stand-in, so it says what it was asked and what it was sent.
    """

    def __init__(self):
        self.asked = []          # the paths it was asked for
        self.tokens = []         # the answers it was given
        self.http = None
        self.port = 0

    def start(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type, x-icloud-challenge")

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                path = self.path.split("?")[0]
                outer.asked.append(path)
                # The answer travels in a header, on the request that opens
                # the browser - which is a GET.
                if path == "/start":
                    outer.tokens.append(
                        self.headers.get("x-icloud-challenge", ""))
                if path == "/turnstile/sitekey":
                    body = json.dumps({"site_key": SITE_KEY,
                                       "asked": True}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self._cors()
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"status": "idle",
                                    "page": "/src/1.html"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                path = self.path.split("?")[0]
                outer.asked.append(path)
                outer.tokens.append(
                    self.headers.get("x-icloud-challenge", ""))
                body = json.dumps({"status": "idle",
                                    "page": "/src/1.html"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.http:
            self.http.shutdown()


async def main():
    from playwright.async_api import async_playwright

    server = TheServer().start()
    try:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            data = standalone.build(f"http://127.0.0.1:{server.port}")
            with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
                zip_file.extractall(folder)

            launcher = (folder / "client.html").read_text(encoding="utf-8")
            check("the copy carries no key of its own",
                  SITE_KEY not in launcher and
                  'name="icloud-turnstile-sitekey"' not in launcher,
                  launcher[:200])

            port = serve(folder)
            playwright = await async_playwright().start()
            try:
                browser = await playwright.chromium.launch(headless=True,
                                                          args=["--no-sandbox"])
                page = await browser.new_page()
                # Cloudflare, in place of the real one.
                await page.route("https://challenges.cloudflare.com/**",
                                 lambda route: route.fulfill(
                                     status=200, content_type="application/javascript",
                                     body=CLOUDFLARE))
                await page.goto(f"http://127.0.0.1:{port}/client.html",
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)

                check("the copy asked the server what the question is",
                      "/turnstile/sitekey" in server.asked, server.asked)

                rendered = await page.evaluate("() => window.__rendered")
                check("...and asked the question with the key the server gave",
                      rendered and rendered["options"]["sitekey"] == SITE_KEY,
                      rendered)

                check("the copy sent the answer with the request that opens "
                      "the browser",
                      "/start" in server.asked and TOKEN in server.tokens,
                      (server.asked, server.tokens))

                await browser.close()
            finally:
                await playwright.stop()
    finally:
        server.stop()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
