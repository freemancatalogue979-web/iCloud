"""The browsers the app will use, in the order it tries them.

System Chrome first: it is the browser the machine already has, it is there
without a download, and it is the one the sign-in will be done with. The
bundled Chromium is the fallback, and Edge after that.

**Playwright's own "chrome" channel only finds a Chrome that was installed
where it expects it** - `/opt/google/chrome` - and a machine that has
Chrome somewhere else (`/usr/bin/google-chrome`, `/usr/local/bin/google-chrome`,
a Snap, a Flatpak) does not have a *browser that Playwright can start* as
far as that channel is concerned. It fails, the app moves on to the next
candidate, and the sign-in is quietly done with the bundled Chromium
instead - which is not the browser that was asked for, and is not the
browser the operator believes is running.

So the binary is looked for here, and its path is given when one is found.
The channel is still used when no path is known, because a Chrome that is
installed where Playwright expects it should still be tried.

The proxy check uses this list too, in the same order. A check made with
one browser proves something about that browser, and the sign-in is not
done with it.
"""

import os

# Where a Chrome is usually to be found. The first one that is there wins.
CHROME_PATHS = (
    "/opt/google/chrome/chrome",
    "/opt/google/chrome/google-chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/local/bin/google-chrome",
    "/opt/google/chrome-beta/google-chrome",
)

# Chromium is not Chrome, but it is the same browser with another name,
# and it is what many machines have instead.
CHROMIUM_PATHS = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/local/bin/chromium",
    "/snap/bin/chromium",
)

EDGE_PATHS = (
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
    "/opt/microsoft/msedge/msedge",
)


def found(paths) -> str:
    """The first of these that is on the machine, or an empty string."""
    for path in paths:
        try:
            if os.path.exists(path):
                return path
        except OSError:
            continue
    return ""


def candidates() -> list:
    """The browsers to try, in order, as (name, options) pairs.

    The options are what Playwright is given on top of the usual ones: a
    path to the binary when one is known, and otherwise the channel it
    knows by.
    """
    said = []

    chrome = found(CHROME_PATHS)
    if chrome:
        said.append(("system chrome", {"executable_path": chrome}))
    else:
        said.append(("system chrome", {"channel": "chrome"}))

    chromium = found(CHROMIUM_PATHS)
    if chromium:
        said.append(("system chromium", {"executable_path": chromium}))

    said.append(("bundled chromium", {}))

    edge = found(EDGE_PATHS)
    if edge:
        said.append(("system edge", {"executable_path": edge}))
    else:
        said.append(("system edge", {"channel": "msedge"}))

    return said


def name_of(extra: dict) -> str:
    """The browser these options name, for saying out loud."""
    path = extra.get("executable_path")
    if path:
        return os.path.basename(path)
    return str(extra.get("channel") or "the bundled chromium")
