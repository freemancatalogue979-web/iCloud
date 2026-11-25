"""The panel's two new rooms: the proxy, and the standalone copy.

Both are called the same way the panel calls them, so that what is tested
here is the panel's own rules:

  * the proxy can be changed and turned off while the server is running,
    and the change is live - the pool the browsers use is changed there
    and then, not at the next restart;
  * a proxy that cannot be used is not switched on, and the panel is told
    exactly which piece of it is missing, because "it is off" on its own
    leaves the operator guessing;
  * a test tries what is in the page - not what is saved, and not a proxy
    that has already been turned on - and a test changes nothing;
  * the password is never sent back to the page, so an empty field means
    "keep the one that is there"; and
  * the standalone copy is a zip, and it has the server's address in it.

Run with: python tests/test_proxy_admin.py
"""

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Request  # noqa: E402

import admin  # noqa: E402
import proxy_pool  # noqa: E402

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
    """The function behind one of the panel's routes."""
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


def make_request(method="GET", path="/admin/api/proxy", body=None,
                 cookies=None):
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
    if isinstance(answer, dict):
        return answer
    try:
        return json.loads(bytes(answer.body).decode("utf-8"))
    except Exception:
        return {}


async def main():
    scratch = Path(tempfile.mkdtemp(prefix="icloud-panel-"))
    cookie = {"icloud_admin": "the-secret"}

    def fresh(name):
        """A panel with nothing saved in it."""
        admin.STORE = admin.Store(scratch / name)
        admin.STORE.set_token("the-secret")
        return admin.STORE

    # The pool starts with whatever the environment said. The panel is the
    # switch from then on.
    proxy_pool.POOL.configure({})
    proxy_pool.POOL.enabled = False

    # ---- a proxy with nothing in it --------------------------------------
    fresh("empty.json")

    answer = await endpoint("/admin/api/proxy")(make_request())
    check("the proxy is for the admin only",
          getattr(answer, "status_code", 200) == 403, answer)

    answer = await endpoint("/admin/api/proxy")(make_request(cookies=cookie))
    said = as_json(answer)
    check("the panel is told whether the proxy is on",
          said.get("enabled") is False, said)
    check("...and what kind it is", "kind" in said and "server" in said, said)
    check("...and how long a session is held",
          said.get("session_minutes") == 120, said)
    check("...and how many accounts to an address",
          said.get("max_per_group") == 3, said)
    check("the password is never sent to the page",
          "password" not in said, said)
    check("...but the panel is told there is one",
          said.get("password_set") is False, said)

    # Turning the switch on with nothing to turn it on to is not an error
    # - it is a proxy that cannot be used, and the panel says which piece
    # of it is missing.
    answer = await endpoint("/admin/api/proxy", "POST")(
        make_request("POST", "/admin/api/proxy", {
            "enabled": True, "kind": "decodo",
        }, cookies=cookie))
    said = as_json(answer)
    check("a proxy that cannot be used is not switched on",
          said.get("enabled") is False, said)
    check("...and the panel is told what it is missing",
          "the server and port" in (said.get("missing") or []),
          said.get("missing"))
    check("...and the user name too",
          "the user name" in (said.get("missing") or []), said.get("missing"))
    check("...and the password",
          "the password" in (said.get("missing") or []), said.get("missing"))

    # ---- and a test says the same, before anything is saved --------------
    answer = await endpoint("/admin/api/proxy/test", "POST")(
        make_request("POST", "/admin/api/proxy/test", {"kind": "decodo"},
                     cookies=cookie))
    said = as_json(answer)
    check("a test with nothing in it says what is missing",
          not said.get("ok") and said.get("missing"), said)
    check("...in words the operator can read",
          "the server and port" in (said.get("reason") or ""),
          said.get("reason"))
    check("a test does not save anything",
          admin.STORE.proxy().get("server") == "", admin.STORE.proxy())

    # ---- and now the panel that has the proxy in it -----------------------
    fresh("panel.json")

    answer = await endpoint("/admin/api/proxy", "POST")(
        make_request("POST", "/admin/api/proxy", {
            "enabled": True,
            "kind": "decodo",
            "server": "gate.decodo.com:7000",
            "scheme": "socks5",
            "username": "user-alice-session-1-country-us",
            "password": "the-secret-password",
            "country": "US",
            "session_minutes": 180,
            "max_per_group": 2,
        }, cookies=cookie))
    said = as_json(answer)
    check("the proxy is on", said.get("enabled") is True, said)
    check("...and nothing is missing", said.get("missing") == [], said.get("missing"))
    check("...and the change is live, not at the next restart",
          proxy_pool.POOL.enabled is True)
    check("...the pool has the new address",
          proxy_pool.POOL.server == "gate.decodo.com:7000",
          proxy_pool.POOL.server)
    check("...the user name, without the parameters that were in it",
          proxy_pool.POOL.user == "user-alice", proxy_pool.POOL.user)
    check("...and the password it was given",
          proxy_pool.POOL.password == "the-secret-password")
    check("...and how long a session is held",
          proxy_pool.POOL.session_minutes == 180,
          proxy_pool.POOL.session_minutes)
    check("...and how many accounts to an address",
          proxy_pool.POOL.max_per_group == 2, proxy_pool.POOL.max_per_group)

    # ---- the name it builds -----------------------------------------------
    settings = proxy_pool.POOL.settings("us-0")
    check("the name it builds is the provider's own shape",
          settings["username"] == "user-alice-session-us-0"
                                  "-sessionduration-180-country-us",
          settings["username"])
    check("...through the SOCKS5 entrance",
          settings["server"] == "socks5://gate.decodo.com:7000",
          settings["server"])

    # ---- an empty password keeps the one that is there --------------------
    answer = await endpoint("/admin/api/proxy", "POST")(
        make_request("POST", "/admin/api/proxy", {
            "enabled": True, "password": "",
        }, cookies=cookie))
    check("an empty password keeps the one that is there",
          proxy_pool.POOL.password == "the-secret-password",
          proxy_pool.POOL.password)
    check("...and the panel is told there is one",
          as_json(answer).get("password_set") is True, as_json(answer))

    # ---- turning it off ----------------------------------------------------
    answer = await endpoint("/admin/api/proxy", "POST")(
        make_request("POST", "/admin/api/proxy", {"enabled": False},
                     cookies=cookie))
    check("the proxy can be turned off",
          as_json(answer).get("enabled") is False, as_json(answer))
    check("...and the browsers stop using it at once",
          proxy_pool.POOL.enabled is False)
    check("...so there is no address to give them",
          proxy_pool.POOL.settings("us-0") is None)

    # ---- and it is kept ----------------------------------------------------
    check("it is kept in the file the panel keeps",
          admin.STORE.proxy().get("username") == "user-alice",
          admin.STORE.proxy())
    check("...with the password, which is a secret",
          admin.STORE.proxy().get("password") == "the-secret-password")
    check("...and the file is a secret's file",
          oct((scratch / "panel.json").stat().st_mode)[-3:] == "600",
          oct((scratch / "panel.json").stat().st_mode))

    # ---- a test does not need the proxy to be on ---------------------------
    # The switch is off, and a test still goes out: a proxy is meant to be
    # tried before it is turned on.
    proxy_pool.POOL.enabled = False
    answer = await endpoint("/admin/api/proxy/test", "POST")(
        make_request("POST", "/admin/api/proxy/test", {
            "kind": "decodo", "server": "gate.decodo.com:7000",
            "username": "alice", "password": "the-secret-password",
            "country": "US",
        }, cookies=cookie))
    said = as_json(answer)
    check("a test answers while the proxy is still off",
          "ok" in said and not said.get("missing"), said)
    check("...and nothing was changed by it",
          admin.STORE.proxy().get("enabled") is False, admin.STORE.proxy())
    proxy_pool.POOL.enabled = True

    # ---- and a test does not need curl -------------------------------------
    # The check is made with a browser, because a browser is the only thing
    # on the machine that can speak to a SOCKS5 entrance on its own. A
    # machine with no curl must still get an answer.
    real_which = shutil.which
    shutil.which = lambda name: None if name == "curl" else real_which(name)
    try:
        answer = await endpoint("/admin/api/proxy/test", "POST")(
            make_request("POST", "/admin/api/proxy/test", {
                "kind": "decodo", "server": "gate.decodo.com:7000",
                "username": "alice", "password": "the-secret-password",
            }, cookies=cookie))
        said = as_json(answer)
        check("a machine with no curl is still answered",
              "ok" in said, said)
        check("...and it is not told there is no way to ask",
              "no way to ask" not in (said.get("said") or "")
              and "no way to ask" not in (said.get("reason") or ""),
              said)
    finally:
        shutil.which = real_which

    # ---- and the check really does go out with a browser -------------------
    # A small server of our own answers the way an address-echo service
    # does, so that the check itself is proved - and proved without a proxy
    # and without a network.
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ip": "203.0.113.9", "country_code": "US"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    said = await proxy_pool.check_in_browser({}, f"http://127.0.0.1:{port}/json")
    check("the check goes out with a browser and reads the answer",
          said.get("ok") and said.get("ip") == "203.0.113.9", said)
    check("...and the country with it", said.get("country") == "US", said)

    # ---- the standalone copy ----------------------------------------------
    answer = await endpoint("/admin/api/standalone")(make_request(cookies=cookie))
    said = as_json(answer)
    check("the panel is told where the server is",
          "server" in said, said)
    check("...and what the copy would hold",
          "1.html" in said.get("pages", []), said)

    answer = await endpoint("/admin/api/standalone", "POST")(
        make_request("POST", "/admin/api/standalone",
                     {"server": "https://icloud.example.com"},
                     cookies=cookie))
    check("the address is saved",
          admin.STORE.standalone()["server"] == "https://icloud.example.com",
          admin.STORE.standalone())

    answer = await endpoint("/admin/api/standalone/zip")(
        make_request("GET", "/admin/api/standalone/zip", cookies=cookie))
    body = getattr(answer, "body", b"")
    check("the copy is a zip",
          body[:2] == b"PK", body[:20])
    if body[:2] == b"PK":
        zip_file = zipfile.ZipFile(io.BytesIO(bytes(body)))
        names = zip_file.namelist()
        check("...with the launcher in it", "client.html" in names, names)
        check("...with the captures in it", "src/1.html" in names, names)
        check("...with the engine beside them", "src/sync.js" in names, names)
        check("...and the address in the pages",
              b'content="https://icloud.example.com"'
              in zip_file.read("src/1.html"))

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
