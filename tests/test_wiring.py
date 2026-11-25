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

import server  # noqa: E402
import identity as identity_module  # noqa: E402
import proxy_pool  # noqa: E402

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

    # -- a visitor with no device yet is not given one by looking -----
    plain = server.Session("visitor-two")
    check("a session that has not been asked for has no device",
          server.identity_summary(plain) == {})
    check("...and no screen of its own size",
          server.geometry_for(plain) is None)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
