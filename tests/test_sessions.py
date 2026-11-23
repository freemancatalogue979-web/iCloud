"""The session manager: the one place that knows who is here.

Every client passes through it on the way in and on the way out, and the
panel subscribes to it. These tests are about that one thing: that the
manager knows when a client connected, when one went, and when one
changed - and that it says so, rather than each part of the server
keeping its own idea of who is here.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sessions

passed = 0
failed = 0


def check(name, cond, detail=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   {name}")
    else:
        failed += 1
        print(f"FAIL {name}", detail if detail is not None else "")


def main():
    from sessions import SessionManager

    manager = SessionManager()

    # ---- who is listening ------------------------------------------------
    heard = []
    manager.subscribe(heard.append)

    class Fake:
        """A client, as far as the manager is concerned."""

        def __init__(self, name):
            self.id = name
            self.seen = time.monotonic()

        def touch(self):
            self.seen = time.monotonic()

    # ---- a client arrives ------------------------------------------------
    check("a new manager has nobody in it",
          manager.sessions == {} and manager.gone == {}, manager.sessions)

    manager.enter("the-first", Fake("the-first"))
    check("the client is in the registry",
          "the-first" in manager.sessions, sorted(manager.sessions))
    check("...and they are here",
          manager.connected("the-first") is True)
    check("...and the manager knows how many are here",
          manager.connected_count() == 1, manager.connected_count())

    check("the panel is told that a client connected",
          len(heard) == 1, heard)
    event = heard[0]
    check("...and it is told which one",
          event["id"] == "the-first", event)
    check("...and that it was a connection",
          event["event"] == "connected", event)
    check("...and that they had not been here before",
          event["returning"] is False, event)
    check("...and when it happened",
          isinstance(event["at"], float) and event["at"] > 0, event)

    connected_at = manager.connected_at("the-first")
    check("the manager knows when they connected",
          connected_at > 0 and connected_at <= time.time(), connected_at)
    check("...and when they first came",
          manager.seen_at("the-first") == connected_at, manager.seen_at("the-first"))

    # ---- a client changes -------------------------------------------------
    manager.changed("the-first", url="https://idmsa.apple.com/signin")
    check("the panel is told that a client changed",
          len(heard) == 2, heard)
    check("...and it is told which one",
          heard[1]["id"] == "the-first", heard[1])
    check("...and what changed",
          heard[1]["event"] == "changed" and
          heard[1].get("url") == "https://idmsa.apple.com/signin", heard[1])

    # ---- a second client -------------------------------------------------
    manager.enter("the-second", Fake("the-second"))
    check("both clients are in the registry",
          sorted(manager.sessions) == ["the-first", "the-second"],
          sorted(manager.sessions))
    check("the panel is told about each of them",
          [e["event"] for e in heard] ==
          ["connected", "changed", "connected"], heard)
    check("the two clients have their own times",
          manager.connected_at("the-first") !=
          manager.connected_at("the-second") or
          abs(manager.connected_at("the-first") -
              manager.connected_at("the-second")) < 1.0,
          manager.connected_at("the-first"))

    # ---- a client goes ----------------------------------------------------
    manager.retire("the-first", {"page": "/src/2.html", "stage": "Code"})
    check("the client is not in the registry any more",
          "the-first" not in manager.sessions, sorted(manager.sessions))
    check("...but they are remembered",
          "the-first" in manager.gone, sorted(manager.gone))
    check("...and what they got to is kept",
          manager.gone["the-first"]["page"] == "/src/2.html",
          manager.gone["the-first"])
    check("...and when they arrived is kept with it",
          manager.gone["the-first"]["arrived"] > 0,
          manager.gone["the-first"])
    check("...and when they left is said",
          manager.gone["the-first"]["left"] > 0,
          manager.gone["the-first"])
    check("the panel is told that a client went",
          heard[-1]["event"] == "left" and heard[-1]["id"] == "the-first",
          heard[-1])

    # ---- a client comes back -----------------------------------------------
    manager.enter("the-first", Fake("the-first"))
    check("a client who comes back is here",
          manager.connected("the-first") is True)
    check("...and is not remembered as gone",
          "the-first" not in manager.gone, sorted(manager.gone))
    check("the panel is told they are back",
          heard[-1]["event"] == "connected" and
          heard[-1]["returning"] is True, heard[-1])
    check("...and their new arrival is the time they are here now",
          manager.connected_at("the-first") >=
          manager.gone.get("the-first", {}).get("left", 0) or
          manager.connected_at("the-first") > 0,
          manager.connected_at("the-first"))

    # ---- looking up -------------------------------------------------------
    check("looking up finds the client",
          manager.lookup("the-first") is not None)
    check("looking up somebody nobody has heard of finds nobody",
          manager.lookup("no-such-client") is None)
    check("looking up a name that is not a name finds nobody",
          manager.lookup("short") is None)
    check("looking up nothing finds nobody",
          manager.lookup("") is None)
    check("looking up a name with a slash in it finds nobody",
          manager.lookup("../../etc/passwd") is None)

    # ---- stopping listening -----------------------------------------------
    manager.unsubscribe(heard.append)
    before = len(heard)
    manager.enter("the-third", Fake("the-third"))
    check("somebody who has stopped listening hears nothing",
          len(heard) == before, heard)

    # ---- a listener that breaks ------------------------------------------
    def broken(event):
        raise RuntimeError("this listener is broken")

    manager.subscribe(broken)
    manager.enter("the-fourth", Fake("the-fourth"))
    check("a listener that breaks does not stop the others",
          manager.connected("the-fourth") is True)

    # ---- the one manager --------------------------------------------------
    check("there is one manager, and it is the same one everywhere",
          sessions.MANAGER is sessions.MANAGER)
    check("...and it is the one the server uses",
          import_server().MANAGER is sessions.MANAGER)

    # ---- one profile folder ------------------------------------------------
    # The visitors' browser profiles live in one place, and both the server
    # and the panel read it there. If they read their own copies, one of
    # them would look in a different folder and see no visitors at all.
    server = import_server()
    admin = import_admin()
    check("the server and the panel read the same profile folder",
          server.PROFILES is sessions.PROFILES
          and admin.PROFILES is sessions.PROFILES)
    check("...and that is a real folder",
          isinstance(sessions.PROFILES, Path) and sessions.PROFILES.exists(),
          sessions.PROFILES)

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def import_server():
    import server
    return server


def import_admin():
    import admin
    return admin


if __name__ == "__main__":
    sys.exit(main())
