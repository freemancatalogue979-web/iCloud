"""The question at the door, asked by the capture itself.

Every capture carries the question: the page asks it before the page does
anything, and the server keeps the content of the session back until the
answer has been checked and found good.

What is pinned here:

  * the block is written once, in ``door.py``, and every capture carries
    that same block - so the five pages cannot drift apart;
  * a capture asks the server whether its session has answered already, and
    the server says, and gives the page the question to ask;
  * a capture whose address names a session is served to it - the page asks
    the question itself - while a capture whose address names nothing is
    sent to the launcher as before;
  * and the content of a session is not given out until the session has
    passed: the other half of the door, and the half that cannot be walked
    around, because it is the server that decides.

Run with: python tests/test_door.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The profiles and the group map are told where to go before anything is
# imported, because both are read at import time.
scratch = tempfile.mkdtemp(prefix="icloud-door-")
os.environ["ICLOUD_PROFILES"] = os.path.join(scratch, "visitors")
os.environ["ICLOUD_STATE"] = os.path.join(scratch, "state")
os.environ["ICLOUD_ADMIN_STORE"] = os.path.join(scratch, "admin.json")

from starlette.requests import Request  # noqa: E402

import admin  # noqa: E402
import door  # noqa: E402
import server  # noqa: E402

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


def make_request(path="/challenge/state", query="", cookies=None, method="GET"):
    """A request the way the server hands one over."""
    payload = b""
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    headers = []
    for name, value in (cookies or {}).items():
        headers.append((b"cookie", f"{name}={value}".encode()))
    # The query string is the raw thing, without the question mark in front
    # of it: that is what the server hands over.
    query = str(query or "")
    if query.startswith("?"):
        query = query[1:]
    scope = {"type": "http", "method": method, "path": path,
             "headers": headers,
             "query_string": query.encode("utf-8")}
    return Request(scope, receive)


def as_json(answer):
    """What a route answered, as data."""
    if isinstance(answer, dict):
        return answer
    try:
        return json.loads(bytes(answer.body).decode("utf-8"))
    except Exception:
        return {}


async def main():
    # ---- the block, and the pages that carry it ---------------------------
    check("the door is a block of its own", door.BLOCK and door.MARK in door.BLOCK)
    check("the block asks the server about the question",
          "/challenge/state" in door.BLOCK and "X-Icloud-Challenge" in door.BLOCK)
    check("...and it does not decide anything itself",
          "grecaptcha" in door.BLOCK and "verify" not in door.BLOCK.lower())

    page = "<!DOCTYPE html><html><head><title>x</title></head><body>the page</body></html>"
    with_door = door.insert(page)
    check("a capture carries the door", door.has_door(with_door))
    check("...and the door is before the body, so it is read first",
          with_door.find(door.MARK) < with_door.find("<body"))
    check("...and putting it in again changes nothing",
          door.insert(with_door) == with_door)

    # Every capture in the repository carries the block as it is written now:
    # if the block is changed, the pages are to be changed with it.
    captures = sorted(p for p in (Path(server.SRC)).glob("*.html"))
    check("there are captures to ask", len(captures) >= 5, len(captures))
    for capture in captures:
        text = capture.read_text(encoding="utf-8", errors="replace")
        check(f"{capture.name} carries the door as it is written",
              door.BLOCK in text, "the block is not in the page")
        check(f"{capture.name} keeps its own content",
              len(text) > 100000, len(text))

    # ---- the server, and what it says to a page ---------------------------
    # The panel is asked a question in the test, so that there is one to ask.
    admin.STORE = admin.Store(Path(scratch) / "panel.json")
    admin.STORE.set_recaptcha(
        site_key="6Lf9FDUuAAAAAB6zPOs50VciqI8vH0n2CopPuFG9",
        secret="a-secret",
        asked=True,
        min_score=0.5,
        kind="invisible")

    said = as_json(await server.challenge_state(make_request()))
    check("the server says what the question is",
          said.get("provider") == "recaptcha" and
          said.get("site_key", "").startswith("6Lf9FDUu") and
          said.get("kind") == "invisible", said)
    check("...and that it is asked", said.get("asked") is True, said)
    check("...and that nobody has answered it yet",
          said.get("passed") is False, said)

    # A session that has answered the question.
    session = server.session_for("the-session")
    session.passed = True
    said = as_json(await server.challenge_state(
        make_request(query="?sid=the-session")))
    check("a session that has answered is said to have answered",
          said.get("passed") is True, said)

    # A session named in the address is found by the address.
    said = as_json(await server.challenge_state(
        make_request(query="?sid=the-session")))
    check("the session in the address is the one that is asked about",
          said.get("passed") is True, said)

    # ---- the captures, and who may have them ------------------------------
    check("a capture is a page", server.is_capture_page("1.html"))
    check("...and the flow is not", not server.is_capture_page("flow.json"))

    # An address that names a session: the name has to be there, and it has
    # to be a name a session could have. The session itself need not be here
    # yet - the capture asks the question itself, and the session is made
    # when the answer comes.
    check("an address that names a session names one",
          server.names_a_session(make_request(query="?sid=the-session")))
    check("...and one that names nothing names none",
          not server.names_a_session(make_request(query="?sid=short")))
    check("...and an address with no names names none",
          not server.names_a_session(make_request()))
    check("...and a name that is not a session's name names none",
          not server.names_a_session(make_request(query="?sid=not%20a%20name")))

    # ---- the content, and when it is given out ----------------------------
    hub = server.Hub()
    hub.session = server.session_for("the-session")
    hub.session.passed = True
    check("a session that has answered may see the content",
          hub.may_see_content())

    waiting = server.session_for("another-session")
    hub.session = waiting
    check("a session that has not answered may not",
          not hub.may_see_content())

    # What a viewer is told when it arrives.
    class FakeViewer:
        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)

    viewer = FakeViewer()
    hub.join(viewer)
    kinds = [message.get("t") for message in viewer.sent]
    check("a viewer of a session that has not answered is told no content",
          "state" not in kinds and "browser" not in kinds, kinds)
    check("...but it is told where the session stands",
          "session" in kinds, kinds)

    viewer = FakeViewer()
    hub.session.passed = True
    hub.join(viewer)
    kinds = [message.get("t") for message in viewer.sent]
    check("a viewer of a session that has answered is given the content",
          "state" in kinds and "browser" in kinds, kinds)

    # ---- the policy, and the widget's hosts ------------------------------
    policy = ("default-src 'none'; font-src 'self' data:; img-src 'self' data:; "
              "style-src 'unsafe-inline'; media-src 'self' data:; "
              "script-src 'unsafe-inline' data:; object-src 'self' data:; "
              "frame-src 'self' data:; form-action 'none'; base-uri 'none';")
    relaxed = server._relax_policy(policy)
    check("the widget's script is allowed",
          "https://www.google.com/recaptcha/" in relaxed, relaxed)
    check("the widget's frame is allowed",
          "frame-src" in relaxed and "https://www.google.com/recaptcha/" in relaxed,
          relaxed)
    check("the engine is still allowed",
          "'self'" in relaxed, relaxed)
    check("...and what the capture forbade is still forbidden",
          "form-action 'none'" in relaxed and "base-uri 'none'" in relaxed,
          relaxed)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
