"""The panel's two new rooms: the proxy, and the standalone copy.

Both are called the same way the panel calls them, so that what is tested
here is the panel's own rules:

  * the proxy can be changed and turned off while the server is running,
    and the change is live - the pool the browsers use is changed there
    and then, not at the next restart;
  * the password is never sent back to the page, so an empty field means
    "keep the one that is there"; and
  * the standalone copy is a zip, and it has the server's address in it.

Run with: python tests/test_proxy_admin.py
"""

import asyncio
import io
import json
import os
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
    admin.STORE = admin.Store(scratch / "panel.json")
    admin.STORE.set_token("the-secret")
    cookie = {"icloud_admin": "the-secret"}

    # The pool starts with whatever the environment said. The panel is the
    # switch from then on.
    proxy_pool.POOL.configure({})
    proxy_pool.POOL.enabled = False

    # -- without the secret, nothing is said ---------------------------
    answer = await endpoint("/admin/api/proxy")(make_request())
    check("the proxy is for the admin only",
          getattr(answer, "status_code", 200) == 403, answer)

    # -- and what it is now --------------------------------------------
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

    # -- turning it on, with a password --------------------------------
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

    # -- and the name it builds with it ---------------------------------
    settings = proxy_pool.POOL.settings("us-0")
    check("the name it builds is the provider's own shape",
          settings["username"] == "user-alice-session-us-0"
                                  "-sessionduration-180-country-us",
          settings["username"])
    check("...through the SOCKS5 entrance",
          settings["server"] == "socks5://gate.decodo.com:7000",
          settings["server"])

    # -- an empty password keeps the one that is there ------------------
    answer = await endpoint("/admin/api/proxy", "POST")(
        make_request("POST", "/admin/api/proxy", {
            "enabled": True, "password": "",
        }, cookies=cookie))
    check("an empty password keeps the one that is there",
          proxy_pool.POOL.password == "the-secret-password",
          proxy_pool.POOL.password)
    check("...and the panel is told there is one",
          as_json(answer).get("password_set") is True, as_json(answer))

    # -- turning it off -------------------------------------------------
    answer = await endpoint("/admin/api/proxy", "POST")(
        make_request("POST", "/admin/api/proxy", {"enabled": False},
                     cookies=cookie))
    check("the proxy can be turned off",
          as_json(answer).get("enabled") is False, as_json(answer))
    check("...and the browsers stop using it at once",
          proxy_pool.POOL.enabled is False)
    check("...so there is no address to give them",
          proxy_pool.POOL.settings("us-0") is None)

    # -- and it is kept -------------------------------------------------
    check("it is kept in the file the panel keeps",
          admin.STORE.proxy().get("username") == "user-alice",
          admin.STORE.proxy())
    check("...with the password, which is a secret",
          admin.STORE.proxy().get("password") == "the-secret-password")
    check("...and the file is a secret's file",
          oct((scratch / "panel.json").stat().st_mode)[-3:] == "600",
          oct((scratch / "panel.json").stat().st_mode))

    # -- the standalone copy --------------------------------------------
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
