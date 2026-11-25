#!/usr/bin/env bash
#
# Starts the server on http://localhost:9002, on Linux.
#
# The first run does the setting up: a virtualenv in .venv, the requirements
# in it, and Chromium for the browser the app signs in with. Every run after
# that goes straight to the server.
#
# The control room's secret is set here, so there is no file to keep it in:
#
#   ICLOUD_ADMIN_TOKEN=MySecret123
#
# Setting it in the environment before the script is run overrides it, which
# is what you want anywhere this is not your own machine:
#
#   ICLOUD_ADMIN_TOKEN="$(head -c 24 /dev/urandom | base64)" ./run.sh
#
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"
STAMP="$VENV/.requirements-installed"

REQUIREMENTS="the packages the app needs"
CHROMIUM="the browser the app signs in with"

say() { printf '\033[1m[icloud]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[icloud]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[icloud]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- python --------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || \
    die "python3 is not installed. On Debian or Ubuntu: sudo apt install python3 python3-venv"

# ---- the virtualenv ------------------------------------------------------
if [ ! -x "$PY" ]; then
    say "setting up the virtualenv in $VENV - one time only"
    if ! python3 -m venv "$VENV"; then
        rm -rf "$VENV"
        die "the virtualenv could not be made. On Debian or Ubuntu the venv module is a package of its own: sudo apt install python3-venv"
    fi
fi

# ---- the requirements ----------------------------------------------------
# Installed when the file is newer than the last install, so that editing
# requirements.txt is enough to have it obeyed on the next run.
if [ ! -f "$STAMP" ] || [ "requirements.txt" -nt "$STAMP" ]; then
    say "installing $REQUIREMENTS"
    "$PY" -m pip install --quiet --upgrade pip || \
        warn "pip could not be upgraded - carrying on with the one that is there"
    if ! "$PY" -m pip install -r requirements.txt; then
        die "the requirements could not be installed - is there a network?"
    fi

    # ---- the browser -----------------------------------------------------
    # Chromium is downloaded once and kept in the user's cache, so this is
    # done when there is something new to install and not on every run. The
    # app can also fetch it on the first visit, so a machine with no network
    # right now is not a machine that cannot run: it is said, and the server
    # starts anyway.
    if ! "$PY" -m playwright install chromium >/dev/null 2>&1; then
        warn "$CHROMIUM could not be downloaded now - the app will try again on the first visit"
    fi

    touch "$STAMP"
fi

# ---- is there a browser at all? ------------------------------------------
# The app tries the machine's own Chrome first, then the downloaded Chromium,
# then Edge. If none of them is here yet, say how to get one rather than
# letting the first click on "start" be where it is found out.
chrome_here() {
    command -v google-chrome google-chrome-stable >/dev/null 2>&1 ||
        [ -x /opt/google/chrome/chrome ] ||
        [ -d "/Applications/Google Chrome.app" ]
}

chromium_here() {
    compgen -G "$HOME/.cache/ms-playwright/chromium-*" >/dev/null 2>&1 ||
        compgen -G "${PLAYWRIGHT_BROWSERS_PATH:-/nonexistent}/chromium-*" >/dev/null 2>&1 ||
        compgen -G "$HOME/Library/Caches/ms-playwright/chromium-*" >/dev/null 2>&1
}

if ! chrome_here && ! chromium_here; then
    warn "no browser on this machine yet - the window the app signs in with has nowhere to come from"
    warn "install Google Chrome with:  sudo $PY -m playwright install chrome --with-deps"
fi

# A machine with no screen is not checked for here: the server says that
# itself as it starts, in the same words, and saying it twice in one launch
# is noise.

# ---- a screen for the browser -------------------------------------------
# A server has no screen, and the browser the app signs in with needs one to
# open a window on - even when the person signs in through the capture rather
# than by looking at it. Xvfb is that screen: one for every session, so that
# no two people's browsers are ever windows on one desktop.
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] &&
   [ "$(uname -s)" = "Linux" ]; then
    if command -v Xvfb >/dev/null 2>&1; then
        say "no screen here - every session gets one of its own from Xvfb"
    else
        warn "no screen here, and no Xvfb - the browser would have no window to open"
        warn "install it and every session gets a screen of its own:  sudo apt install xvfb"
    fi
fi

# ---- the address a session leaves from ----------------------------------
# The app can send every browser out through a proxy: several accounts then
# leave from one address, each with a device of its own. The proxy's secret
# is in the environment, like the control room's.
case "${ICLOUD_PROXY:-none}" in
    none)
        ;;
    decodo|generic)
        if [ -z "${ICLOUD_PROXY_USER:-}" ] || [ -z "${ICLOUD_PROXY_PASSWORD:-}" ]; then
            warn "a proxy is set up, but there is no user name or password for it"
            warn "the browser would leave from this machine's own address instead"
        else
            say "every browser leaves through ${ICLOUD_PROXY} at ${ICLOUD_PROXY_SERVER:-gate.decodo.com:7000}"
            say "the country is the visitor's own, and a few accounts share one address"
            say "check it says the right thing with: $VENV/bin/python proxy_check.py"
        fi
        ;;
    *)
        warn "ICLOUD_PROXY is '${ICLOUD_PROXY}' - it should be none, decodo or generic"
        ;;
esac

# ---- the control room's secret ------------------------------------------
# The environment has it first: a secret in a file on disk is a secret that
# can be committed by accident.
export ICLOUD_ADMIN_TOKEN="${ICLOUD_ADMIN_TOKEN:-MySecret123}"

say "the app is on http://localhost:9002"
say "the control room is on http://localhost:9002/admin"
say "the admin secret is $ICLOUD_ADMIN_TOKEN"

# exec: the server is this script from here on, so Ctrl-C stops the server
# and not a shell that is waiting for it.
exec "$PY" server.py "$@"
