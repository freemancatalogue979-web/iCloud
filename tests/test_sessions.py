"""The session manager: the one door every client comes through.

Two things are pinned here:

  * the manager really is the door - a session is made by ``arrive``, found
    by ``lookup`` and put away by ``retire``, and each of those is said to
    whoever subscribed - and
  * the panel is a subscriber. A client that comes through the door reaches
    the panel without the server telling the panel anything, which is the
    whole point: there is one list, and the panel is told about it.

The sessions in the first half are stand-ins, so no browser or hub is
needed. The second half uses the server's own ``Session``, because that is
what a client is in the app.

Run with: python tests/test_sessions.py
"""

import asyncio
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin  # noqa: E402
import server  # noqa: E402
import sessions  # noqa: E402

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


class FakeHub:
    """What a session is holding, so the manager has something to look at."""

    def __init__(self):
        self.page = "/src/1.html"
        self.browser = {"url": "", "title": "", "fields": {}}
        self.finished = False
        self.viewers = set()


class FakeSession:
    """A session, without a browser behind it."""

    def __init__(self, sid):
        self.id = sid
        self.hub = FakeHub()
        self.state = {"status": "idle", "step": "", "error": "",
                      "browser_open": False}
        self.waiting = None
        self.seen = time.monotonic()

    def touch(self):
        self.seen = time.monotonic()


class FakeListener:
    """A panel on the other end of the line."""

    def __init__(self, secret=""):
        self.sent = []
        self.secret = secret
        self.closed = None

    def put_nowait(self, message):
        self.sent.append(message)

    def close(self, code=1000):
        self.closed = code


class FakeViewer:
    def send(self, message):
        pass

    def close(self):
        pass


def rows_of(message, sid):
    return [row for row in message.get("clients", []) if row["id"] == sid]


async def main():
    # ---- the door --------------------------------------------------------
    profiles = Path(tempfile.mkdtemp())
    manager = sessions.SessionManager(profiles)

    said = []
    stop = manager.subscribe(said.append)

    session, how = manager.arrive("the-first", FakeSession)
    check("a client who comes through the door is new",
          how == "new", how)
    check("...and the manager is holding them",
          "the-first" in manager and len(manager) == 1, manager.session_ids())
    check("...and it said so",
          [event["kind"] for event in said] == ["connected"], said)
    check("...with the client's name on it",
          said[0]["session"] == "the-first", said[0])

    again, how = manager.arrive("the-first", FakeSession)
    check("the same name is the same client",
          again is session and how == "here", (again, how))
    check("...and nobody is told about a client who was already here",
          len(said) == 1, said)

    # A name the manager did not make is not a name it will use as a folder.
    made, _ = manager.arrive("../etc/passwd", FakeSession)
    check("a name that is not a name is given one",
          made.id != "../etc/passwd" and
          sessions.SESSION_ID.match(made.id), made.id)

    # Looking is not making.
    before = len(manager)
    check("a stranger has no session",
          manager.lookup("no-such-client") is None, manager.session_ids())
    check("...and looking them up made none",
          len(manager) == before, manager.session_ids())

    # ---- going, and coming back ------------------------------------------
    manager.retire("the-first", {"page": "/src/2.html",
                                 "stage": "Verification code"})
    check("a client who is put away is no longer held",
          "the-first" not in manager and len(manager) == before - 1,
          manager.session_ids())
    check("...and is remembered as one who has gone",
          manager.gone["the-first"]["stage"] == "Verification code",
          manager.gone)
    check("...and the manager said they had gone",
          said[-1]["kind"] == "gone" and said[-1]["session"] == "the-first",
          said[-1])

    back, how = manager.arrive("the-first", FakeSession)
    check("a client who comes back is back, not new",
          how == "back", how)
    check("...and is not still remembered as gone",
          "the-first" not in manager.gone, manager.gone)
    check("...and the manager says so",
          said[-1]["kind"] == "back", said[-1])

    # ---- what changed -----------------------------------------------------
    revision = manager.revision
    said.clear()
    manager.changed(back, why="page")
    check("a change is said out loud",
          said and said[-1]["kind"] == "changed" and
          said[-1]["why"] == "page", said)
    check("...and it is numbered",
          manager.revision > revision and said[-1]["revision"] ==
          manager.revision, said[-1])

    # A change nobody announced is still noticed. This is the safety net:
    # whatever moved the page, the panel is told what the manager holds.
    kept = back.hub.page
    back.hub.page = "/src/4.html"
    said.clear()
    moved = manager.sync()
    check("a change nobody announced is noticed",
          moved == ["the-first"], moved)
    check("...and the panel is told",
          said and said[-1]["why"] == "poll" and
          said[-1]["sessions"] == ["the-first"], said)
    check("...and it is not said again until it changes again",
          manager.sync() == [] and len(said) == 1, (manager.sync(), said))
    back.hub.page = kept
    manager.sync()             # the page going back is a change as well

    # The number of people looking is a change; the moment they last typed
    # is not - that would redraw the panel on every keystroke.
    said.clear()
    manager.touch("the-first")
    check("a visitor typing is not a redraw",
          manager.sync() == [] and not said, said)
    back.hub.viewers.add(FakeViewer())
    check("...but somebody arriving at the page is",
          manager.sync() == ["the-first"], manager.sync())
    back.hub.viewers.clear()
    manager.sync()

    # ---- what a client is, outside the manager ---------------------------
    folder = profiles / "the-one-from-before"
    folder.mkdir(parents=True, exist_ok=True)
    (profiles / "not a client").mkdir(exist_ok=True)
    check("a profile on disk is a client from an earlier run",
          manager.on_disk() == ["the-one-from-before"], manager.on_disk())
    check("...and the manager knows them", manager.knows("the-one-from-before"))
    check("...and knows where their browser's memory is",
          manager.profile("the-one-from-before") == folder,
          manager.profile("the-one-from-before"))
    check("a folder that is not a client's name is not a client",
          not manager.knows("not a client"), manager.on_disk())
    check("a client the manager has never met is not one",
          not manager.knows("nobody-at-all"))

    # ---- being told ------------------------------------------------------
    said.clear()

    def broken(event):
        raise RuntimeError("this subscriber has stopped listening")

    manager.subscribe(broken)
    stop_second = manager.subscribe(said.append)
    still, _ = manager.arrive("the-last", FakeSession)
    check("a subscriber that breaks does not break the others",
          [event["kind"] for event in said] == ["connected"], said)

    stop_second()
    said.clear()
    manager.changed(still, why="page")
    check("a subscriber that stops is not told again", not said, said)
    stop()
    manager.retire("the-last", {})
    manager.retire("the-first", {})

    # ---- the server goes through the manager -----------------------------
    check("the server's sessions are the manager's",
          server.SESSIONS is sessions.MANAGER)
    check("...and the record of the ones who have gone is too",
          server.GONE is sessions.MANAGER.gone)
    check("...and the panel subscribes to the same manager",
          admin.heard in sessions.MANAGER.watchers,
          sessions.MANAGER.watchers)
    check("the panel does not reach into the server for the clients",
          not hasattr(admin, "_server"), dir(admin))

    # ---- the panel hears a client connect --------------------------------
    # This is the thing the panel got wrong: a client comes through the
    # door, and the panel is told - not because the server told the panel,
    # but because the panel is subscribed to the door.
    scratch = Path(tempfile.mkdtemp())
    admin.STORE = admin.Store(scratch / "panel.json")
    admin.STORE.set_token("the-secret")

    listener = FakeListener(secret="the-secret")
    admin.LISTENERS.add(listener)
    try:
        client = server.session_for("the-manager-test")
        check("the server's client is the manager's client",
              sessions.MANAGER.get("the-manager-test") is client,
              sessions.MANAGER.session_ids())
        check("the panel is told about a client who connects",
              len(listener.sent) >= 1, listener.sent)
        message = listener.sent[-1]
        check("...and what it is told is the client list",
              "clients" in message and message["t"] == "state", message)
        rows = rows_of(message, "the-manager-test")
        check("...with the new client on it",
              len(rows) == 1, message.get("clients"))
        check("...said to be connected",
              rows[0]["live"] is True, rows)
        check("...and counted as connected",
              message["counts"]["live"] == len(sessions.MANAGER),
              message["counts"])
        check("...and the connection is in the feed",
              any(event.get("event") == "connected" and
                  event.get("session") == "the-manager-test"
                  for event in message["events"]),
              message["events"])

        # A page that moves reaches the panel, without the server pushing.
        listener.sent.clear()
        client.hub.navigate("/src/3.html")
        check("the panel is told when a client moves on",
              bool(listener.sent), listener.sent)
        rows = rows_of(listener.sent[-1], "the-manager-test")
        check("...and where they got to",
              rows and rows[0]["stage"] == "Address", rows)

        # Somebody looking at the capture is a person being here.
        listener.sent.clear()
        client.hub.join(FakeViewer())
        rows = rows_of(listener.sent[-1], "the-manager-test")
        check("the panel is told when somebody is at the app",
              rows and rows[0]["here"] is True, rows)
        listener.sent.clear()
        client.hub.leave(next(iter(client.hub.viewers), FakeViewer()))
        rows = rows_of(listener.sent[-1], "the-manager-test")
        check("...and when they look away",
              rows and rows[0]["here"] is False, rows)

        # A browser that opened without saying so is still noticed.
        listener.sent.clear()
        client.state["browser_open"] = True
        sessions.MANAGER.sync()
        rows = rows_of(listener.sent[-1], "the-manager-test")
        check("the panel is told when a browser opens unannounced",
              rows and rows[0]["online"] is True, rows)

        # And when the client goes, the panel is told that too.
        listener.sent.clear()
        sessions.MANAGER.retire("the-manager-test", {
            "page": "/src/2.html", "stage": "Verification code",
            "seen": time.time(),
        })
        rows = rows_of(listener.sent[-1], "the-manager-test")
        check("the panel is told when a client goes",
              rows and rows[0]["status"] == "gone", rows)
        check("...and where they got to",
              rows and rows[0]["stage"] == "Verification code", rows)
        check("...and it is in the feed",
              any(event.get("event") == "gone" for event in
                  listener.sent[-1]["events"]), listener.sent[-1]["events"])
    finally:
        admin.LISTENERS.discard(listener)
        sessions.MANAGER.gone.pop("the-manager-test", None)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
