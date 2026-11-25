"""The question asked before a browser is opened.

Opening a browser on this machine costs something - a screen, a profile, a
Chromium - and anybody who knows the address can ask for one. That is the
one door in this app that a stranger can knock on, so it has a question at
it: **are you a person?**

Cloudflare Turnstile answers that without a puzzle: the visitor's page is
given the site's key - which is public, and no secret is in it - and
Cloudflare gives back a token. The token is a claim, not a proof, so the
server asks Cloudflare about it before it opens anything. The secret that
does the asking never leaves this machine.

**The answer that is not good is the answer that stops.** If Cloudflare
cannot be reached, or the token is not good, the browser is not opened.
A check that can be walked around is not a check.

The question is asked of the *visitor's page*, and the answer is verified
here, on the server. The two are kept apart on purpose: a page that could
say "I am a person" would be worth nothing.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# Where Cloudflare answers the question.
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# How long to wait for the answer. Long enough for a slow network, short
# enough that a visitor is not left looking at a black screen.
TIMEOUT = 15

# The name of the question, so that a token made for something else is not
# taken for this. Cloudflare gives the name back with the answer.
ACTION = "start"

# The header the answer travels in. A header rather than the address,
# because an address is written down in logs and a token is no secret once
# it has been used - but it need not be written down at all.
HEADER = "x-icloud-challenge"


def site_key() -> str:
    """The site's key, which is public and goes in the page."""
    return str(os.environ.get("ICLOUD_TURNSTILE_SITE_KEY") or "").strip()


def secret() -> str:
    """The secret, which stays here."""
    return str(os.environ.get("ICLOUD_TURNSTILE_SECRET") or "").strip()


def asked_for(site: str = "", given: str = "") -> bool:
    """Is the question being asked at all?

    Either key being there means the question is asked: a page with the
    site's key and a server with no secret is a misreading, and a
    misreading must not open a door by accident.
    """
    return bool((site or site_key()).strip() or (given or secret()).strip())


async def verify(token: str, given_secret: str = "", ip: str = "",
                 action: str = ACTION) -> dict:
    """Ask Cloudflare whether the token is good.

    Returns a dictionary with ``ok`` - whether the person may through - and
    ``said``, which is what to tell them if not.
    """
    said = {"ok": False, "said": "", "hostname": "", "action": ""}

    token = (token or "").strip()
    if not token:
        said["said"] = "no answer was given"
        return said
    if not given_secret:
        said["said"] = ("the secret is not set, so the answer cannot be "
                        "checked")
        return said

    body = urllib.parse.urlencode({
        "secret": given_secret,
        "response": token,
        "action": action,
        **({"remoteip": ip} if ip else {}),
    }).encode("utf-8")

    request = urllib.request.Request(
        VERIFY_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})

    try:
        import asyncio  # noqa: PLC0415

        def ask():
            with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
                return answer.read().decode("utf-8", "replace")

        raw = await asyncio.to_thread(ask)
    except urllib.error.HTTPError as exc:
        said["said"] = f"cloudflare said {exc.code}"
        return said
    except Exception as exc:
        said["said"] = f"cloudflare could not be reached: {exc}"
        return said

    try:
        answer = json.loads(raw)
    except Exception:
        said["said"] = "cloudflare's answer was not understood"
        return said

    said["hostname"] = str(answer.get("hostname") or "")
    said["action"] = str(answer.get("action") or "")

    if answer.get("success"):
        said["ok"] = True
        said["said"] = "the answer is good"
        return said

    codes = answer.get("error-codes") or []
    said["said"] = "the answer was not good"
    if codes:
        said["said"] += " (" + ", ".join(str(one) for one in codes) + ")"
    return said
