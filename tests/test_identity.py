"""The device a visitor's browser is, and the group it leaves from.

Three things are pinned here:

  * a device is made once and kept - a visitor who comes back gets the
    same machine, which is the whole point of keeping it in the profile,
  * the device agrees with the country it leaves from, and
  * nothing secret is ever written into the profile.

Run with: python tests/test_identity.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ICLOUD_STATE", "/tmp/icloud-test-state")

import identity  # noqa: E402
import stealth  # noqa: E402

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


def main():
    with tempfile.TemporaryDirectory() as scratch:
        here = Path(scratch)

        # -- the country says what the machine says -------------------
        check("a country has a language and a clock",
              identity.place_for("US") == ("en-US", "America/New_York"),
              identity.place_for("US"))
        check("...and so does Germany",
              identity.place_for("DE") == ("de-DE", "Europe/Berlin"))
        check("a country nobody knows falls back",
              identity.place_for("ZZ") == ("en-US", "UTC"))

        device = identity.make_device("FR")
        check("the device speaks the country",
              device.locale == "fr-FR", device.locale)
        check("...and keeps the country's time",
              device.timezone == "Europe/Paris", device.timezone)
        check("the screen is one a machine has",
              (device.screen["width"], device.screen["height"]) in identity.SCREENS)
        check("the ratio is one a machine has",
              device.dpr in identity.DPRS)

        # -- made once, kept ------------------------------------------
        profile = here / "visitor-one"
        profile.mkdir()

        first = identity.assign(profile, "US", "us-0")
        second = identity.assign(profile, "US", "us-0")
        check("the same profile gets the same device",
              first.device.to_dict() == second.device.to_dict())
        check("...and the same group",
              first.group == second.group == "us-0")
        check("...and the same identity",
              first.id == second.id)

        other = identity.assign(here / "visitor-two", "US", "us-1")
        check("another profile gets another device",
              other.device.to_dict() != first.device.to_dict()
              or other.id != first.id)

        # -- and it is kept on disk -----------------------------------
        said = json.loads((profile / "identity.json").read_text("utf-8"))
        check("the identity is kept with the profile",
              said["group"] == "us-0" and said["country"] == "US", said)
        check("...with the device in it",
              said["device"]["screen"]["width"] == first.device.screen["width"])

        # -- nothing secret is written down ---------------------------
        os.environ["ICLOUD_PROXY_PASSWORD"] = "the-secret-password"
        text = (profile / "identity.json").read_text("utf-8")
        check("no password is written into the profile",
              "the-secret-password" not in text)
        check("no proxy user name either",
              "ICLOUD_PROXY_USER" not in text)

        # -- the browser's own name for itself ------------------------
        real = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
        kept = identity.update_user_agent(profile, real)
        check("the browser's own name is kept",
              kept.device.user_agent == real, kept.device.user_agent)
        again = identity.assign(profile, "US", "us-0")
        check("...and it is still there next time",
              again.device.user_agent == real)

    # -- what the browser is told -------------------------------------
    check("the version in a name can be read",
          stealth.parse_major("Chrome/140.0.0.0") == 140)
    check("...and changed",
          stealth.parse_major(stealth.ua_with_major("Chrome/140.0.0.0", 137)) == 137)
    check("a name that says nothing has no version",
          stealth.parse_major("Mozilla/5.0") is None)

    check("by default the browser says what it really is",
          stealth.choose_user_agent("Chrome/140.0.0.0") is None)

    flags = stealth.launch_flags(identity.make_device("US"))
    check("the browser stops saying it is being driven",
          "--disable-blink-features=AutomationControlled" in flags)
    check("WebRTC is not allowed to say the machine's address",
          "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in flags)
    check("QUIC is not allowed to go around the proxy",
          "--disable-quic" in flags)
    check("the ratio is the machine's",
          any(f.startswith("--force-device-scale-factor=") for f in flags),
          flags)

    options = stealth.context_options(identity.make_device("DE"))
    check("the language is the machine's",
          options["locale"] == "de-DE", options)
    check("the clock is the machine's",
          options["timezone_id"] == "Europe/Berlin")
    check("the colour scheme is the machine's",
          options["color_scheme"] in ("light", "dark"))
    check("the browser's own name is not overridden",
          "user_agent" not in options)

    # -- and whether the name and the hints agree ---------------------
    agreeing = {
        "user-agent": "Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="140", "Google Chrome";v="140"',
    }
    check("a name and its hints that agree are fine",
          stealth.agree(agreeing, agreeing["user-agent"])["ok"])

    disagreeing = {
        "user-agent": "Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="138", "Google Chrome";v="138"',
    }
    check("a name and its hints that disagree are not",
          not stealth.agree(disagreeing, disagreeing["user-agent"])["ok"])

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(main())
