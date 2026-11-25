"""What a browser will and will not do with a proxy.

This is the test that pins the reason the app speaks HTTP and not SOCKS5,
and that the browser that does the sign-in is the browser that is asked.

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

And the proxy has to be accepted by **the browser that will do the
sign-in**, which is the system's own Chrome where there is one. So the
browsers are asked in turn, each with a proxy that asks for a name and a
password and only goes on to the request when the credentials are right -
and each one has to answer.

If there is no browser to be had, the suite says so and is skipped.
"""

import asyncio
import base64
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browsers  # noqa: E402

passed = 0
failed = 0

USER = "alice"
PASSWORD = "the-secret-password"


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
    """A proxy that wants a name and a password, like a real one.

    It answers 407 with a challenge, and only goes on to the request when
    the credentials that come back are the right ones. Whatever it is sent
    is written down, so the suite can say what the browser did.
    """

    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *args):
        return

    def _authorized(self):
        head = self.headers.get("Proxy-Authorization", "")
        if not head.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(head[6:]).decode()
        except Exception:
            return False
        return raw == f"{USER}:{PASSWORD}"

    def _challenge(self):
        Recorder.seen.append((self.command, self.path, "407",
                              self.headers.get("Proxy-Authorization", "")))
        body = b"the proxy wants a name"
        self.send_response(407)
        self.send_header("Proxy-Authenticate", 'Basic realm="decodo"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _answer(self):
        Recorder.seen.append((self.command, self.path, "200",
                              self.headers.get("Proxy-Authorization", "")))
        body = b'{"ip":"203.0.113.9","country":"US"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorized():
            return self._challenge()
        return self._answer()

    do_CONNECT = do_GET


async def run():
    from playwright.async_api import async_playwright

    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    proxy = {"server": f"http://127.0.0.1:{port}",
             "username": USER, "password": PASSWORD}

    async with async_playwright() as pw:
        # ---- a SOCKS5 proxy with a password is refused ---------------
        refused = ""
        try:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
                proxy={"server": "socks5://gate.decodo.com:7000",
                       "username": USER, "password": PASSWORD},
            )
            await browser.close()
        except Exception as exc:
            refused = str(exc)
        check("a SOCKS5 proxy with a password is refused",
              "socks5" in refused.lower() and "authentication" in refused.lower(),
              refused)

        # ---- and each browser that will do the sign-in is asked ------
        for label, extra in browsers.candidates():
            if extra.get("channel") == "msedge":
                continue        # Edge is not on most machines
            path = extra.get("executable_path")
            if path and not Path(path).exists():
                continue
            Recorder.seen.clear()
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox"],
                    proxy=proxy,
                    **extra,
                )
            except Exception as exc:
                check(f"{label} takes the proxy", False,
                      str(exc).splitlines()[0][:70])
                continue
            page = await browser.new_page()
            body = ""
            try:
                await page.goto("http://ip.decodo.com/json", timeout=20000)
                body = await page.inner_text("body")
            except Exception:
                pass
            await browser.close()

            answered = any(code == "200" for _, _, code, _ in Recorder.seen)
            challenged = any(code == "407" for _, _, code, _ in Recorder.seen)
            check(f"{label} takes the proxy and answers the challenge",
                  answered and challenged and "203.0.113.9" in body,
                  f"answered={answered} challenged={challenged} body={body[:40]!r}")

            # ---- and the name is sent, not the address ----------------
            Recorder.seen.clear()
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox"],
                    proxy={"server": f"http://127.0.0.1:{port}"},
                    **extra,
                )
                page = await browser.new_page()
                try:
                    await page.goto("https://icloud.example.com/signin",
                                    timeout=20000)
                except Exception:
                    pass
                await browser.close()
            except Exception as exc:
                check(f"{label} sends the name, not the address", False,
                      str(exc).splitlines()[0][:70])
                continue

            # The CONNECT line carries the name or the address. The
            # address would mean this machine resolved it, and the
            # resolver's answers are the machine's own business.
            check(f"{label} sends the name, not the address",
                  any(method == "CONNECT" and target.startswith("icloud.example.com")
                      for method, target, _, _ in Recorder.seen),
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
