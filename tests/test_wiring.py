"""The server's own wiring: the device, the group, and the browser.

This is the join between the three modules that know about the visitor
(where they are, which address they share, what their machine is) and
the browser that is opened for them. What is pinned here is that the
device is the same on every visit, that the screen is the machine's own
size, and that the browser is told to leave from the group's address.

Run with: python tests/test_wiring.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The profiles and the group map are told where to go before anything is
# imported, because both are read at import time.
scratch = tempfile.mkdtemp(prefix="icloud-wiring-")
os.environ["ICLOUD_PROFILES"] = os.path.join(scratch, "visitors")
os.environ["ICLOUD_STATE"] = os.path.join(scratch, "state")
os.environ["ICLOUD_PROXY"] = "decodo"
os.environ["ICLOUD_PROXY_USER"] = "alice"
os.environ["ICLOUD_PROXY_PASSWORD"] = "secret"
os.environ["ICLOUD_PROXY_MAX_PER_GROUP"] = "2"
os.environ["ICLOUD_ADMIN_STORE"] = os.path.join(scratch, "admin.json")

from fastapi.responses import Response  # noqa: E402

import server  # noqa: E402
import admin  # noqa: E402
import browsers  # noqa: E402
import identity as identity_module  # noqa: E402
import proxy_pool  # noqa: E402
import turnstile  # noqa: E402

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


class FakeRequest:
    """A request with the two things the app reads off one."""

    def __init__(self, host, headers=None):
        self.client = type("Address", (), {"host": host})()
        self.headers = dict(headers or {})


def header(response, name: str) -> str:
    """A header's value, whichever way it is written.

    Starlette's headers do not care about the case; a plain dictionary
    does, and the test should not care either.
    """
    for key, value in response.headers.items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


class FakeCall:
    """A request the policy can be asked about, and an answer to give it."""

    def __init__(self, path, origin="", method="GET"):
        self.headers = {"origin": origin} if origin else {}
        self.method = method
        self.url = type("Url", (), {"path": path})()


async def main():
    # -- the visitor is given a device and a group --------------------
    session = server.Session("visitor-one")
    request = FakeRequest("104.16.0.1", {"cf-connecting-ip": "198.51.100.9"})

    await server.prepare_identity(session, request)
    check("the session has a device", session.identity is not None)
    check("...and a group", bool(session.group), session.group)
    check("the device is kept with the profile",
          (session.profile() / "identity.json").exists())

    device = session.identity.device
    check("the device is a size a machine has",
          (device.screen["width"], device.screen["height"]) in
          identity_module.SCREENS)
    check("...with a ratio a machine has", device.dpr in identity_module.DPRS)

    # A second visit: the same visitor, the same machine, the same address.
    again = server.Session("visitor-one")
    await server.prepare_identity(again, request)
    check("a visitor who comes back gets the same device",
          again.identity.device.to_dict() == device.to_dict())
    check("...and the same group", again.group == session.group)
    check("...and the same identity", again.identity.id == session.identity.id)

    # -- the screen is the machine's own ------------------------------
    geometry = server.geometry_for(session)
    check("the screen is the size the device says",
          geometry == f"{int(device.screen['width'] * device.dpr)}x"
                      f"{int(device.screen['height'] * device.dpr)}x24",
          geometry)

    # -- and the browser is told to leave from the group's address ----
    options = server.context_options(session)
    check("the language is the machine's", "locale" in options, options)
    check("the clock is the machine's", "timezone_id" in options, options)
    check("the browser leaves from the group's address",
          options.get("proxy", {}).get("server") == "http://gate.decodo.com:7000",
          options.get("proxy"))
    check("...with the country in the name",
          "-country-" in options["proxy"]["username"], options["proxy"]["username"])

    # -- the browser's flags ------------------------------------------
    args = server.browser_args(device)
    check("the browser stops saying it is being driven",
          "--disable-blink-features=AutomationControlled" in args)
    check("the browser's ratio is the machine's",
          f"--force-device-scale-factor={device.dpr}" in args, args)

    # -- and the panel is told ----------------------------------------
    said = server.identity_summary(session)
    check("the panel is told the group", said.get("group") == session.group, said)
    check("...the country", said.get("country") == session.identity.country)
    check("...the screen", said.get("screen") ==
          f"{device.screen['width']}x{device.screen['height']}", said)

    # -- the browsers are the ones the machine has --------------------
    said = browsers.candidates()
    check("the system browser is tried first",
          said[0][0] in ("system chrome", "system chromium"), said[0][0])
    check("...and the bundled one is there to fall back on",
          any(label == "bundled chromium" for label, _ in said), said)
    for label, extra in said:
        path = extra.get("executable_path")
        if path:
            check(f"{label} is a browser that is on the machine",
                  Path(path).exists(), path)
    check("the app and the proxy check use one list",
          server.BROWSERS == said, server.BROWSERS)

    # -- a browser that left from the wrong place is not handed over ---
    # The sign-in is the whole point of the address, so a browser that
    # ignored the proxy is a browser that must not be given to the user.
    class FakeContext:
        closed = False

        async def close(self):
            FakeContext.closed = True

        def on(self, *a, **k):
            return None

    class FakeLocator:
        first = None

        def __init__(self):
            self.first = self

        async def click(self, *a, **k):
            return None

    class FakePage:
        async def evaluate(self, what):
            return "Mozilla/5.0"

        async def goto(self, *a, **k):
            return None

        async def inner_text(self, what):
            return ""

        async def bring_to_front(self):
            return None

        def locator(self, what):
            return FakeLocator()

    class FakeSessions:
        @staticmethod
        def changed(*a, **k):
            return None

        @staticmethod
        def failed(*a, **k):
            return None

    session.ctx = FakeContext()
    session.page = FakePage()
    old_sessions = server.SESSIONS
    server.SESSIONS = FakeSessions
    old_verify = server.verify_exit
    old_launch = server.launch

    async def fake_launch(_session):
        return FakeContext(), FakePage()

    server.launch = fake_launch

    async def wrong_exit(_session):
        return {"ok": False, "wrong": True, "ip": "198.51.100.7",
                "country": "US", "said": "the browser left from the machine itself"}

    server.verify_exit = wrong_exit
    try:
        state = await server.start_browser(session)
    finally:
        server.verify_exit = old_verify
        server.SESSIONS = old_sessions
        server.launch = old_launch

    check("a browser that left from the machine itself is not handed over",
          state.get("status") == "error", state)
    check("...and the session holds no browser any more",
          session.ctx is None and not session.state.get("browser_open"),
          session.ctx)
    check("...and the reason is said",
          "machine itself" in str(state.get("error")), state)

    # -- a good address in another country is not stopped either ------
    # The country comes from where the visitor was seen, and the operator
    # may want another one. The address is what matters: it is the
    # machine's own address that stops everything.
    session2 = server.Session("visitor-three")
    await server.prepare_identity(session2, request)
    session2.ctx = FakeContext()
    session2.page = FakePage()

    async def other_country(_session):
        return {"ok": False, "wrong": False, "mismatch": True,
                "ip": "203.0.113.44", "country": "DE",
                "said": "it left from DE, not US"}

    server.verify_exit = other_country
    server.launch = fake_launch
    try:
        state = await server.start_browser(session2)
    finally:
        server.verify_exit = old_verify
        server.SESSIONS = old_sessions
        server.launch = old_launch

    check("an address in another country is not stopped",
          state.get("status") != "error", state)
    check("...but it is said",
          "DE" in str(state.get("error")), state)

    # -- a copy on another host is allowed, and only that one -----------
    # A browser will not let a page on one host ask a server on another
    # unless the server says it may - and it will say it may for the host
    # the copy is on and for that host only.
    os.environ["ICLOUD_STANDALONE_ORIGINS"] = "https://pages.example.com"
    try:
        check("the copy's own origin is allowed",
              server.origin_allowed("https://pages.example.com"))
        check("another origin is not",
              not server.origin_allowed("https://elsewhere.example.com"))
        check("no origin is not", not server.origin_allowed(""))
        check("a null origin is not", not server.origin_allowed("null"))

        async def call_next(request):
            return Response(status_code=200)

        response = await server.allow_the_copy(
            FakeCall("/status", "https://pages.example.com"), call_next)
        check("the copy's pages are told they may ask",
              header(response, "access-control-allow-origin") ==
              "https://pages.example.com", dict(response.headers))

        response = await server.allow_the_copy(
            FakeCall("/status", "https://elsewhere.example.com"), call_next)
        check("...and another host is not",
              not header(response, "access-control-allow-origin"))

        response = await server.allow_the_copy(
            FakeCall("/admin/api/proxy", "https://pages.example.com"), call_next)
        check("the panel is not opened to a page on another host",
              not header(response, "access-control-allow-origin"))

        response = await server.allow_the_copy(
            FakeCall("/nav", "https://pages.example.com", method="OPTIONS"),
            call_next)
        check("the question a browser asks first is answered",
              response.status_code == 204 and
              header(response, "access-control-allow-methods"),
              response.status_code)

        # The copy's pages carry the answer to the question in a header,
        # and a header the browser has not been told about makes it ask
        # first. So the header has to be allowed by name.
        response = await server.allow_the_copy(
            FakeCall("/start", "https://pages.example.com", method="OPTIONS"),
            call_next)
        check("the answer to the question is allowed across",
              turnstile.HEADER in header(response,
                                         "access-control-allow-headers"),
              header(response, "access-control-allow-headers"))

        # The site's key is public, so it goes to anybody who asks. A copy
        # that has not been told about the server yet still has to be able
        # to ask what the question is.
        response = await server.allow_the_copy(
            FakeCall("/turnstile/sitekey", "https://elsewhere.example.com"),
            call_next)
        check("the site's key is given to anybody who asks",
              header(response, "access-control-allow-origin") == "*",
              dict(response.headers))
        check("...and without credentials, which a wildcard cannot carry",
              not header(response, "access-control-allow-credentials"),
              dict(response.headers))
    finally:
        os.environ.pop("ICLOUD_STANDALONE_ORIGINS", None)

    # -- and the address the panel was given is allowed too --------------
    admin.STORE.set_standalone("", "https://the-copy.example.com")
    try:
        check("the address the panel was given is allowed",
              server.origin_allowed("https://the-copy.example.com"))
        check("...and it is not the same as the server's own",
              not server.origin_allowed("https://the.server.example.com"))
    finally:
        admin.STORE.set_standalone("", "")

    # -- a visitor with no device yet is not given one by looking -----
    plain = server.Session("visitor-two")
    check("a session that has not been asked for has no device",
          server.identity_summary(plain) == {})
    check("...and no screen of its own size",
          server.geometry_for(plain) is None)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
