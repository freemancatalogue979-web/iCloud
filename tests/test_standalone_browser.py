"""The standalone copy, in a browser.

The copy has to work on a host that is not the server's: the engine has to
find the server, the pages have to call the server's own names for them,
and a page the server asks for has to be the page in the folder the copy
is in.

There is no server here to talk to - so the socket is a recorder that
says what it was called with, and the page is opened from a server of its
own. What is being proved is the copy's side of the conversation: where it
reaches for, and what it does with what it is told.

Run with: python tests/test_standalone_browser.py
"""

import asyncio
import http.server
import io
import os
import socket
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import standalone  # noqa: E402

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


# The server the copy will be told to talk to. It is not running: what
# matters is where the copy reaches for.
SERVER = "http://127.0.0.1:9002"

# What the recorder sees, and what the page is told.
RECORDER = """
window.__seen = { socket: "", beacons: [], hello: null, sent: [] };
// This browser has been through the launcher: it knows what it is called,
// and the capture in front of it is for this browser.
try {
  window.localStorage.setItem("icloud-user", "the-user");
  window.localStorage.setItem("icloud-visitor", "the-session");
} catch (err) { /* no storage */ }
(function () {
  var Real = window.WebSocket;
  window.WebSocket = function (url) {
    window.__seen.socket = String(url);
    var fake = {
      readyState: 0,
      sent: [],
      send: function (data) {
        this.sent.push(JSON.parse(data));
        if (JSON.parse(data).t === "hello") {
          window.__seen.hello = JSON.parse(data);
        }
      },
      close: function () {},
      set onopen(fn) { this._open = fn; },
      get onopen() { return this._open; },
      set onmessage(fn) { this._message = fn; },
      get onmessage() { return this._message; },
      set onclose(fn) { this._close = fn; },
      get onclose() { return this._close; },
      set onerror(fn) { this._error = fn; },
      get onerror() { return this._error; },
    };
    window.__seen.fake = fake;
    setTimeout(function () { if (fake._open) fake._open({}); }, 30);
    return fake;
  };
  window.WebSocket.prototype = Real && Real.prototype;
  window.navigator.sendBeacon = function (url) {
    window.__seen.beacons.push(String(url));
    return true;
  };
})();
"""


def serve(folder: Path):
    """A server of our own for the copy to be opened from."""
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(folder), **k)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


async def main():
    from playwright.async_api import async_playwright

    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        data = standalone.build(SERVER)
        with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
            zip_file.extractall(folder)

        check("the copy is unpacked",
              (folder / "client.html").is_file()
              and (folder / "src" / "sync.js").is_file())

        port = serve(folder)
        # The names are in the address: a capture is for one browser and one
        # session, and says so in the address it is asked for by.
        page_url = (f"http://127.0.0.1:{port}/src/1.html"
                    f"?uid=the-user&sid=the-session")

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(headless=True,
                                                      args=["--no-sandbox"])
            page = await browser.new_page()
            await page.add_init_script(RECORDER)
            await page.goto(page_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)

            seen = await page.evaluate("() => window.__seen")

            check("the copy reaches for the server, not for itself",
                  seen["socket"].startswith("ws://127.0.0.1:9002/ws"),
                  seen["socket"])
            check("...and says who the visitor is",
                  "visitor=" in seen["socket"], seen["socket"])

            hello = seen["hello"]
            check("...and says hello with the page the server knows it by",
                  hello and hello["page"] == "/src/1.html",
                  hello)

            # The server asks for a page by its own name; the copy has to
            # ask for the one in its own folder.
            await page.evaluate("""() => {
              var fake = window.__seen.fake;
              if (fake && fake._message) {
                fake._message({ data: JSON.stringify({ t: "nav", page: "/src/4.html" }) });
              }
            }""")
            await page.wait_for_timeout(800)
            where = page.url
            check("a page the server asks for is the one in the copy's folder",
                  where.split("?")[0].endswith(f"/src/4.html"), where)
            check("...and it kept the folder it was in",
                  f":{port}/src/4.html" in where, where)
            check("...and the names went with it",
                  "uid=the-user" in where and "sid=the-session" in where, where)

            # The beacon goes to the server too. The captures have no link
            # to each other - the flow is driven by the server - so the link
            # is put there for this.
            # The beacon is sent as the link is clicked, and the click
            # navigates - so what was said is read in the same breath.
            beacons = await page.evaluate("""() => {
              var link = document.createElement("a");
              link.href = "/src/2.html";
              link.textContent = "on";
              document.body.appendChild(link);
              link.click();
              return window.__seen.beacons;
            }""")
            check("a link click is reported to the server",
                  any(b.startswith(SERVER + "/nav") for b in beacons),
                  beacons)

            await browser.close()
        finally:
            await playwright.stop()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
