"""What the browser is told, and what it is allowed to hide.

Two things live here: the flags the browser starts with, and the handful
of things it says about the machine it is on - the language, the clock,
the colour scheme, the screen.

There is a rule in this module that is worth more than everything else in
it:

> **Do not say anything the browser does not already say.**

A browser running on a Linux machine reports ``Linux x86_64`` in its
client hints, and it says so whatever the ``User-Agent`` header claims.
Spoiling that header without spoiling the hints leaves the two next to
each other, telling each other apart. So the honest name is the default
here, and a different name is something a person asks for on purpose and
then has the panel check for them.

What this module never does is hide a browser. It does not remove
``navigator.webdriver`` with a patch that could be spotted, and it does
not pretend a headless machine has a screen. It tells the browser to
stop announcing that it is being driven, which is the one thing it can be
asked to stop saying without lying about anything else.
"""

import os
import re
import secrets

# How the browser's own name for itself is treated.
#
#   real   the browser says what it really is. The default, and the only
#          one that cannot be contradicted by the client hints.
#   major  the browser is given a different version number, and the
#          client hints are given the same one - the panel then checks
#          whether the browser actually sent it, because a browser that
#          ignored the instruction is worse than one that never got it.
UA_MODE = (os.environ.get("ICLOUD_UA_MODE") or "real").strip().lower()

# The platform a client hint says, for the platform this runs on.
PLATFORM = os.environ.get("ICLOUD_PLATFORM") or "Linux"

# How many versions back a browser may say it is. A machine that was
# updated a month ago is behind; one that is three versions behind is
# old and says so on its own.
MAJORS_BACK = (1, 2, 3)

# The Chrome version in a user agent.
_VERSION = re.compile(r"(?:Chrome|Chromium)/(\d+)\.", re.IGNORECASE)

# The version in a client hint: "Chromium";v="140", "Google Chrome";v="140"
_HINT_VERSION = re.compile(r'v="(\d+)')


def parse_major(user_agent: str) -> int | None:
    """The version a browser's name says it is, if it says one."""
    found = _VERSION.search(user_agent or "")
    return int(found.group(1)) if found else None


def hint_major(hints: str) -> int | None:
    """The version a client hint says, if it says one."""
    found = _HINT_VERSION.search(hints or "")
    return int(found.group(1)) if found else None


def ua_with_major(user_agent: str, major: int) -> str:
    """The same name, saying a different version."""
    return _VERSION.sub(lambda m: f"Chrome/{major}.", user_agent, count=1)


def client_hints(major: int, mobile: bool = False) -> dict:
    """The client hints that go with a version number.

    Chromium sends these itself, and it sends them whatever the
    ``User-Agent`` says. When the name is changed, these are changed with
    it - and whether the browser obeyed is a thing the panel checks.
    """
    brand = '"Not=A?Brand";v="24", "Chromium";v="%d", "Google Chrome";v="%d"'
    full = ('"Not=A?Brand";v="24.0.0.0", "Chromium";v="%d.0.0.0", '
            '"Google Chrome";v="%d.0.0.0"')
    return {
        "Sec-CH-UA": brand % (major, major),
        "Sec-CH-UA-Full-Version-List": full % (major, major),
        "Sec-CH-UA-Mobile": "?1" if mobile else "?0",
        "Sec-CH-UA-Platform": f'"{PLATFORM}"',
    }


def choose_user_agent(real_user_agent: str, seed: str = "") -> str | None:
    """The name the browser should use, or ``None`` to keep its own.

    ``None`` means: say what you really are. That is the default, and
    the reason it is the default is written at the top of this module.
    """
    if UA_MODE != "major":
        return None
    major = parse_major(real_user_agent)
    if not major:
        return None
    chooser = secrets.SystemRandom()
    behind = MAJORS_BACK[chooser.randrange(len(MAJORS_BACK))]
    return ua_with_major(real_user_agent, max(1, major - behind))


def context_options(device) -> dict:
    """What the browser's context is told, from the device.

    The language, the clock and the colour scheme come from the device,
    because they are what the device is. The name of the browser comes
    from the device too, but only when there is one to use and the mode
    says to use it.
    """
    options = {
        "locale": device.locale,
        "timezone_id": device.timezone,
        "color_scheme": device.color_scheme,
    }
    # The header the language is asked in. Chromium sets this from the
    # locale, but saying it too costs nothing and keeps the two together.
    options["extra_http_headers"] = {
        "Accept-Language": f"{device.locale},en;q=0.8",
    }
    if device.user_agent:
        said = choose_user_agent(device.user_agent)
        if said:
            options["user_agent"] = said
            major = parse_major(said)
            if major:
                options["extra_http_headers"].update(client_hints(major))
    return options


def launch_flags(device=None) -> list:
    """The flags the browser starts with.

    Nothing here hides the browser. What it does is stop the browser
    announcing things that are true of a machine it is not on: the
    automation flag that says a person did not press anything, the
    WebRTC that can say the machine's own address, and the QUIC that
    would go around the proxy instead of through it.
    """
    flags = [
        # The browser says it is being driven. This is the one flag that
        # makes it stop, and it changes nothing about how it works.
        "--disable-blink-features=AutomationControlled",
        # No local addresses, and no way around the proxy.
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        # QUIC runs over UDP, and UDP does not go through a proxy.
        "--disable-quic",
    ]
    if device is not None:
        # The screen is the size of the display, and the display is made
        # for the screen - so the ratio is the machine's, not the
        # browser's. Under a window with no viewport of its own this
        # flag is the only way to set it.
        try:
            flags.append(f"--force-device-scale-factor={device.dpr}")
        except (AttributeError, TypeError, ValueError):
            pass
    return flags


def agree(headers: dict, user_agent: str) -> dict:
    """Does what the browser sent match the name it was given?

    The browser was asked to say a certain version. It may have said it
    in the ``User-Agent`` and not in the client hints, or in neither. The
    panel is told what happened, because a name that disagrees with the
    hints is worse than no name at all.
    """
    said = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    ua = said.get("user-agent", "")
    hints = said.get("sec-ch-ua", "")
    major = parse_major(ua)
    hinted = hint_major(hints)

    if not user_agent:
        return {"ok": True, "said": "the browser's own name", "ua": ua}
    if major and hinted and major == hinted:
        return {"ok": True, "said": "the name and the hints agree", "ua": ua}
    if major and not hinted:
        return {"ok": False, "said": "the hints did not carry the name", "ua": ua}
    if major and hinted and major != hinted:
        return {"ok": False,
                "said": f"the name says {major} and the hints say {hinted}",
                "ua": ua}
    return {"ok": False, "said": "the name was not sent as it was given",
            "ua": ua}
