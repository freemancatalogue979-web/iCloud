"""The session manager: the one door every client comes through.

A client is a visitor, and a visitor is a session: a name, a browser of
their own, and a profile folder that is that browser's memory. This module
is where a session is made, where it is handed out, and where it is put
away - and every one of those says so, once, to whoever asked to be told.

That is the point of it. Nothing else keeps a list of the clients: the
control room does not reach into the server for one, and the server does
not tell the control room when to look. The manager is the list, and the
panel is a subscriber. It hears "connected" when a client comes through
the door, "gone" when their browser is closed, and "changed" when what a
client is doing changes - instead of the panel asking a table every two
seconds whether anything has happened.

The manager says what it knows; it does not draw, and it does not decide
what a panel should show. That is the panel's work, and the panel is told.
"""

import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).parent

# Where each visitor's browser keeps its profile. A profile is a browser's
# memory: the cookies, the "trust this device" answer, the half-typed
# address. Giving every visitor their own directory is what lets two people
# use the app at the same time without signing in as each other.
PROFILES = Path(os.environ.get("ICLOUD_PROFILES")
                or (HERE / "visitors")).resolve()

# The cookie that says which visitor this is. It is a name, not a secret:
# it says whose browser this is, and nothing else.
SESSION_COOKIE = "icloud_session"

# What a visitor's name may look like. It is a name the server made, so it
# is checked before it is used as a folder name.
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def new_session_id() -> str:
    """A name for a visitor who has not been here before."""
    return secrets.token_urlsafe(12)


def visitor_of(request) -> str:
    """The name this request says the visitor is.

    The page keeps the name in its own storage and sends it with the
    request; the cookie says the same thing afterwards, so the name can be
    read either way. The cookie wins when both are there, because it is what
    the server said last.

    This only reads. A request that names nobody gets nobody: it is not the
    manager's place to invent a visitor for a page that has not said who it
    is.
    """
    kept = request.cookies.get(SESSION_COOKIE, "")
    if kept and SESSION_ID.match(kept):
        return kept
    query = request.scope.get("query_string", b"") or b""
    named = urllib.parse.parse_qs(
        query.decode("utf-8", "replace")).get("visitor", [""])[0]
    return named if SESSION_ID.match(named) else ""


class SessionManager:
    """Every client, in one place.

    The manager is a mapping of the sessions that are here right now - so
    reading it is what it always was - but it is not written to from the
    outside. A session is made by ``arrive``, found by ``lookup``, and put
    away by ``retire``, and each of those tells the subscribers.

    A client who has gone is kept in ``gone``: their browser is shut, but
    their profile is on disk, and the panel still has something to say
    about them. A profile on disk with no session and no record is a client
    from an earlier run of the server, and ``on_disk`` is how they are
    found.
    """

    def __init__(self, profiles: Path = PROFILES):
        self.profiles = Path(profiles)
        # The sessions that are here now, by name.
        self.live: dict = {}
        # The ones who have gone, by name: what they got to, and when.
        self.gone: dict = {}
        # Bumped on every change, so a panel can tell a new picture from an
        # old one it has already drawn.
        self.revision = 0
        # What the manager said last, per session. This is how a change
        # that nobody announced - a page moved by a route that forgot to
        # say so - is still noticed, and still told.
        self._prints: dict = {}
        # The panels, and anything else that asked to be told.
        self._watchers: list = []

    # -- reading it, the way a mapping is read ---------------------------
    #
    # The manager is the list, so code that only reads the clients reads
    # it here. Nothing writes to it this way: ``here[...] = session`` is
    # not how a session comes to exist, and it would not be announced.

    def __len__(self) -> int:
        return len(self.live)

    def __iter__(self):
        return iter(self.live)

    def __contains__(self, sid) -> bool:
        return sid in self.live

    def __getitem__(self, sid):
        return self.live[sid]

    def get(self, sid, default=None):
        return self.live.get(sid, default)

    def keys(self):
        return self.live.keys()

    def values(self):
        return self.live.values()

    def items(self):
        return self.live.items()

    def session_ids(self) -> list[str]:
        return list(self.live)

    # -- the door ---------------------------------------------------------

    def arrive(self, sid, make):
        """The session this name belongs to, made if there is none.

        This is the only way a client gets a session, and it is the only
        place "connected" is said. ``make`` is what builds one - the server
        passes its ``Session`` - so the manager does not have to know what a
        browser, a hub or a capture is to know who is here.

        Returns the session and what happened: ``"new"`` for a visitor the
        manager had not heard of, ``"back"`` for one whose profile was still
        remembered, and ``"here"`` for one who is already in.
        """
        if not sid or not SESSION_ID.match(str(sid)):
            sid = new_session_id()

        session = self.live.get(sid)
        if session is not None:
            self.touch(sid)
            return session, "here"

        back = self.gone.pop(sid, None) is not None
        session = make(sid)
        self.live[sid] = session
        self._prints[sid] = self._print(session)
        # A visitor who comes back is here, not gone: the record of their
        # last visit is dropped, so nobody says they have left while they
        # are sitting right there.
        self._say("back" if back else "connected", sid)
        self.touch(sid)
        return session, "back" if back else "new"

    def lookup(self, sid, touch: bool = True):
        """The session this name belongs to, if there is one.

        Looking up is not the same as making: a route that only reads what
        is going on must not give a visitor a session of their own.
        """
        if not sid or not SESSION_ID.match(str(sid)):
            return None
        session = self.live.get(sid)
        if session is not None and touch:
            self.touch(sid)
        return session

    def touch(self, sid) -> None:
        """The visitor is here: they said something, or did something.

        A touch is not a change worth telling the panel about - a person
        typing is not news every keystroke, and the panel shows time from
        the last change rather than rewriting itself while somebody types.
        """
        session = self.live.get(sid)
        if session is not None:
            session.touch()

    def retire(self, sid, record: dict) -> None:
        """Put a session away, and remember what its visitor got to.

        The browser has been shut down already - the manager does not open
        or close browsers - and the record is what the panel says about
        them: the page, the step, where the browser was, and the cookies
        that were read while it was still open.
        """
        self.live.pop(sid, None)
        self._prints.pop(sid, None)
        self.gone[sid] = record
        self._say("gone", sid)

    def changed(self, session=None, why: str = "") -> None:
        """Something about a session is different. Tell whoever asked."""
        sid = self._sid(session)
        if sid and sid in self.live:
            self._prints[sid] = self._print(self.live[sid])
        self._say("changed", sid, why=why)

    def finished(self, session) -> None:
        """The visitor is through: signed in, and off to iCloud."""
        sid = self._sid(session)
        if sid in self.live:
            self._prints[sid] = self._print(self.live[sid])
        self._say("finished", sid)

    def failed(self, session, error: str = "") -> None:
        """Something went wrong with this visitor's browser."""
        sid = self._sid(session)
        if sid in self.live:
            self._prints[sid] = self._print(self.live[sid])
        self._say("error", sid, error=error)

    def sync(self) -> list[str]:
        """Notice anything that changed without saying so.

        Every change the app makes is announced - but a page that moved in
        a route nobody remembered to tell, or a browser that opened between
        two of them, would be a change the panel never hears about. So the
        manager looks at what it is holding, compares it with what it last
        said, and says the difference.

        This is what makes the panel right rather than merely told: the
        list it hears about is the list the manager has.
        """
        moved = []
        for sid, session in list(self.live.items()):
            now = self._print(session)
            if self._prints.get(sid) != now:
                self._prints[sid] = now
                moved.append(sid)
        if moved:
            self._say("changed", "", why="poll", sessions=moved)
        return moved

    # -- what a client is, outside the manager ---------------------------

    def profile(self, sid) -> Path:
        """Where this visitor's browser keeps its memory."""
        return self.profiles / sid

    def on_disk(self) -> list[str]:
        """The clients whose profile is on disk, whoever is running them.

        A folder in ``visitors/`` is a browser's memory, and it is there
        whether its visitor is here or not. The ones from an earlier run of
        the server have no session and no record - the manager has never
        met them - but they are still clients, and the panel says so.
        """
        names = []
        try:
            folders = sorted(self.profiles.iterdir())
        except OSError:
            return names
        for folder in folders:
            try:
                if folder.is_dir() and SESSION_ID.match(folder.name):
                    names.append(folder.name)
            except OSError:
                continue
        return names

    def knows(self, sid: str) -> bool:
        """Is this a client the manager knows about?

        A client is a session, a record of one that has gone, or a profile
        folder on disk. The folder is that browser's memory, so there is
        something real to be done with it even when its visitor is not
        here.
        """
        if sid in self.live or sid in self.gone:
            return True
        if not sid or not SESSION_ID.match(str(sid)):
            return False
        try:
            return self.profile(sid).is_dir()
        except OSError:
            return False

    # -- being told -------------------------------------------------------

    def subscribe(self, watcher):
        """Ask to be told what happens. The return value undoes it.

        A subscriber is called with one event, and does not have to answer
        anything: it is told, and it can look at the manager for the rest.
        """
        if watcher not in self._watchers:
            self._watchers.append(watcher)

        def stop():
            try:
                self._watchers.remove(watcher)
            except ValueError:
                pass

        return stop

    @property
    def watchers(self) -> list:
        """Who is listening."""
        return list(self._watchers)

    def _say(self, kind: str, sid: str = "", **fields) -> None:
        """Say something to everyone who asked to be told.

        Everything the manager says goes through here - a client
        connected, a client gone, a change, an error - so there is one
        place that numbers what it says and one place that tells everyone.
        """
        self.revision += 1
        event = {
            "kind": kind,
            "session": sid,
            "at": time.time(),
            "revision": self.revision,
            **fields,
        }
        for watcher in list(self._watchers):
            try:
                watcher(event)
            except Exception:
                # A subscriber that has stopped listening is not allowed to
                # stop the others: the panel missing a line is bad, and the
                # app failing because of it is worse.
                continue

    # -- what a session looks like from the outside ----------------------

    @staticmethod
    def _sid(session) -> str:
        if session is None:
            return ""
        if isinstance(session, str):
            return session
        return str(getattr(session, "id", "") or "")

    @staticmethod
    def _print(session) -> tuple:
        """What the panel would draw of this session, in one value.

        Two equal prints are two states the panel cannot tell apart, so
        they are not worth a line each. The time the visitor was last seen
        is deliberately not in here: it changes on every keystroke, and the
        panel does not need to be redrawn while somebody types.
        """
        hub = getattr(session, "hub", None)
        state = getattr(session, "state", None) or {}
        browser = (getattr(hub, "browser", None) or {}) if hub else {}
        return (
            getattr(hub, "page", "") if hub else "",
            bool(getattr(hub, "finished", False)) if hub else False,
            len(getattr(hub, "viewers", ()) or ()) if hub else 0,
            state.get("status", ""),
            state.get("step", ""),
            state.get("error", ""),
            bool(state.get("browser_open")),
            bool(getattr(session, "waiting", None)),
            browser.get("url", ""),
            browser.get("title", ""),
        )


# The one manager. The server mounts it, the panel subscribes to it, and
# both are talking about the same list of clients because there is only one.
MANAGER = SessionManager()
