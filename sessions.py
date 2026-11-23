"""The session manager.

One place that knows which clients are here, when each of them arrived,
and who wants to be told about it.

Every client passes through here on the way in and on the way out. The
server asks this for a session and hands it back; the panel subscribes to
it and is told when a client connects, when one goes, and when one
changes. So there is one answer to "who is here?" and one place that says
so - not six hooks scattered through the server that each tell the panel
something slightly different.

The manager knows nothing about browsers, captures or pages. It knows
clients: who has arrived, who is still here, who has left, and when.
"""

import os
import re
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).parent

# Where each visitor's browser keeps its profile. A profile is a browser's
# memory: the cookies, the "trust this device" answer, the half-typed
# address. Giving every visitor their own directory is what lets two people
# use the app at the same time without signing in as each other.
#
# It is read through this module by both the server and the panel, so they
# cannot disagree about where a visitor's browser keeps its memory.
PROFILES = Path(os.environ.get("ICLOUD_PROFILES") or (HERE / "visitors")).resolve()

# The cookie that says which visitor this is. It is a name, not a secret:
# it says whose browser this is, and nothing else.
SESSION_COOKIE = "icloud_session"

# What a visitor's name may look like. It is a name the server made, so it
# is checked before it is used as a folder name.
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def visitor_of(request) -> str:
    """The name this request says the visitor is.

    The page keeps the name in its own storage and sends it with the
    request; the cookie says the same thing afterwards, so the name can be
    read either way. The cookie wins when both are there, because it is what
    the server said last.
    """
    kept = request.cookies.get(SESSION_COOKIE, "")
    if kept and SESSION_ID.match(kept):
        return kept
    query = request.scope.get("query_string", b"") or b""
    named = urllib.parse.parse_qs(query.decode("utf-8", "replace")).get(
        "visitor", [""])[0]
    return named if SESSION_ID.match(named) else ""


class SessionManager:
    """The clients the server has, and the news about them.

    The registry lives here, so both the server and the panel can see the
    same one without either of them importing the other. The news lives
    here too: whoever wants to be told subscribes, and is told when a
    client arrives, when one leaves, and when one changes.
    """

    def __init__(self):
        # The clients that are here now: id -> their session.
        self.sessions: dict = {}
        # The clients that have gone: id -> what is remembered of them.
        self.gone: dict = {}
        # When each client's current stay began (epoch seconds).
        self.arrived: dict = {}
        # When each client first came (epoch seconds), across every visit.
        self.first_seen: dict = {}
        # Who is listening.
        self._listeners: list = []

    # ------------------------------------------------------------------
    # Being told
    # ------------------------------------------------------------------

    def subscribe(self, callback) -> None:
        """Ask to be told when a client arrives, leaves or changes.

        The callback is given one event: a dict saying what happened, to
        whom, and when.
        """
        if callback not in self._listeners:
            self._listeners.append(callback)

    def unsubscribe(self, callback) -> None:
        """Stop listening."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def publish(self, event: dict) -> None:
        """Tell everybody who is listening.

        A listener that cannot hear is not allowed to stop the others from
        hearing: the server carries on either way.
        """
        for callback in list(self._listeners):
            try:
                callback(event)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # The clients
    # ------------------------------------------------------------------

    def enter(self, sid: str, session) -> bool:
        """A client has arrived. Returns True if they had been here before.

        Their stay starts now, and everybody who is listening is told -
        with the time it happened, because that is what the panel has to
        show.
        """
        again = self.gone.pop(sid, None) is not None
        # Mutated in place, never rebound: the server and the panel both
        # hold this very dictionary.
        self.sessions[sid] = session
        self.arrived[sid] = time.time()
        self.first_seen.setdefault(sid, self.arrived[sid])
        self.publish({"t": "client", "event": "connected", "id": sid,
                      "at": self.arrived[sid], "returning": again})
        return again

    def lookup(self, sid: str | None):
        """The session this name belongs to, if there is one.

        Looking up is not the same as making: a route that only reads what
        is going on must not give a visitor a session of their own.
        """
        if not sid or not SESSION_ID.match(sid):
            return None
        return self.sessions.get(sid)

    def retire(self, sid: str, record: dict) -> None:
        """A client has gone.

        What they got to is remembered, so the panel can say it: a visitor
        who has gone is still a visitor, and their profile is still on disk.
        """
        record = dict(record)
        record.setdefault("arrived", self.arrived.get(sid, 0.0))
        record["left"] = time.time()
        self.sessions.pop(sid, None)
        self.gone[sid] = record
        self.publish({"t": "client", "event": "left", "id": sid,
                      "at": record["left"]})

    def changed(self, sid: str, **what) -> None:
        """Something about a client has changed.

        They moved to the next step, their browser showed something new, or
        somebody opened or closed their page.
        """
        self.publish({"t": "client", "event": "changed", "id": sid,
                      "at": time.time(), **what})

    # ------------------------------------------------------------------
    # What the panel wants to know
    # ------------------------------------------------------------------

    def connected_at(self, sid: str) -> float:
        """When this client's current stay began."""
        return self.arrived.get(sid, 0.0)

    def seen_at(self, sid: str) -> float:
        """When this client first came, over every visit."""
        return self.first_seen.get(sid, 0.0)

    def connected(self, sid: str) -> bool:
        """Is this client here right now?"""
        return sid in self.sessions

    def connected_count(self) -> int:
        """How many clients are here right now."""
        return len(self.sessions)


# The one manager. The server and the panel both use it, so they cannot
# disagree about who is here.
MANAGER = SessionManager()
