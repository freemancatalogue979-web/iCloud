"""A screen of one's own, for a machine that has none.

The browser the app signs in with is a real browser with a real window, and a
window needs an X server. A desktop Linux machine has one. A server, a
container, a cloud runner and an ``ssh`` without ``-X`` have none - and a
server is exactly where an app like this ends up.

Xvfb is an X server that draws on nothing: a screen with no glass in front of
it. It is all a browser needs to open a window, render a page and be typed
into - and being typed into is the whole of what the app asks of it, because
what the person types goes into the browser from the capture, not from their
keyboard.

**One each, and not one for the app.** A screen is shared the way a desktop is
shared: two Chromes on one display are two windows on one desktop. The second
one can decide the first is already running and hand its URL over and exit, a
dialog can appear in front of the wrong browser, "bring this tab to the front"
means whatever the window manager decides, and one browser's crash takes the
screen with it. Each session's browser on a display of its own is alone on it,
which is how a browser behaves on a machine of its own.

    DISPLAY           a screen that is already here - used, nothing started
    ICLOUD_XVFB=1     a screen of one's own even when there is a display
    ICLOUD_XVFB=0     never start Xvfb
    ICLOUD_SCREEN     what Xvfb draws, default 1360x1024x24
"""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

# What the screen is. A browser window is laid out as if it were on a real
# desktop this size, so a small screen makes a small, cramped page.
SCREEN = os.environ.get("ICLOUD_SCREEN") or "1360x1024x24"

# Where X servers put the socket a client connects to. The tests point this
# somewhere of their own; in use it is where X itself puts it.
SOCKET_DIR = Path(os.environ.get("ICLOUD_X11_SOCKET_DIR") or "/tmp/.X11-unix")

# Display numbers to try. 99 upwards is out of the way of a real seat, which
# is :0 on almost every machine.
FIRST_DISPLAY = int(os.environ.get("ICLOUD_XVFB_FIRST") or 99)
LAST_DISPLAY = FIRST_DISPLAY + 101

# How long a server is given to come up, and how long it is given to stop.
START_SECONDS = 5.0
STOP_SECONDS = 5.0

# The numbers this process is using or has been refused, so that two sessions
# opening a screen at the same time do not both pick the same one.
_taken: set = set()


def has_a_display() -> bool:
    """Is there a screen here already?

    Windows and macOS always have one. On Linux it is the environment that
    says so, and it is read when it is asked rather than when this module is
    imported: a test, or a shell that exports DISPLAY late, both work.
    """
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def binary() -> str:
    """Where Xvfb is, or "" when it is not installed.

    ``ICLOUD_XVFB_BIN`` is for the tests, which need a stand-in that puts a
    socket where a server would put one. A path that is not there is no
    better than no path at all: what is found has to be a file.
    """
    found = os.environ.get("ICLOUD_XVFB_BIN") or shutil.which("Xvfb") or ""
    return found if found and os.path.exists(found) else ""


def _setting() -> str:
    return os.environ.get("ICLOUD_XVFB", "").strip().lower()


def off() -> bool:
    """Has somebody said not to use Xvfb, whatever the machine looks like?"""
    return _setting() in ("0", "off", "no", "false")


def on() -> bool:
    """Has somebody asked for Xvfb even though there is a display?"""
    return _setting() in ("1", "on", "yes", "true")


def wanted() -> bool:
    """Does this machine's browsers need a screen made for them?

    A real display is used as it is. A machine with none gets Xvfb for every
    session - and a machine with no Xvfb gets the answer False, so that the
    caller can say what to install instead of failing with a missing file.
    """
    if off() or not binary():
        return False
    return on() or not has_a_display()


def socket_for(number: int) -> Path:
    """Where a server on this display number puts its socket."""
    return SOCKET_DIR / f"X{number}"


def busy(number: int) -> bool:
    """Is another server already on this display number?"""
    return number in _taken or socket_for(number).exists()


class Screen:
    """One Xvfb, and the display number it answers on."""

    def __init__(self, number: int, process, noise=None):
        self.number = number
        self.process = process
        self.noise = noise or []
        self._drain = None

    @property
    def display(self) -> str:
        """What DISPLAY is set to for a browser on this screen."""
        return f":{self.number}"

    def alive(self) -> bool:
        return self.process.returncode is None

    def said(self) -> str:
        """Whatever the server wrote to its error stream."""
        return "".join(self.noise).strip()

    async def stop(self) -> None:
        """Put the screen away. Anything still on it goes with it.

        Called when the browser that was on it is shut down, and never on a
        screen two browsers share, because no two do.
        """
        await _end(self.process, self._drain)
        _taken.discard(self.number)


async def _drain(stream, noise: list) -> None:
    """Keep a server's error stream read, or the pipe fills and it blocks."""
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            noise.append(chunk.decode(errors="replace"))
            del noise[:-4]
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _end(process, drain=None) -> None:
    """Stop a process and everything it started, without waiting for ever."""
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), STOP_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), STOP_SECONDS)
            except asyncio.TimeoutError:
                pass
    if drain is not None:
        drain.cancel()


async def open_screen() -> Screen:
    """Make a screen, and say which display it is.

    The first display number nothing else is using is taken, so two sessions
    never share one. A number that refuses to come up is tried past rather
    than guessed about: a socket left behind by a server that is gone, or a
    real seat somebody is using, both look like "not free" and neither is
    worth stopping for.

    Raises when no display number will come up at all, with what the last
    server said, because the fault is the machine's and not a session's.
    """
    program = binary()
    if not program:
        raise RuntimeError("Xvfb is not installed")

    SOCKET_DIR.mkdir(parents=True, exist_ok=True)

    said = ""
    for number in range(FIRST_DISPLAY, LAST_DISPLAY):
        if busy(number):
            continue

        _taken.add(number)
        process = await asyncio.create_subprocess_exec(
            program, f":{number}", "-screen", "0", SCREEN, "-nolisten", "tcp",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        noise: list = []
        drain = asyncio.ensure_future(_drain(process.stderr, noise))

        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if socket_for(number).exists():
                screen = Screen(number, process, noise)
                screen._drain = drain
                return screen
            if process.returncode is not None:
                break
            await asyncio.sleep(0.02)

        said = "".join(noise).strip() or said
        await _end(process, drain)
        _taken.discard(number)

    raise RuntimeError(said or "no display number would come up")
