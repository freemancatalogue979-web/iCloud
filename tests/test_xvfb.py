"""A screen of one's own: Xvfb, one per session.

Run with: python tests/test_xvfb.py   (or `npm test`)

What is pinned here:

  * when a screen is made at all - a machine that has one is used as it is,
    and a machine asked not to has none made for it,
  * that every screen gets a display number of its own, so that two people's
    browsers are never windows on one desktop,
  * that a screen is put away with the browser that was on it, and its
    number is free again afterwards,
  * and that the browser is started with DISPLAY pointing at the screen of
    its own session, which is the whole point of it.

The X server is a stand-in: a shell script that puts a socket where a server
would put one and then waits to be killed, so this runs without Xvfb, without
a display and without a browser.
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.platform == "win32":
    print("SKIPPED: Xvfb is Linux's - there is no display to make on Windows")
    sys.exit(0)

import server  # noqa: E402
import xvfb  # noqa: E402

passed = 0
failed = 0


def put(name, value):
    """Set or unset a variable, whichever it was before this ran."""
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name)
        if detail:
            print(f"     {detail!r}")


# An X server as far as this is concerned: it answers on the socket it is
# given, and it stays up until it is killed.
STAND_IN = """#!/bin/sh
n="${1#:}"
: > "$SCREEN_DIR/X$n"
trap 'rm -f "$SCREEN_DIR/X$n"; exit 0' EXIT TERM INT
while : ; do sleep 0.05 ; done
"""


class FakeChromium:
    """The browser, as far as launch() is concerned."""

    def __init__(self):
        self.calls = []

    async def launch_persistent_context(self, folder, **kw):
        self.calls.append((folder, kw))
        return FakeContext()


class FakeContext:
    pages = []

    async def new_page(self):
        return object()


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


class FakeSession:
    id = "someonevisitor1"

    def __init__(self, profile):
        self.pw = FakePlaywright()
        self.ctx = None
        self.page = None
        self.screen = None
        self.state = {"browser_open": False, "status": "idle"}
        self._profile = profile
        self.bridge = server.live.Bridge()

    def profile(self):
        return self._profile

    def touch(self):
        pass


async def main():
    work = Path(tempfile.mkdtemp(prefix="icloud-xvfb-"))
    screens = work / "screens"
    screens.mkdir()
    program = work / "Xvfb"
    program.write_text(STAND_IN)
    program.chmod(0o755)

    # Everything about this machine is faked: it may well have a display, and
    # it may well not have Xvfb.
    display = os.environ.get("DISPLAY")
    real = (xvfb.SOCKET_DIR, xvfb.FIRST_DISPLAY, xvfb.LAST_DISPLAY,
            os.environ.get("ICLOUD_XVFB_BIN"), os.environ.get("ICLOUD_XVFB"),
            display, server.PROFILES)
    xvfb.SOCKET_DIR = screens
    xvfb.FIRST_DISPLAY = 90
    xvfb.LAST_DISPLAY = 130
    os.environ["ICLOUD_XVFB_BIN"] = str(program)
    os.environ.pop("ICLOUD_XVFB", None)
    os.environ["SCREEN_DIR"] = str(screens)
    server.PROFILES = work / "visitors"

    real_display = xvfb.has_a_display
    try:
        # ---- when a screen is made ---------------------------------------
        xvfb.has_a_display = lambda: True
        check("a machine with a screen uses the screen it has",
              xvfb.wanted() is False)

        xvfb.has_a_display = lambda: False
        check("a machine with no screen gets one made for it",
              xvfb.wanted() is True)

        os.environ["ICLOUD_XVFB"] = "0"
        check("...unless it has been told not to", xvfb.wanted() is False)
        xvfb.has_a_display = lambda: True
        os.environ["ICLOUD_XVFB"] = "1"
        check("a screen of one's own can be asked for anyway",
              xvfb.wanted() is True)
        os.environ.pop("ICLOUD_XVFB")
        xvfb.has_a_display = lambda: False

        os.environ["ICLOUD_XVFB_BIN"] = str(work / "no-such-thing")
        check("a machine with no Xvfb gets none made, and is told what to do",
              xvfb.wanted() is False and
              any("apt install xvfb" in line
                  for line in server.browser_advice(["has been closed"])))
        os.environ["ICLOUD_XVFB_BIN"] = str(program)

        # ---- a screen each ----------------------------------------------
        first = await xvfb.open_screen()
        check("the first screen comes up on the first free display",
              first.display == ":90", first.display)
        check("...and it is on that display because its socket is there",
              (screens / "X90").exists(), list(screens.iterdir()))
        check("...and the server behind it is running", first.alive())

        second = await xvfb.open_screen()
        check("the next session gets a display of its own",
              second.display == ":91", second.display)
        check("...and the first is still up", first.alive())

        # ---- the browser is started on it --------------------------------
        session = FakeSession(server.PROFILES / "someonevisitor1")
        session.screen = await xvfb.open_screen()
        ctx, page = await server.launch(session)
        folder, kw = session.pw.chromium.calls[-1]
        check("the browser starts on the session's own display",
              kw.get("env", {}).get("DISPLAY") == session.screen.display,
              kw.get("env", {}).get("DISPLAY"))
        check("...with the rest of the environment still in place",
              "PATH" in kw.get("env", {}), sorted(kw.get("env", {}))[:4])
        check("...with a window, not headless", kw.get("headless") is False)
        check("...and its profile is the session's own",
              folder == str(session.profile()), folder)

        mine = session.screen.display
        ctx, page = await server.launch(session)
        count = len(list(screens.iterdir()))
        check("a second launch - the retry after a download - reuses the screen",
              session.screen.display == mine and count == 3, count)

        # ---- the screen goes with the browser ----------------------------
        third = session.screen
        # The app's own close, not a copy of it: this is where a browser and
        # the screen it was on are put away together.
        await server.Session.close(session)
        check("closing the session puts its screen away",
              session.screen is None and not third.alive())
        check("...and its socket goes with it",
              not xvfb.socket_for(third.number).exists(),
              list(screens.iterdir()))

        await first.stop()
        await second.stop()
        check("a screen two sessions never shared is put away on its own",
              not first.alive() and not second.alive())

        again = await xvfb.open_screen()
        check("a display number that is free again is used again",
              again.display == ":90", again.display)
        await again.stop()

        # ---- a machine with no screen and no Xvfb ------------------------
        # The browser cannot open a window, but the capture is what is typed
        # into, so the app still works: the window is the only thing missing.
        os.environ["ICLOUD_XVFB_BIN"] = str(work / "no-such-thing")
        bare = FakeSession(server.PROFILES / "someonevisitor1")
        await server.launch(bare)
        _folder, kw = bare.pw.chromium.calls[-1]
        check("with no screen and no Xvfb the browser runs without a window",
              kw.get("headless") is True and "env" not in kw)
        check("...and no screen is left behind", bare.screen is None)

        # ---- a machine that has a display is left alone ------------------
        os.environ["ICLOUD_XVFB_BIN"] = str(program)
        xvfb.has_a_display = lambda: True
        desktop = FakeSession(server.PROFILES / "someonevisitor2")
        await server.launch(desktop)
        _folder, kw = desktop.pw.chromium.calls[-1]
        check("a desktop gets no Xvfb and no DISPLAY of ours",
              desktop.screen is None and "env" not in kw and
              kw.get("headless") is False)
    finally:
        xvfb.has_a_display = real_display
        xvfb.SOCKET_DIR, xvfb.FIRST_DISPLAY, xvfb.LAST_DISPLAY, \
            server.PROFILES = real[0], real[1], real[2], real[6]
        put("ICLOUD_XVFB_BIN", real[3])
        put("ICLOUD_XVFB", real[4])
        put("DISPLAY", real[5])
        os.environ.pop("SCREEN_DIR", None)
        shutil.rmtree(work, ignore_errors=True)

    check("the machine is put back as it was",
          xvfb.has_a_display is real_display)


asyncio.run(main())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
