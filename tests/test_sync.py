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
from html.parser import HTMLParser
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
    check("the capture's own scripts are untouched",
          "window.__ran" in served and "bundle.js" in served and
          "document.body.innerHTML" in served, served)
    check("the capture's policy is extended, not replaced",
          'content="default-src \'none\'; script-src \'self\'; '
          'connect-src \'self\' ws: wss:"' in served, served[:300])
    check("the policy still forbids what it forbade",
          "default-src 'none'" in served and "form-action" not in served, served[:300])
    check("no meta is added",
          'name="mobile-web-app-capable"' not in served, served)
    check("the captured markup survives untouched",
          '<p>hello</p>' in served and 'id="account"' in served)
    check("preparing a page twice changes nothing",
          server.with_engine(served) == served)

    # ---- the policy, on its own ------------------------------------------
    # The engine is a script of ours and it opens a socket to our own
    # origin. A capture's policy says no to both, because it was written for
    # a page that has no engine in it - so without this the page is a
    # picture of a page.
    check("a policy that pins scripts gains 'self'",
          server._relax_policy("default-src 'none'; script-src 'unsafe-inline' data:") ==
          "default-src 'none'; script-src 'unsafe-inline' data: 'self'; "
          "connect-src 'self' ws: wss:", None)
    check("a policy with no script-src gets one",
          server._relax_policy("default-src 'none'") ==
          "default-src 'none'; script-src 'self'; connect-src 'self' ws: wss:", None)
    check("a policy that says nothing about scripts is left alone",
          server._relax_policy("img-src 'self' data:") == "img-src 'self' data:", None)
    check("a socket is allowed where the policy would refuse one",
          "connect-src 'self' ws: wss:" in
          server._relax_policy("default-src 'none'; script-src 'unsafe-inline'"), None)
    check("relaxing a policy twice changes nothing",
          server._relax_policy(server._relax_policy("default-src 'none'")) ==
          server._relax_policy("default-src 'none'"), None)

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

    # ---- a SingleFile capture -------------------------------------------
    # SingleFile keeps a whole nested document inside an <iframe srcdoc="...">
    # attribute, and the outer page has no head of its own. The nested </head>
    # must not be mistaken for the page's: a tag inserted there ends the
    # attribute at its first quote, which cuts the frame down to a fragment
    # and drops the sign-in form the capture exists for.
    inner = (
        "<!DOCTYPE html> <html dir=ltr lang=en><meta charset=utf-8><title></title>"
        "<style>.a{color:red}</style>"
        '<meta http-equiv=content-security-policy content=&quot;default-src \'none\'&quot;>'
        "</head><body class=tk-body>"
        "<div class=signin-v2><input type=text id=account_name_text_field value=.>"
        "<button id=sign-in>Continue</button></div></body></html>"
    )
    single_file = (
        "<!DOCTYPE html> <html lang=en-us dir=ltr><!-- Page saved with SingleFile -->"
        "<meta charset=utf-8><meta name=viewport content=\"width=device-width\">"
        "<meta name=apple-mobile-web-app-capable content=yes><title>iCloud</title>"
        "<style>.sf-hidden{display:none!important}</style>"
        '<meta http-equiv=content-security-policy content="default-src \'none\'; '
        'script-src \'unsafe-inline\' data:">'
        "<body __processed_=true class=clicking><div id=root><header>logo</header>"
        f'<iframe src width=100% height=365 sandbox="allow-scripts allow-modals" srcdoc="{inner}"></iframe>'
        "</div></body></html>"
    )
    single_served = server.with_engine(single_file)

    check("a SingleFile capture keeps its markup",
          "<div id=root><header>logo</header>" in single_served, single_served[:200])
    check("the tag lands in the capture's own body, not in an attribute",
          single_served.index(server.SYNC_TAG) > single_served.index("<body")
          and single_served.index(server.SYNC_TAG) < single_served.index("<iframe"),
          single_served[:260])
    check("the nested document keeps its own </head>",
          single_served.count("</head>") == single_file.count("</head>")
          == 1, single_served[:260])
    check("the capture's own policy is extended",
          "script-src 'unsafe-inline' data: 'self'" in single_served, single_served[:300])

    class _Srcdoc(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.value = None

        def handle_starttag(self, tag, attrs):
            if tag == "iframe":
                self.value = dict(attrs).get("srcdoc")

    def srcdoc_of(text):
        grab = _Srcdoc()
        grab.feed(text)
        return grab.value

    check("the iframe's srcdoc attribute is untouched",
          srcdoc_of(single_served) == srcdoc_of(single_file),
          str(srcdoc_of(single_served))[:200])
    check("the nested document's own policy is untouched",
          "content=&quot;default-src 'none'&quot;" in single_served, single_served[:400])
    check("the sign-in form is still inside it",
          "account_name_text_field" in (srcdoc_of(single_served) or "")
          and "id=sign-in" in (srcdoc_of(single_served) or ""),
          str(srcdoc_of(single_served))[:200])
    check("a SingleFile capture is not tagged twice",
          server.with_engine(single_served) == single_served)

    # ---- the frames a capture carries ------------------------------------
    # The form in a SingleFile capture lives in a frame that is sandboxed
    # without allow-same-origin, so the page cannot reach it at all. The
    # engine is in the page, so without this the form is a picture of a form.
    check("a frame the capture carries is given the page's origin",
          'sandbox="allow-scripts allow-modals allow-same-origin"' in single_served,
          single_served[:400])

    plain_frame = (
        "<!DOCTYPE html><html><body>"
        '<iframe src="https://idmsa.apple.com/"></iframe></body></html>'
    )
    check("a frame of another site is left alone",
          "allow-same-origin" not in server.with_engine(plain_frame), plain_frame)

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
    check("the served page is the file, with the engine and the policy extended",
          body == source.replace("</head>", server.SYNC_TAG + "</head>", 1).replace(
              'content="default-src \'none\'"',
              'content="default-src \'none\'; script-src \'self\'; '
              'connect-src \'self\' ws: wss:"'),
          body[:300])
    check("nothing else was rewritten",
          "window.__ran" in body and
          "default-src 'none'" in body and
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

    # ---- the flow --------------------------------------------------------
    # Which capture leads to which is the user's, and it is said in a file
    # next to the captures: src/flow.json.
    flow_root = Path(tempfile.mkdtemp()).resolve()
    (flow_root / "flow.json").write_text(
        json.dumps({
            "steps": [
                {"page": "/src/1.html", "press": "sign-in", "go": "/src/4.html"},
                {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
                 "live": True},
            ]
        }),
        encoding="utf-8",
    )
    server.SRC = flow_root

    check("the flow is read from the folder of captures",
          len(server.flow_steps()) == 2, server.flow_steps())
    check("a step is found for a press on a capture",
          (server.flow_step("/src/1.html", "sign-in") or {}).get("go") == "/src/4.html",
          server.flow_step("/src/1.html", "sign-in"))
    check("a step is not found for a press it does not mention",
          server.flow_step("/src/1.html", "create-link") is None)
    check("a step is not found for another capture",
          server.flow_step("/src/3.html", "sign-in") is None)

    server.hub = server.Hub()
    server.hub.page = "/src/1.html"
    alice, alice_task = await open_viewer()
    bob, bob_task = await open_viewer()

    count = len(alice.sent)
    bob.feed({"t": "press", "id": "sign-in", "page": "/src/1.html"})
    await settle()
    check("a press the flow mentions moves the app to the next capture",
          server.hub.page == "/src/4.html", server.hub.page)
    check("...and the viewer that pressed it moves too",
          alice.sent[count:] == [{"t": "nav", "page": "/src/4.html"}],
          alice.sent[count:])
    check("...and the other viewer moves as well",
          bob.sent[-1] == {"t": "nav", "page": "/src/4.html"}, bob.sent[-1])

    # A press the flow says nothing about is still a press in the browser.
    count = len(alice.sent)
    bob.feed({"t": "press", "id": "create-link", "page": "/src/4.html"})
    await settle()
    check("a press the flow does not mention does not move the app",
          server.hub.page == "/src/4.html" and len(alice.sent) == count,
          (server.hub.page, alice.sent[count:]))

    # No flow file, no flow.
    (flow_root / "flow.json").unlink()
    check("with no flow file there are no steps", server.flow_steps() == [])
    check("...and a press is not found in it",
          server.flow_step("/src/1.html", "sign-in") is None)

    for viewer in (alice, bob):
        viewer.drop()
    await asyncio.gather(alice_task, bob_task, return_exceptions=True)

    for viewer in (alice, carol):
        viewer.drop()
    await asyncio.gather(alice_task, carol_task, bob_task, return_exceptions=True)


asyncio.run(main())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
