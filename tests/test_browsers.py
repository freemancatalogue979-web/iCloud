"""The browsers the app will use, and where it finds them.

Playwright's own "chrome" channel only finds a Chrome that was installed
where it expects it - /opt/google/chrome - and a machine that has Chrome
somewhere else has no browser that Playwright can start by that name. It
fails, the app moves on, and the sign-in is quietly done with the bundled
Chromium instead - which is not the browser that was asked for.

So the binary is looked for on the disk, and the path is given when one is
found. That is what is pinned here.

Run with: python tests/test_browsers.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browsers  # noqa: E402

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
    said = browsers.candidates()

    check("there is something to try", bool(said), said)
    check("the machine's own browser is tried first",
          said[0][0] in ("system chrome", "system chromium"), said[0][0])
    check("the bundled one is there to fall back on",
          any(label == "bundled chromium" for label, _ in said), said)

    for label, extra in said:
        path = extra.get("executable_path")
        if path:
            check(f"{label} points at a browser that is there",
                  Path(path).exists(), path)
        elif label == "bundled chromium":
            # The one Playwright brings itself: no path and no channel.
            check("the bundled one is left to Playwright", not extra, extra)
        else:
            check(f"{label} is a channel Playwright knows",
                  extra.get("channel") in ("chrome", "msedge"), extra)

    # A path that is not there is not offered.
    check("a path that is not there is not found",
          browsers.found(("/nowhere/at/all/chrome",)) == "")

    # A path that is there is found, and the first one wins.
    here = os.path.abspath(__file__)
    check("a browser that is there is found",
          browsers.found(("/nowhere/at/all/chrome", here)) == here)

    check("a browser can be named out loud",
          browsers.name_of({"executable_path": "/usr/bin/google-chrome"}) ==
          "google-chrome", browsers.name_of({"executable_path": "/usr/bin/google-chrome"}))
    check("...and a channel too",
          browsers.name_of({"channel": "chrome"}) == "chrome")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(main())
