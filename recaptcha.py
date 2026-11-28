"""The question asked before a browser is opened.

Google's answer, and the same question Cloudflare's was: are you a person?

Opening a browser on this machine costs something - a screen, a profile, a
Chromium - and anybody who knows the address can ask for one. That is the one
door in this app that a stranger can knock on, so it has a question at it:
**are you a person?**

reCAPTCHA answers that without a puzzle, in the invisible kind: the visitor's
page is given the site's key - which is public, and no secret is in it - and
Google gives back a token. The token is a claim, not a proof, so the server
asks Google about it before it opens anything. The secret that does the asking
never leaves this machine.

**The answer that is not good is the answer that stops.** If Google cannot be
reached, or the token is not good, the browser is not opened. A check that can
be walked around is not a check.

The question is asked of the *visitor's page*, and the answer is verified
here, on the server. The two are kept apart on purpose: a page that could say
"I am a person" would be worth nothing.

The invisible kind answers with a score as well as a verdict: how likely it is
that the person is a person. The score is said here, and the lowest score
that will do is in the panel - because a score that stops every visitor is a
door that is shut, and a score that stops nobody is not a door at all.

This is the same question ``turnstile.py`` asks, in the same words and to the
same shape, so that the page in front of the visitor does not have to know
which of the two it is answering. One of them is asked at a time: whichever
the panel has the keys for.

**Google has two kinds of key, and they answer in two shapes.** The plain
kind puts the name of the question, the host and the score at the top of
its answer. The Enterprise kind says more, and says it in a second place:
the same three things are in ``tokenProperties``, beside the answer rather
than in it. An answer read from only the top of it is an answer that looks
as though it was given to no question at all - which is how a good answer
comes to be turned away, with a reason that is true of the reading and not
of the answer. So both places are read here, and an answer is understood
the same way whichever kind of key made it.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

# Where Google answers the question.
VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"

# How long to wait for the answer. Long enough for a slow network, short
# enough that a visitor is not left looking at a black screen.
TIMEOUT = 15

# The name of the question, so that a token made for something else is not
# taken for this. Google gives the name back with the answer.
#
# It is set on the widget, in the page, and it comes back with the answer
# when the answer is checked. It is not a thing that is sent to Google when
# the answer is checked: the answer carries it.
#
# It is the same name Cloudflare's answer is given, so that the page does not
# have to be told which question it is answering.
ACTION = "start"

# The longest answer that can be an answer.
#
# Google says an answer may be up to four thousand characters long, and
# says that anything longer is not an answer at all. The plain kind comes
# back in a few hundred characters, but the Enterprise kind comes back
# much longer - it carries the signals it collected inside it - and a
# limit that is too small throws away good answers before Google has
# been asked about them.
MAX_TOKEN = 4000

# The answers Google says are worth asking again.
#
# "browser-error" is Google's own word for a failure in the browser - a
# network trouble while the signals were being collected - and the answer
# it left behind is not good because of it. "timeout-or-duplicate" is an
# answer that has grown old, or one that has been offered before.
#
# Both are the browser's fault rather than the person's, and both are
# answered by asking the question again: a new answer comes back, and a
# new answer is a new answer.
RETRYABLE = ("browser-error", "network-error", "timeout-or-duplicate")

# The score a person has to reach to be let through. Google's own advice for
# the invisible kind is 0.5; anything below that and more than half of the
# people answering are told they are not people.
MIN_SCORE = 0.5

# The widget this app asks. The site's key is public - it is in every copy of
# the page a visitor sees - so it can be written down here. The secret is the
# other half of it, and it is never written down anywhere: it goes in the
# panel, or in the environment.
#
# This is what the panel offers the operator to start from. It is not used
# as a key on its own: a machine with a site's key and no secret is a
# misreading, and a misreading must not open a door by accident.
DEFAULT_SITE_KEY = "6Lf9FDUuAAAAAB6zPOs50VciqI8vH0n2CopPuFG9"

# The kind of widget this is.
#
# Google's reCAPTCHA comes in two kinds, and they are fetched two different
# ways:
#
#   * the invisible kind - v3 - is fetched with the site's key in the
#     address ("?render=THE_SITE_KEY"), and the answer comes on its own
#     with a score beside it. There is no puzzle and there is nothing to
#     answer;
#   * the checkbox kind - v2 - is fetched with "?render=explicit", and is
#     drawn by hand with the key afterwards. The visitor answers it.
#
# Which kind a key is is a property of the key, and the two are told apart
# by the way the script is asked for. So the kind is said here, and the
# page is told which one it is answering.
#
# The invisible kind is the one this is written for.
KIND = "invisible"

# The kinds there are.
KINDS = ("invisible", "checkbox")


def kind() -> str:
    """Which kind of widget the key is."""
    said = str(os.environ.get("ICLOUD_RECAPTCHA_KIND") or "").strip().lower()
    return said if said in KINDS else KIND


# The header the answer travels in. A header rather than the address, because
# an address is written down in logs and a token is no secret once it has
# been used - but it need not be written down at all.
#
# It is the same header Cloudflare's answer travels in, so that a copy of the
# pages already in the field keeps working whichever question is asked.
HEADER = "x-icloud-challenge"


def site_key() -> str:
    """The site's key, which is public and goes in the page."""
    return str(os.environ.get("ICLOUD_RECAPTCHA_SITE_KEY") or "").strip()


def secret() -> str:
    """The secret, which stays here."""
    return str(os.environ.get("ICLOUD_RECAPTCHA_SECRET") or "").strip()


def min_score() -> float:
    """The lowest score that will let a person through."""
    said = str(os.environ.get("ICLOUD_RECAPTCHA_MIN_SCORE") or "").strip()
    if not said:
        return MIN_SCORE
    try:
        return float(said)
    except ValueError:
        return MIN_SCORE


def asked_for(site: str = "", given: str = "") -> bool:
    """Is the question being asked at all?

    Either key being there means the question is asked: a page with the
    site's key and a server with no secret is a misreading, and a
    misreading must not open a door by accident.
    """
    return bool((site or site_key()).strip() or (given or secret()).strip())


def settings(store=None) -> dict:
    """What is set right now, and whether the question is being asked.

    The keys live in the environment, or in the panel's store. The store is
    what the panel writes to, and the panel writes to it while the machine
    is running - so this is asked for at the moment it is wanted, and never
    remembered anywhere else.

    The panel comes first. The panel is the live control: what the operator
    set there is what is used, and the environment is what the machine falls
    back to until the panel says otherwise.

    Three things are said here:

    * ``site_key`` - the key the page is given. It is public.
    * ``switch`` - the panel's switch: the question can be turned off
      without losing the keys, which is the way out when a widget
      misbehaves.
    * ``asked`` - whether the question is really being asked: the switch is
      on, and there is something to ask with. Either key counts, because one
      key without the other is a misreading, and a misreading must not open
      a door by accident.

    Where each key came from is said as well, so that the panel can say it
    out loud rather than the operator guessing.

    The secret is in here too, but it is for this machine. It is never sent
    to a page, and it is not in anything a page is given.
    """
    site = site_key()
    given = secret()
    site_source = "environment"
    secret_source = "environment"
    switch = True
    score = min_score()

    if store is not None:
        try:
            stored = store.recaptcha()
        except Exception:
            stored = {}
        panel_site = str(stored.get("site_key") or "").strip()
        panel_given = str(stored.get("secret") or "").strip()
        if panel_site:
            site = panel_site
            site_source = "panel"
        if panel_given:
            given = panel_given
            secret_source = "panel"
        switch = stored.get("asked", True) is not False
        said_score = stored.get("min_score")
        if isinstance(said_score, (int, float)):
            try:
                score = float(said_score)
            except (TypeError, ValueError):
                pass

    keys = asked_for(site, given)
    said_kind = kind()
    if store is not None:
        try:
            stored_kind = str(store.recaptcha().get("kind") or "").strip().lower()
        except Exception:
            stored_kind = ""
        if stored_kind in KINDS:
            said_kind = stored_kind
    return {
        "site_key": site,
        "secret": given,
        "secret_set": bool(given),
        "site_key_source": site_source,
        "secret_source": secret_source,
        "switch": switch,
        "keys": keys,
        "asked": bool(switch and keys),
        "min_score": score,
        "kind": said_kind,
    }


def host_of(address: str) -> str:
    """The host of an address, and nothing else.

    No scheme, no port, no path, and all in lower case - because that is
    the shape Google gives a host back in, and a host is compared to be
    the same host.
    """
    said = (address or "").strip()
    found = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", said, re.IGNORECASE)
    if found:
        said = found.group(1)
    if "@" in said:
        said = said.split("@")[-1]
    if said.startswith("["):
        said = said[1:].split("]")[0]        # an address in brackets
    else:
        said = said.split(":")[0]
    return said.strip().lower()


def expected_hosts(store=None, request_host: str = "") -> set:
    """The hosts an answer may come from.

    An answer is given on a host, and Google says which one. So the server
    has to say which hosts it will take an answer from: the address this
    server is reached at, the address the copy of the pages lives at, and
    anything said outright in ``ICLOUD_RECAPTCHA_HOSTNAMES``. And the host
    the visitor used to get here - an answer is given on a host, and the
    host it was given on is the one it is checked against.

    The copy of the pages has to be told where it lives for the browser to
    let it ask this server anything at all, so its address is here too. An
    answer from a copy that has not been registered is an answer this server
    will not take.
    """
    said = set()

    for one in os.environ.get("ICLOUD_RECAPTCHA_HOSTNAMES", "").split(","):
        host = host_of(one)
        if host:
            said.add(host)

    if store is not None:
        try:
            stored = store.standalone()
        except Exception:
            stored = {}
        for one in (stored.get("server", ""), stored.get("origin", "")):
            host = host_of(one)
            if host:
                said.add(host)

    for one in os.environ.get("ICLOUD_STANDALONE_ORIGINS", "").split(","):
        host = host_of(one)
        if host:
            said.add(host)

    host = host_of(request_host)
    if host:
        said.add(host)

    return said


def _properties(answer: dict) -> dict:
    """The properties Google puts beside the answer.

    Google's Enterprise answer says more than the plain one does, and it
    says it in a second place: the name of the question, the host the
    answer was given on and the score are in ``tokenProperties`` as well
    as at the top of the answer. Both places are read here, so that an
    answer is understood the same way whichever kind of key made it.
    """
    said = {}
    for name in ("tokenProperties", "token_properties"):
        one = answer.get(name) if isinstance(answer, dict) else None
        if isinstance(one, dict):
            said.update(one)
    return said


def _field(answer: dict, props: dict, *names):
    """The first of these names that Google has given something to.

    The plain part of the answer is looked at first, and the properties
    beside it second. A zero is a value: a score of nothing is still a
    score, and it is a score somebody is turned away for.
    """
    for source in (answer, props):
        if not isinstance(source, dict):
            continue
        for name in names:
            value = source.get(name)
            if value is not None and value != "":
                return value
    return None


def _score_of(answer: dict, props: dict):
    """The score Google gave, or nothing if it gave no score.

    A checkbox has no score - it says yes or no, and that is the whole of
    it - so an answer with no score is not turned away for the lack of one.

    The score is looked for in the places Google puts it: at the top of the
    answer, in the properties beside it, and in the risk analysis. Where
    more than one is given, the one that is not zero is the one - a zero
    beside a real score is a placeholder and not a verdict, and a visitor
    is not turned away for a placeholder.
    """
    found = []
    for source in (answer, props):
        if isinstance(source, dict) and source.get("score") is not None:
            found.append(source.get("score"))
    for name in ("riskAnalysis", "risk_analysis"):
        risk = answer.get(name) if isinstance(answer, dict) else None
        if isinstance(risk, dict) and risk.get("score") is not None:
            found.append(risk.get("score"))
    if not found:
        return None
    for one in found:
        try:
            if float(one) > 0.0:
                return one
        except (TypeError, ValueError):
            continue
    return found[0]


async def verify(token: str, given_secret: str = "", ip: str = "",
                 action: str = ACTION, hostnames=None, score: float = MIN_SCORE) -> dict:
    """Ask Google whether the token is good.

    Returns a dictionary with ``ok`` - whether the person may through - and
    ``said``, which is what to tell them if not.

    Four things are asked of the answer, and all four have to be right:

    * that Google says the answer is good;
    * that the answer was given to *this* question - the action is set on
      the widget and comes back with the answer, so an answer made for
      something else is not taken for this one;
    * that the answer was given on a host this server knows about. The host
      comes back with the answer too, and the hosts it may come from are the
      deployment's own - the address the server is reached at and the address
      the copy of the pages lives at; and
    * that the score is high enough. The invisible kind says how likely it
      is that the person is a person, and a score below the one the panel
      asks for is a person who is not let through.

    Google's answer is read the same way whichever kind of key made it.
    The Enterprise kind says the name of the question, the host and the
    score in a second place, beside the answer rather than in it, and an
    answer read from only the top of it is an answer that looks like it
    was given to no question at all - which is how a good answer comes to
    be turned away.

    When Google names a question and it is not this one, the answer is not
    taken. When Google names no question at all, there is nothing to
    compare, and an answer that is good and came from a host this server
    knows about is a good answer.

    A token is single use. Google says so the second time one is offered,
    and so does this: an answer that is replayed is not an answer.

    ``retry`` says the answer is worth asking again: Google's own word for
    it is ``browser-error``, and it means the browser had a bad moment and
    not that the person did.

    ``hostnames`` may be left out when there is nothing to say about it;
    then the host is not checked. The server always says what the hosts are,
    so it is always checked there.
    """
    said = {"ok": False, "said": "", "hostname": "", "action": "",
            "codes": [], "reason": "", "score": 0.0, "retry": False,
            "raw": {}}

    token = (token or "").strip()
    if not token:
        said["said"] = "no answer was given"
        said["reason"] = "no-answer"
        return said
    if len(token) > MAX_TOKEN:
        said["said"] = "the answer was too long to be an answer"
        said["reason"] = "too-long"
        return said
    if not given_secret:
        said["said"] = ("the secret is not set, so the answer cannot be "
                        "checked")
        said["reason"] = "no-secret"
        return said

    # What Google is asked: the secret, the answer, and where the answer came
    # from. The question's name is not sent - it is on the widget, and it
    # comes back with the answer.
    body = urllib.parse.urlencode({
        "secret": given_secret,
        "response": token,
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
        said["said"] = f"google said {exc.code}"
        said["reason"] = "unreachable"
        return said
    except Exception as exc:
        said["said"] = f"google could not be reached: {exc}"
        said["reason"] = "unreachable"
        return said

    try:
        answer = json.loads(raw)
    except Exception:
        said["said"] = "google's answer was not understood"
        said["reason"] = "unreadable"
        return said

    # Google's whole answer is kept, so that the log can say what Google
    # actually said rather than what this file made of it.
    said["raw"] = answer

    props = _properties(answer)
    said["hostname"] = str(_field(answer, props, "hostname") or "")
    said["action"] = str(_field(answer, props, "action") or "")
    codes = answer.get("error-codes") or answer.get("error_codes") or []
    said["codes"] = [str(one) for one in codes]

    # The score, if there is one. A score of nothing is a score.
    given_score = _score_of(answer, props)
    has_score = given_score is not None
    try:
        said["score"] = float(given_score) if has_score else 0.0
    except (TypeError, ValueError):
        said["score"] = 0.0
        has_score = False

    if not answer.get("success"):
        said["said"] = "the answer was not good"
        if codes:
            said["said"] += " (" + ", ".join(str(one) for one in codes) + ")"
        said["reason"] = "not-good"
        said["retry"] = any(str(one).lower() in RETRYABLE
                            for one in said["codes"])
        return said

    # The answer must have been given to this question - but only if Google
    # said which question it was given to. An answer that names no question
    # is not an answer to another question; it is an answer with nothing
    # said about it, and the host below is what is checked instead.
    if action and said["action"] and said["action"] != action:
        said["said"] = (f"the answer was given to another question "
                        f"({said['action']}), not to this one")
        said["reason"] = "action"
        return said

    # And it must have been given on a host this server knows about.
    if hostnames:
        host = said["hostname"].lower()
        if host not in hostnames:
            said["said"] = (f"the answer came from "
                            f"{said['hostname'] or 'a host with no name'}, "
                            f"which this server does not know")
            said["reason"] = "hostname"
            return said

    # The score is only there for the invisible kind. A checkbox says yes or
    # no and has no score, and a good answer is a good answer.
    if has_score and said["score"] < score:
        said["said"] = (f"the answer was given, but it scored "
                        f"{said['score']:.2f} and the lowest score that will "
                        f"do is {score:.2f}")
        said["reason"] = "score"
        return said

    said["ok"] = True
    said["said"] = "the answer is good"
    return said
