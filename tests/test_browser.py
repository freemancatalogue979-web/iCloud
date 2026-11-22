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

The bridge is asynchronous and so is this: the app's server and the browser
it drives share one event loop, and so do this test and the page it opens.

If there is no browser to be had, the suite says so and is skipped.
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
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


class FakeLocator:
    """What Playwright's locator does, as far as the bridge uses it.

    The bridge types into the browser with a locator, so a stub that only
    answers evaluate() would say nothing about whether typing works.
    """

    def __init__(self, frame, elements, index=0):
        self.frame = frame
        self.elements = elements
        self.index = index

    def nth(self, index):
        return FakeLocator(self.frame, self.elements, index)

    @property
    def first(self):
        return FakeLocator(self.frame, self.elements, 0)

    def _element(self):
        if self.index < len(self.elements):
            return self.elements[self.index]
        return None

    async def count(self):
        return len(self.elements)

    async def input_value(self, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        return self.frame.fields.get(name, "")

    async def click(self, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        self.frame.clicks.append(name)

    async def fill(self, value, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        self.frame.filled.append((name, value))
        self.frame.fields[name] = value

    async def press_sequentially(self, value, delay=None, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        self.frame.typed.append((name, value))
        # What a real input does: the letters are added to what is there.
        self.frame.fields[name] = self.frame.fields.get(name, "") + value


class FakeFrame:
    """A frame of the browser, with the controls it holds.

    ``ids`` are the controls that have an id. ``kinds`` are the controls
    that have none - Apple's verification code boxes - named by their place,
    the same way the capture names them.
    """

    def __init__(self, ids=(), fields=None, kinds=None,
                 url="https://www.icloud.com/", title="iCloud"):
        self.ids = set(ids)
        self.fields = dict(fields or {})
        self.kinds = dict(kinds or {})
        self.url = url
        self.title = title
        self.written = []
        self.pressed = []
        self.clicks = []      # every click, into a field or on a button
        self.typed = []       # what was typed, key by key
        self.filled = []      # what was cleared

    def locator(self, selector):
        if selector.startswith("#"):
            name = selector[1:]
            elements = [name] if name in self.ids else []
        else:
            elements = list(self.kinds.get(selector, []))
        return FakeLocator(self, elements)

    async def evaluate(self, script, argument=None):
        if "out.slice" in script:
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

        # The write: [target, value]
        target, value = argument
        name = target.get("id") if isinstance(target, dict) else target
        self.written.append((name, value))
        if name in self.ids:
            self.fields[name] = value
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
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    candidates = []
    try:
        with async_playwright() as p:
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


async def main():
    root = Path(tempfile.mkdtemp()).resolve()
    (root / "1.html").write_text(FIXTURE, encoding="utf-8")
    server.SRC = root

    # The browser behind the app: a sign-in form with the ids the capture
    # has. A frame with no ids at all would say nothing: the bridge would
    # find nothing to type into and the test would pass anyway.
    frame = FakeFrame(
        ids=["account_name_text_field", "password_text_field", "sign-in"],
        fields={"account_name_text_field": "", "password_text_field": ""},
        url="https://idmsa.apple.com/signin",
    )
    live.attach(FakeContext([FakePage([frame])]), None)

    executable = browser_path()
    if not executable or not Path(executable).exists():
        print("SKIPPED: no Chromium to test with")
        print(f"\n{passed} passed, {failed} failed")
        return 0

    import uvicorn
    from playwright.async_api import async_playwright

    port = free_port()
    httpd = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    asyncio.create_task(httpd.serve())
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            await asyncio.sleep(0.1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        console = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        await page.goto(f"http://127.0.0.1:{port}/src/1.html", wait_until="load")
        await page.wait_for_timeout(1500)

        # ---- the engine runs at all --------------------------------------
        check("the engine is running in the page",
              await page.evaluate("!!window.__icloudSync"), console[:4])

        refusals = [m for m in console if "Content Security Policy" in m]
        check("the capture's policy does not refuse the engine",
              not refusals, refusals[:2])

        # ---- the form inside the frame is reached ------------------------
        reachable = await page.evaluate(
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
        await field.click()
        await field.type("someone@example.com", delay=20)

        # The letters that arrive while the person is still typing are held
        # back and written as one, when the typing stops.
        await page.wait_for_timeout(900)
        await live.flush_pending()
        await page.wait_for_timeout(400)

        held = frame.fields.get("account_name_text_field")
        check("what is typed in the capture is written into the browser",
              held == "someone@example.com", frame.typed)

        # ---- pressing a button in the capture presses it in the browser --
        await page.frame_locator("#aid-auth-widget-iFrame").locator("#sign-in").click()
        await page.wait_for_timeout(700)
        check("a button pressed in the capture is pressed in the browser",
              "sign-in" in frame.clicks, frame.clicks)

        # ---- and the browser's state comes back --------------------------
        await page.wait_for_timeout(1200)
        shown = await page.frame_locator("#aid-auth-widget-iFrame").locator(
            "#account_name_text_field").input_value()
        check("what the browser holds is shown in the capture",
              shown == "someone@example.com", shown)

        # ---- the shipped scripts, in a real page -------------------------
        # The bridge runs three scripts in the browser. A stub that answers
        # for them proves nothing about whether the scripts work: a script
        # that takes its argument in the wrong shape finds nothing and says
        # so, and the stub would have said it was fine. So they are run
        # here, in a page, as they are run in the browser.
        plain = await browser.new_page()
        await plain.set_content(
            '<input id=account_name_text_field type=text>'
            '<input id=password_text_field type=password>'
            '<button id=sign-in>Continue</button>'
        )

        written = await plain.evaluate(live._WRITE, [{"id": "account_name_text_field"}, "someone@example.com"])
        value = await plain.evaluate("document.getElementById('account_name_text_field').value")
        check("the write script writes into the field it is given",
              written is True and value == "someone@example.com", (written, value))

        # The press is a real click, not a script: the page's own handlers
        # have to run, and the browser has to take it as a person.
        live.attach(FakeContext([FakePage([plain.main_frame])]), None)
        pressed = await live.press("sign-in")
        check("a button is pressed in the live browser", pressed == "pressed", pressed)
        live.detach()

        read = await plain.evaluate(live._READ)
        check("the read script reads the field back",
              read["fields"]["account_name_text_field"]["value"] == "someone@example.com",
              read)

        missing = await plain.evaluate(live._WRITE, [{"id": "no_such_field"}, "x"])
        check("the write script says so when there is no such field",
              missing is False, missing)

        await plain.close()

        # ---- the bridge, typing into a real page -------------------------
        # The value has to arrive as keys, not as an assignment: Apple's
        # sign-in widget watches for keys, and a value put in behind its
        # back leaves the page looking like nobody has touched it - the
        # Continue button never appears.
        typed = await browser.new_page()
        await typed.set_content(
            "<input id=account_name_text_field type=text>"
            "<script>window.__keys = [];"
            "document.addEventListener('keydown', (e) => window.__keys.push(e.key));"
            "</script>"
        )

        # The bridge reaches the browser through its frames, so the frame is
        # what is handed over - the same thing Playwright's page.frames
        # gives.
        live.attach(FakeContext([FakePage([typed.main_frame])]), None)

        wrote = await live.write_field("account_name_text_field", "hello")
        keys = await typed.evaluate("window.__keys")
        value = await typed.input_value("#account_name_text_field")
        check("a value is written into the live browser", wrote and value == "hello",
              (wrote, value))
        check("...and it is typed, key by key, the way a person types it",
              keys == ["h", "e", "l", "l", "o"], keys)

        # ---- typing on, the way a person types ----------------------------
        # Each letter arrives on its own. The field keeps what it has and
        # the new letters are typed - not the whole address again for every
        # letter of it.
        await typed.evaluate(
            """() => {
                window.__typed = [];
                document.getElementById('account_name_text_field')
                    .addEventListener('keydown', e => window.__typed.push(e.key));
            }""")
        await typed.fill("#account_name_text_field", "")
        await typed.evaluate("() => { window.__typed = []; }")

        await live.write_field("account_name_text_field", "s")
        await live.write_field("account_name_text_field", "so")
        await live.write_field("account_name_text_field", "som")

        # The first letter goes in at once; the rest wait for the typing to
        # stop, and then go in as one.
        keys = await typed.evaluate("() => window.__typed")
        check("the first letter goes in at once", keys == ["s"], keys)

        await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
        await live.flush_pending()

        value = await typed.evaluate(
            "() => document.getElementById('account_name_text_field').value")
        check("the field ends up holding the whole address", value == "som", value)

        keys = await typed.evaluate("() => window.__typed")
        check("...and the letters that followed go in when the typing stops",
              keys == ["s", "o", "m"], keys)

        live.detach()
        await typed.close()

        # ---- a control with no id, in a real page ------------------------
        # Apple's verification code boxes have no ids at all: six inputs and
        # a hidden one. They are named by their place - the third input of
        # the page - and only written when the page has the same number of
        # them, because otherwise the right box cannot be told from the
        # wrong one.
        boxes = await browser.new_page()
        await boxes.set_content(
            "".join(
                f'<input class=form-security-code-input aria-label="Digit {i}">'
                for i in range(1, 7)
            )
            + "<input type=hidden>"
        )

        live.attach(FakeContext([FakePage([boxes.main_frame])]), None)

        wrote = await live.write_at("input", 3, 7, "7")
        values = await boxes.evaluate(
            "[...document.querySelectorAll('input')].map(i => i.value)")
        check("a control with no id is written by its place",
              wrote and values[2] == "7", (wrote, values))
        check("...and the other boxes are left alone",
              values[:2] == ["", ""] and values[3:] == ["", "", "", ""], values)

        await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
        refused = await live.write_at("input", 3, 6, "7")
        check("a control is not written when the page has a different number of them",
              refused == "missing", refused)

        live.detach()
        await boxes.close()
        await browser.close()

    httpd.should_exit = True
    await asyncio.sleep(0.3)      # let the server shut down tidily
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
