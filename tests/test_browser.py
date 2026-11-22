"""The whole way round, in a real browser.

Run with: python tests/test_browser.py   (or `npm test`)

This is the one test that needs a browser, and it is the one that proves the
thing the app is for: a capture, served from src/, that can be typed into and
that writes what is typed into the browser the server opened.

The capture it uses is a fixture, not one of the real ones: a page with the
policy a SingleFile capture carries, and the sign-in form in a frame that is
sandboxed without allow-same-origin. That is the shape that used to fail -
the engine was refused by the policy, and the form was in a frame the page
could not reach.

If there is no browser to be had, the suite says so and is skipped.
"""

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402
import server  # noqa: E402

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


# The capture the test serves: the policy a SingleFile capture carries, and
# the form in a frame that is sandboxed without allow-same-origin.
FIXTURE = """<!DOCTYPE html><html lang=en-us dir=ltr>
<meta charset=utf-8><meta name=viewport content="width=device-width">
<meta http-equiv=content-security-policy content="default-src 'none'; font-src 'self' data:; img-src 'self' data:; style-src 'unsafe-inline'; media-src 'self' data:; script-src 'unsafe-inline' data:; object-src 'self' data:; frame-src 'self' data:; form-action 'none'; base-uri 'none';">
<title>iCloud</title><style>.sf-hidden{display:none!important}</style>
<body class=clicking><div id=root><header>logo</header>
<iframe id=aid-auth-widget-iFrame width=100% height=365 sandbox="allow-popups allow-scripts allow-modals" srcdoc="&lt;!DOCTYPE html&gt; &lt;html dir=ltr&gt;&lt;head&gt;&lt;meta http-equiv=content-security-policy content=&quot;default-src 'none'&quot;&gt;&lt;/head&gt;&lt;body class=tk-body&gt;&lt;div class=signin-v2&gt;&lt;input type=text id=account_name_text_field value=.&gt;&lt;button id=sign-in&gt;Continue&lt;/button&gt;&lt;/div&gt;&lt;/body&gt;&lt;/html&gt;"></iframe>
</div></body></html>"""


class FakeFrame:
    """The frame of the browser that holds the form."""

    def __init__(self):
        self.ids = {"account_name_text_field", "sign-in"}
        self.fields = {"account_name_text_field": ""}
        self.url = "https://idmsa.apple.com/signin"
        self.title = "Sign in to your Apple Account"
        self.written = []
        self.pressed = []

    async def evaluate(self, script, argument=None):
        if "ids.slice" in script:
            return list(self.ids)

        if "querySelectorAll" in script:
            return {
                "url": self.url,
                "title": self.title,
                "fields": {
                    name: {"value": value, "checked": False}
                    for name, value in self.fields.items()
                },
            }
        if "el.click()" in script:
            self.pressed.append(argument)
            return argument in self.ids
        element, value = argument
        self.written.append((element, value))
        if element in self.ids:
            self.fields[element] = value
            return True
        return False


class FakePage:
    def __init__(self, frames):
        self.frames = frames


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def browser_path():
    """A browser to test with, or None."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    candidates = []
    try:
        with sync_playwright() as p:
            candidates.append(p.chromium.executable_path)
    except Exception:
        pass

    # Playwright's own download, when the headless shell is not there.
    roots = []
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        roots.append(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    roots.append(Path("/root/.cache/ms-playwright"))
    for cache in roots:
        if not cache.is_dir():
            continue
        for found in sorted(cache.glob("chromium-*/chrome-linux/chrome")):
            candidates.append(str(found))
        for found in sorted(cache.glob("chromium-*/chrome-linux/headless_shell")):
            candidates.append(str(found))

    for system in ("/usr/bin/google-chrome", "/usr/bin/chromium",
                   "/usr/bin/chromium-browser", "/Applications/Google Chrome.app/"
                   "Contents/MacOS/Google Chrome"):
        candidates.append(system)

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def main():
    global passed, failed

    root = Path(tempfile.mkdtemp()).resolve()
    (root / "1.html").write_text(FIXTURE, encoding="utf-8")
    server.SRC = root

    frame = FakeFrame()
    live.attach(FakeContext([FakePage([frame])]), None)

    executable = browser_path()
    if not executable or not Path(executable).exists():
        print("SKIPPED: no Chromium to test with")
        print(f"\n{passed} passed, {failed} failed")
        return 0

    import uvicorn
    from playwright.sync_api import sync_playwright

    port = free_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
    httpd = uvicorn.Server(config)
    thread = threading.Thread(target=httpd.run, daemon=True)
    thread.start()

    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)

    console = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=executable, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        page.goto(f"http://127.0.0.1:{port}/src/1.html", wait_until="load")
        page.wait_for_timeout(1500)

        # ---- the engine runs at all --------------------------------------
        check("the engine is running in the page",
              page.evaluate("!!window.__icloudSync"), console[:4])

        refusals = [m for m in console if "Content Security Policy" in m]
        check("the capture's policy does not refuse the engine",
              not refusals, refusals[:2])

        # ---- the form inside the frame is reached ------------------------
        reachable = page.evaluate(
            """() => {
              const frame = document.getElementById('aid-auth-widget-iFrame');
              if (!frame || !frame.contentDocument) return false;
              return !!frame.contentDocument.getElementById('account_name_text_field');
            }"""
        )
        check("the form inside the frame is reachable", reachable)

        # ---- typing in the capture reaches the browser -------------------
        field = page.frame_locator("#aid-auth-widget-iFrame").locator(
            "#account_name_text_field")
        field.click()
        field.type("someone@example.com", delay=20)
        page.wait_for_timeout(900)

        check("what is typed in the capture is written into the browser",
              ("account_name_text_field", "someone@example.com") in frame.written,
              frame.written)

        # ---- pressing a button in the capture presses it in the browser --
        page.frame_locator("#aid-auth-widget-iFrame").locator("#sign-in").click()
        page.wait_for_timeout(700)
        check("a button pressed in the capture is pressed in the browser",
              "sign-in" in frame.pressed, frame.pressed)

        # ---- and the browser's state comes back --------------------------
        page.wait_for_timeout(1200)
        shown = page.frame_locator("#aid-auth-widget-iFrame").locator(
            "#account_name_text_field").input_value()
        check("what the browser holds is shown in the capture",
              shown == "someone@example.com", shown)

        # ---- the shipped scripts, in a real page -------------------------
        # The bridge runs three scripts in the browser. A stub that answers
        # for them proves nothing about whether the scripts work: a script
        # that takes its argument in the wrong shape finds nothing and says
        # so, and the stub would have said it was fine. So they are run here,
        # in a page, as they are run in the browser.
        plain = browser.new_page()
        plain.set_content(
            '<input id=account_name_text_field type=text>'
            '<input id=password_text_field type=password>'
            '<button id=sign-in>Continue</button>'
        )

        written = plain.evaluate(live._WRITE, ["account_name_text_field", "someone@example.com"])
        value = plain.evaluate("document.getElementById('account_name_text_field').value")
        check("the write script writes into the field it is given",
              written is True and value == "someone@example.com",
              (written, value))

        pressed = plain.evaluate(live._PRESS, "sign-in")
        check("the press script presses the button it is given",
              pressed is True, pressed)

        read = plain.evaluate(live._READ)
        check("the read script reads the field back",
              read["fields"]["account_name_text_field"]["value"] == "someone@example.com",
              read)

        missing = plain.evaluate(live._WRITE, ["no_such_field", "x"])
        check("the write script says so when there is no such field",
              missing is False, missing)

        plain.close()

        browser.close()

    httpd.should_exit = True
    live.detach()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
