"""The parts of server.py that no browser is needed to check.

Run with: python tests/test_sync.py   (or `npm test`)

Two things are pinned here:

  * the captured pages are served as they are, with /sync.js injected into
    the HTML and nothing outside src/ readable, and
  * the hub really is the shared state - a value typed in one viewer reaches
    the others and is handed to a viewer that joins late, a click is relayed
    but not replayed, and following a link moves everyone to the same page.

The WebSockets are stubs, so this runs without uvicorn, a browser or a
network. The route functions themselves are the shipped ones.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from starlette.requests import Request  # noqa: E402

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


async def settle():
    """Let the viewers' pump tasks run."""
    for _ in range(5):
        await asyncio.sleep(0.01)


class FakeWebSocket:
    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_json(self, message):
        self.sent.append(message)

    def feed(self, message):
        self.incoming.put_nowait(json.dumps(message))

    def drop(self):
        self.incoming.put_nowait(server.WebSocketDisconnect(code=1000))


async def open_viewer():
    websocket = FakeWebSocket()
    task = asyncio.create_task(server.sync_socket(websocket))
    await settle()
    return websocket, task


def make_request(payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    done = False

    async def receive():
        nonlocal done
        if done:
            return {"type": "http.disconnect"}
        done = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/nav", "headers": []}, receive)


async def main():
    # ---- what a captured page gets on the way out ------------------------
    page = (
        "<!DOCTYPE html><html><head>"
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
        '<meta name="viewport" content="width=device-width">'
        "<title>Sign in</title>"
        "<script>window.__ran = true;</script>"
        '<script src="https://www.icloud.com/bundle.js"></script>'
        '<script type="application/json">{"step":"account"}</script>'
        "</head><body>"
        "<script>document.body.innerHTML = '';</script>"
        '<p>hello</p><form><input id="account"></form></body></html>'
    )
    served = server.with_engine(page)

    check("the sync engine is injected into the head",
          server.SYNC_TAG in served and
          served.index(server.SYNC_TAG) < served.index("</head>"), served[:300])
    check("the sync tag is the only difference",
          served.replace(server.SYNC_TAG, "") == page, served[:300])
    check("the capture's own scripts are untouched",
          "window.__ran" in served and "bundle.js" in served and
          "document.body.innerHTML" in served, served)
    check("the capture's policy is untouched",
          'content="default-src \'none\'"' in served, served)
    check("no meta is added",
          'name="mobile-web-app-capable"' not in served, served)
    check("the captured markup survives untouched",
          '<p>hello</p>' in served and 'id="account"' in served)
    check("preparing a page twice changes nothing",
          server.with_engine(served) == served)

    upper = server.with_engine("<HTML><HEAD></HEAD><BODY></BODY></HTML>")
    check("a capture with an upper-case head still gets it",
          server.SYNC_TAG in upper and
          upper.replace(server.SYNC_TAG, "") == "<HTML><HEAD></HEAD><BODY></BODY></HTML>",
          upper)

    headless = server.with_engine("<html><body>x</body></html>")
    check("a capture with no head still gets it",
          server.SYNC_TAG in headless and
          headless.replace(server.SYNC_TAG, "") == "<html><body>x</body></html>",
          headless)

    bare = server.with_engine("no html wrapper at all")
    check("a capture with no head or body still gets it",
          bare == server.SYNC_TAG + "no html wrapper at all", bare)

    # ---- only captured pages may be navigated to -------------------------
    check("a captured page is a valid destination",
          server.clean_page("/src/2.html") == "/src/2.html")
    check("an off-site page is refused", server.clean_page("https://example.com/") == "")
    check("a traversal is refused", server.clean_page("/src/../server.py") == "")
    check("a non-html file is refused", server.clean_page("/src/site.css") == "")
    check("a non-string is refused", server.clean_page(None) == "")

    check("a referrer from a captured page means a link was followed",
          server.came_from_a_capture("http://localhost:9002/src/1.html"))
    check("the app itself is not a link being followed",
          not server.came_from_a_capture("http://localhost:9002/"))
    check("an outside referrer is not a link being followed",
          not server.came_from_a_capture("https://example.com/docs/page.html"))
    check("an empty referrer is not a link being followed",
          not server.came_from_a_capture(""))

    # ---- serving src/ -----------------------------------------------------
    root = Path(tempfile.mkdtemp()).resolve()
    (root / "1.html").write_text(
        "<!DOCTYPE html><html><head><title>Sign in to iCloud</title>"
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
        "</head><body>"
        "<script>window.__ran = true;</script>"
        '<form><input id="account"></form></body></html>',
        encoding="utf-8",
    )
    (root / "2.html").write_text(
        "<!DOCTYPE html><html><body>two<script>window.__two = 1;</script></body></html>",
        encoding="utf-8",
    )
    (root / "site.css").write_text("body { color: red; }", encoding="utf-8")
    elsewhere = Path(tempfile.mkdtemp()).resolve()
    (elsewhere / "secret.html").write_text("TOP SECRET", encoding="utf-8")

    server.SRC = root

    response = await server.captured("1.html")
    body = response.body.decode()
    check("a captured page is served as HTML", response.status_code == 200 and
          response.media_type.startswith("text/html"), response.status_code)
    check("the served page has the engine and its own markup",
          "/sync.js" in body and "Sign in to iCloud" in body and 'id="account"' in body)
    check("the captured page is not cached", response.headers.get("cache-control") == "no-store")

    source = (root / "1.html").read_text(encoding="utf-8")
    check("the served page is the file, byte for byte, with the engine",
          body == source.replace("</head>", server.SYNC_TAG + "</head>", 1),
          body[:300])
    check("nothing else was rewritten",
          "window.__ran" in body and
          'content="default-src \'none\'"' in body and
          'name="mobile-web-app-capable"' not in body, body[:300])

    asset = await server.captured("site.css")
    check("an asset is served with its type",
          asset.status_code == 200 and asset.media_type == "text/css" and
          asset.body == b"body { color: red; }", asset.media_type)

    listing = await server.captured("")
    check("the folder lists the captures",
          listing.status_code == 200 and "1.html" in listing.body.decode() and
          "2.html" in listing.body.decode())

    missing = await server.captured("nope.html")
    check("a missing capture says where to put it",
          missing.status_code == 404 and "src/nope.html" in missing.body.decode() and
          "/sync.js" not in missing.body.decode(), missing.status_code)

    traversal = await server.captured("../" + elsewhere.name + "/secret.html")
    leaked = traversal.body.decode()
    check("nothing outside src/ can be read",
          traversal.status_code == 404 and "TOP SECRET" not in leaked, traversal.status_code)

    client = await server.index()
    check("/ serves the launcher while the browser is idle",
          str(client.path).endswith("client.html"), client.path)

    live = await server.status()
    check("status names the page the app should open",
          live.get("page") == server.hub.page, live)

    server.state["status"] = "done"
    redirect = await server.index()
    check("/ becomes the capture once Sign In has been clicked",
          redirect.status_code == 307 and
          redirect.headers.get("location") == server.hub.page,
          (redirect.status_code, redirect.headers.get("location")))
    server.state["status"] = "idle"

    engine = await server.sync_js()
    check("/sync.js serves the engine", str(engine.path).endswith("sync.js"), engine.path)

    # ---- the hub is the shared state -------------------------------------
    server.hub = server.Hub()
    alice, alice_task = await open_viewer()
    check("a new viewer is told what to show",
          alice.sent and alice.sent[0] == {"t": "state", "page": "/src/1.html", "values": {}},
          alice.sent)

    bob, bob_task = await open_viewer()

    bob.feed({"t": "value", "key": "#account", "value": "someone@example.com", "checked": False})
    await settle()
    check("a value reaches the other viewer",
          alice.sent[-1] == {"t": "value", "key": "#account",
                             "value": "someone@example.com", "checked": False},
          alice.sent[-1])
    check("the viewer that typed it is not echoed",
          all(m.get("t") != "value" for m in bob.sent), bob.sent)
    check("the server keeps the value",
          server.hub.values.get("#account") ==
          {"value": "someone@example.com", "checked": False}, server.hub.values)

    carol, carol_task = await open_viewer()
    joined = carol.sent[0] if carol.sent else {}
    check("a viewer that joins late is given what has been typed",
          joined.get("values", {}).get("#account", {}).get("value") == "someone@example.com",
          joined)

    # A checkbox is state, not a value.
    bob.feed({"t": "value", "key": "#remember", "value": "on", "checked": True})
    await settle()
    check("a checkbox state is kept",
          server.hub.values.get("#remember") == {"value": "on", "checked": True},
          server.hub.values.get("#remember"))

    # ---- clicks are relayed, not replayed --------------------------------
    count = len(alice.sent)
    bob.feed({"t": "click", "key": "#go", "x": 12.5, "y": 40})
    await settle()
    check("a click is relayed with its position",
          alice.sent[count:] == [{"t": "click", "key": "#go", "x": 12.5, "y": 40}],
          alice.sent[count:])
    count = len(alice.sent)
    bob.feed({"t": "click", "key": "#go", "x": "left", "y": 40})
    await settle()
    check("a click with no usable position is dropped",
          len(alice.sent) == count, alice.sent[count:])

    # ---- following a link moves everyone --------------------------------
    count = len(alice.sent)
    bob.feed({"t": "nav", "page": "/src/2.html"})
    await settle()
    check("a page change reaches the other viewers",
          alice.sent[count:] == [{"t": "nav", "page": "/src/2.html"}], alice.sent[count:])
    check("the server follows the page", server.hub.page == "/src/2.html", server.hub.page)
    check("form state belongs to the page it was typed on",
          server.hub.values == {}, server.hub.values)

    count = len(alice.sent)
    bob.feed({"t": "nav", "page": "https://example.com/"})
    bob.feed({"t": "nav", "page": "/src/../server.py"})
    await settle()
    check("an off-site or unsafe page is not followed",
          server.hub.page == "/src/2.html" and len(alice.sent) == count,
          (server.hub.page, alice.sent[count:]))

    # ---- the sendBeacon path --------------------------------------------
    response = await server.nav(make_request({"page": "/src/1.html"}))
    await settle()
    check("a beacon drives the same navigation",
          response.status_code == 204 and server.hub.page == "/src/1.html",
          (response.status_code, server.hub.page))

    bad = await server.nav(make_request(b"not json"))
    check("a malformed beacon is refused", bad.status_code == 400, bad.status_code)

    # A hello that is really a navigation (the beacon did not make it).
    count = len(alice.sent)
    bob.feed({"t": "hello", "page": "/src/3.html",
              "from": "http://localhost:9002/src/1.html"})
    await settle()
    check("a hello after following a link moves the hub",
          server.hub.page == "/src/3.html", server.hub.page)
    check("...and the other viewers follow",
          alice.sent[count:] == [{"t": "nav", "page": "/src/3.html"}], alice.sent[count:])
    check("the viewer that arrived is told where it is",
          bob.sent[-1] == {"t": "state", "page": "/src/3.html", "values": {}}, bob.sent[-1])

    # ---- leaving ---------------------------------------------------------
    bob.drop()
    await settle()
    check("a viewer that leaves is forgotten",
          bob not in server.hub.viewers and len(server.hub.viewers) == 2,
          len(server.hub.viewers))
    check("the socket loop finishes when the viewer goes",
          bob_task.done(), bob_task.done())

    alice.feed({"t": "value", "key": "#account", "value": "still here", "checked": False})
    await settle()
    check("the remaining viewers still talk",
          carol.sent[-1] == {"t": "value", "key": "#account",
                             "value": "still here", "checked": False}, carol.sent[-1])

    for viewer in (alice, carol):
        viewer.drop()
    await asyncio.gather(alice_task, carol_task, bob_task, return_exceptions=True)


asyncio.run(main())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
