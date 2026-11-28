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
import os
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The panel of this machine is not this test's business. A question asked
# at the door puts the captures behind it, and a capture asked for here
# would be sent to the launcher instead of being served. So the panel is
# given a store of its own, with no question in it.
scratch = tempfile.mkdtemp(prefix="icloud-sync-")
os.environ["ICLOUD_ADMIN_STORE"] = os.path.join(scratch, "admin.json")

import admin  # noqa: E402
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
    def __init__(self, host="127.0.0.1:8000", session="the-tests"):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.accepted = False
        self.headers = {"host": host}
        # The visitor this socket belongs to: the cookie says whose
        # browser this is.
        self.cookies = {server.SESSION_COOKIE: session}

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


async def open_viewer(session="the-tests"):
    websocket = FakeWebSocket(session=session)
    task = asyncio.create_task(server.sync_socket(websocket))
    await settle()
    return websocket, task


def make_session_request(session="the-tests", named=False):
    """A request that belongs to a visitor.

    ``named`` puts the names in the address as well: a capture is for one
    browser and one session, and says so in the address it is asked for by.
    """
    query = f"sid={session}".encode() if named else b""
    return Request({"type": "http", "method": "GET", "path": "/",
                    "query_string": query,
                    "headers": [(b"cookie", f"{server.SESSION_COOKIE}={session}".encode())]},
                   None)


def make_request(payload, session="the-tests"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    done = False

    async def receive():
        nonlocal done
        if done:
            return {"type": "http.disconnect"}
        done = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = []
    if session:
        headers.append(
            (b"cookie", f"{server.SESSION_COOKIE}={session}".encode()))
    return Request({"type": "http", "method": "POST", "path": "/nav",
                    "headers": headers},
                   receive)


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
          'content="default-src \'none\'; script-src \'self\' '
          'https://www.google.com/recaptcha/ '
          'https://www.gstatic.com/recaptcha/ '
          'https://challenges.cloudflare.com/; '
          'connect-src \'self\' ws: wss: '
          'https://www.google.com/recaptcha/ '
          'https://challenges.cloudflare.com/; '
          'frame-src https://www.google.com/recaptcha/ '
          'https://challenges.cloudflare.com/; '
          'img-src https://www.gstatic.com/recaptcha/"' in served, served[:300])
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
    #
    # And the question at the door, which the capture asks itself, needs the
    # widget's own hosts: the script that asks, the frame it is drawn in,
    # the place the answer is checked at, and the badge's picture.
    check("a policy that pins scripts gains 'self'",
          server._relax_policy("default-src 'none'; script-src 'unsafe-inline' data:") ==
          "default-src 'none'; script-src 'unsafe-inline' data: 'self' "
          "https://www.google.com/recaptcha/ "
          "https://www.gstatic.com/recaptcha/ "
          "https://challenges.cloudflare.com/; "
          "connect-src 'self' ws: wss: "
          "https://www.google.com/recaptcha/ "
          "https://challenges.cloudflare.com/; "
          "frame-src https://www.google.com/recaptcha/ "
          "https://challenges.cloudflare.com/; "
          "img-src https://www.gstatic.com/recaptcha/", None)
    check("a policy with no script-src gets one",
          server._relax_policy("default-src 'none'") ==
          "default-src 'none'; script-src 'self' "
          "https://www.google.com/recaptcha/ "
          "https://www.gstatic.com/recaptcha/ "
          "https://challenges.cloudflare.com/; "
          "connect-src 'self' ws: wss: "
          "https://www.google.com/recaptcha/ "
          "https://challenges.cloudflare.com/; "
          "frame-src https://www.google.com/recaptcha/ "
          "https://challenges.cloudflare.com/; "
          "img-src https://www.gstatic.com/recaptcha/", None)
    check("a policy that says nothing about scripts is left alone",
          "script-src" not in server._relax_policy("img-src 'self' data:"), None)
    check("a socket is allowed where the policy would refuse one",
          "connect-src 'self' ws: wss:" in
          server._relax_policy("default-src 'none'; script-src 'unsafe-inline'"), None)
    check("the widget is allowed where the policy would refuse it",
          "https://www.google.com/recaptcha/" in
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

    response = await server.captured(make_session_request(named=True), "1.html")
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
              'content="default-src \'none\'; script-src \'self\' '
              'https://www.google.com/recaptcha/ '
              'https://www.gstatic.com/recaptcha/ '
              'https://challenges.cloudflare.com/; '
              'connect-src \'self\' ws: wss: '
              'https://www.google.com/recaptcha/ '
              'https://challenges.cloudflare.com/; '
              'frame-src https://www.google.com/recaptcha/ '
              'https://challenges.cloudflare.com/; '
              'img-src https://www.gstatic.com/recaptcha/"'),
          body[:300])
    check("nothing else was rewritten",
          "window.__ran" in body and
          "default-src 'none'" in body and
          'name="mobile-web-app-capable"' not in body, body[:300])

    asset = await server.captured(make_session_request(named=True), "site.css")
    check("an asset is served with its type",
          asset.status_code == 200 and asset.media_type == "text/css" and
          asset.body == b"body { color: red; }", asset.media_type)

    listing = await server.captured(make_session_request(named=True), "")
    check("the folder lists the captures",
          listing.status_code == 200 and "1.html" in listing.body.decode() and
          "2.html" in listing.body.decode())

    missing = await server.captured(make_session_request(named=True), "nope.html")
    check("a missing capture says where to put it",
          missing.status_code == 404 and "src/nope.html" in missing.body.decode() and
          "/sync.js" not in missing.body.decode(), missing.status_code)

    traversal = await server.captured(make_session_request(named=True), "../" + elsewhere.name + "/secret.html")
    leaked = traversal.body.decode()
    check("nothing outside src/ can be read",
          traversal.status_code == 404 and "TOP SECRET" not in leaked, traversal.status_code)

    request = make_session_request(named=True)
    client = await server.index(request)
    check("/ serves the launcher while the browser is idle",
          str(client.path).endswith("client.html"), client.path)

    session = server.session_for(request.cookies.get(server.SESSION_COOKIE))
    live = await server.status(request)
    check("status names the page the app should open",
          live.get("page") == session.hub.page, live)

    session.state["status"] = "done"
    redirect = await server.index(request)
    # The capture it sends the person to carries the names as well, or the
    # page it lands on does not know who it is for.
    where = redirect.headers.get("location") or ""
    check("/ becomes the capture once Sign In has been clicked",
          redirect.status_code == 307 and
          where.split("?")[0] == session.hub.page,
          (redirect.status_code, where))
    check("...and the names went with it",
          "sid=the-tests" in where, where)
    session.state["status"] = "idle"

    # ---- every visitor gets their own browser ---------------------------
    one = server.session_for("visitor-one")
    two = server.session_for("visitor-two")
    check("two visitors have two sessions",
          one is not two and one.id != two.id)
    check("two visitors have two browsers",
          one.bridge is not two.bridge and one.hub is not two.hub)
    check("two visitors do not share a page",
          one.hub is not two.hub)
    one.hub.navigate("/src/4.html", everyone=True)
    check("...and one cannot move the other",
          two.hub.page != "/src/4.html", two.hub.page)
    check("a visitor's browser keeps its own profile",
          one.profile() != two.profile() and
          one.profile().name == one.id, one.profile())

    engine = await server.sync_js()
    check("/sync.js serves the engine", str(engine.path).endswith("sync.js"), engine.path)

    # ---- the font the captures are read in -------------------------------
    # It is served from here and not fetched from a font house, so a machine
    # with no network still shows the page in the font it was meant to be
    # read in.
    font = await server.font_file("montserrat-latin.woff2")
    check("/fonts/ serves the font", font.status_code == 200, font.status_code)
    check("...as a font, and not as a page",
          font.media_type == "font/woff2", font.media_type)
    check("...with the bytes of a woff2 in it",
          font.body[:4] == b"wOF2" and len(font.body) > 10000, len(font.body))
    check("...and it can be kept",
          "max-age" in font.headers.get("cache-control", ""),
          font.headers.get("cache-control"))

    nowhere = await server.font_file("no-such-font.woff2")
    check("a font that is not there is not a file to read",
          nowhere.status_code == 404, nowhere.status_code)

    outside = await server.font_file("../server.py")
    check("...and fonts/ is not a way into the rest of the app",
          outside.status_code == 404, outside.status_code)

    # ---- the hub is the shared state -------------------------------------
    session = server.session_for('the-tests')
    session.hub = server.Hub()
    session.hub.session = session
    # And the question has been answered: a session that has not answered
    # it is given no content at all, so the hub keeps its mouth shut until
    # the answer has been good.
    session.passed = True
    alice, alice_task = await open_viewer()
    check("a new viewer is told what to show",
          alice.sent and alice.sent[0] == {"t": "state", "page": "/src/1.html", "values": {}},
          alice.sent)

    bob, bob_task = await open_viewer()

    # ---- and what this session is doing is said, not asked for ------------
    # The launcher waits on a black screen for the browser to be ready. It
    # used to ask the server every quarter of a second; now it is told,
    # over the line the captures already use.
    told = [m for m in alice.sent if m.get("t") == "session"]
    check("a viewer that arrives is told what the session is doing",
          told and told[0].get("status") == "idle", alice.sent)
    check("...and which page is on screen",
          told and told[0].get("page") == "/src/1.html", told)

    count = len(alice.sent)
    session.state["status"] = "done"
    session.hub.tell_state()
    await settle()
    check("the viewers are told when the browser is ready",
          alice.sent[count:] == [{"t": "session", "status": "done",
                                  "error": "", "browser_open": False,
                                  "passed": True,
                                  "page": "/src/1.html"}],
          alice.sent[count:])

    count = len(alice.sent)
    session.state["status"] = "error"
    session.state["error"] = "the browser would not open"
    session.hub.tell_state()
    await settle()
    check("...and when something went wrong",
          alice.sent[count:] == [{"t": "session", "status": "error",
                                  "error": "the browser would not open",
                                  "browser_open": False,
                                  "passed": True,
                                  "page": "/src/1.html"}],
          alice.sent[count:])
    session.state["status"] = "idle"
    session.state["error"] = ""

    bob.feed({"t": "value", "key": "#account", "value": "someone@example.com", "checked": False})
    await settle()
    check("a value reaches the other viewer",
          alice.sent[-1] == {"t": "value", "key": "#account",
                             "value": "someone@example.com", "checked": False},
          alice.sent[-1])
    check("the viewer that typed it is not echoed",
          all(m.get("t") != "value" for m in bob.sent), bob.sent)
    check("the server keeps the value",
          session.hub.values.get("#account") ==
          {"value": "someone@example.com", "checked": False}, session.hub.values)

    carol, carol_task = await open_viewer()
    joined = carol.sent[0] if carol.sent else {}
    check("a viewer that joins late is given what has been typed",
          joined.get("values", {}).get("#account", {}).get("value") == "someone@example.com",
          joined)

    # A checkbox is state, not a value.
    bob.feed({"t": "value", "key": "#remember", "value": "on", "checked": True})
    await settle()
    check("a checkbox state is kept",
          session.hub.values.get("#remember") == {"value": "on", "checked": True},
          session.hub.values.get("#remember"))

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
    check("the server follows the page", session.hub.page == "/src/2.html", session.hub.page)
    check("form state belongs to the page it was typed on",
          session.hub.values == {}, session.hub.values)

    count = len(alice.sent)
    bob.feed({"t": "nav", "page": "https://example.com/"})
    bob.feed({"t": "nav", "page": "/src/../server.py"})
    await settle()
    check("an off-site or unsafe page is not followed",
          session.hub.page == "/src/2.html" and len(alice.sent) == count,
          (session.hub.page, alice.sent[count:]))

    # ---- the sendBeacon path --------------------------------------------
    response = await server.nav(make_request({"page": "/src/1.html"}))
    await settle()
    check("a beacon drives the same navigation",
          response.status_code == 204 and session.hub.page == "/src/1.html",
          (response.status_code, session.hub.page))

    bad = await server.nav(make_request(b"not json"))
    check("a malformed beacon is refused", bad.status_code == 400, bad.status_code)

    # A hello that is really a navigation (the beacon did not make it).
    count = len(alice.sent)
    bob.feed({"t": "hello", "page": "/src/3.html",
              "from": "http://localhost:9002/src/1.html"})
    await settle()
    check("a hello after following a link moves the hub",
          session.hub.page == "/src/3.html", session.hub.page)
    check("...and the other viewers follow",
          alice.sent[count:] == [{"t": "nav", "page": "/src/3.html"}], alice.sent[count:])
    check("the viewer that arrived is told where it is",
          bob.sent[-1] == {"t": "state", "page": "/src/3.html", "values": {}}, bob.sent[-1])

    # ---- the launcher says where it is -----------------------------------
    # A copy that is serving the launcher instead of the capture looks, from
    # the visitor's side, like a page that simply reloads. So the launcher
    # says where it is, and the server says it out loud.
    bob.feed({"t": "splash", "page": "/src/1.html", "going": "/src/1.html",
              "why": "the copy is serving the launcher where the capture "
                     "should be"})
    await settle()
    check("the launcher says where it is going",
          session.launcher_going == "/src/1.html", session.launcher_going)
    check("...and a launcher sitting on a capture's page is said out loud",
          "the copy is serving the launcher there"
          in server.state.get("step", ""), server.state.get("step"))
    check("...and the session keeps where the launcher is, for the panel",
          session.launcher_page == "/src/1.html"
          and "serving the launcher" in session.launcher_why,
          (session.launcher_page, session.launcher_why))
    check("...and the panel is told what is wrong with the copy",
          "serving the launcher" in admin.splash_note(session),
          admin.splash_note(session))

    bob.feed({"t": "splash", "page": "/client.html", "going": "/src/1.html"})
    await settle()
    check("...and going to the same place twice is said out loud",
          "is answering with the launcher again"
          in server.state.get("step", ""), server.state.get("step"))

    # A capture on screen means the launcher is not where it was: whatever
    # it said about the copy is over.
    bob.feed({"t": "hello", "page": "/src/1.html",
              "from": "http://localhost:9002/src/1.html"})
    await settle()
    check("...and a capture on screen clears it",
          session.launcher_page == "" and session.launcher_why == "",
          (session.launcher_page, session.launcher_why))

    # ---- the client's own state ------------------------------------------
    # How far this person has got is kept in their own browser, and the
    # server is told about it when the engine says hello. Somebody who has
    # already got in is sent to iCloud; somebody whose browser has no state,
    # or whose state does not say they finished, starts the process as
    # usual.
    session.hub.page = "/src/1.html"
    session.hub.finished = False
    count = len(alice.sent)

    bob.feed({"t": "hello", "page": "/src/1.html",
              "state": {"stage": "Address", "finished": False}})
    await settle()
    check("a hello with a state that is not finished starts the process as usual",
          session.hub.page == "/src/1.html" and not session.hub.finished,
          (session.hub.page, session.hub.finished))
    check("...and the viewer is told where it is",
          bob.sent[-1]["t"] == "state", bob.sent[-1])

    bob.feed({"t": "hello", "page": "/src/1.html"})
    await settle()
    check("a hello with no state at all starts the process as usual",
          session.hub.page == "/src/1.html" and not session.hub.finished,
          (session.hub.page, session.hub.finished))

    count = len(alice.sent)
    bob.feed({"t": "hello", "page": "/src/1.html",
              "state": {"stage": "Signed in", "finished": True}})
    await settle()
    check("a hello that says they have finished sends them to iCloud",
          alice.sent[-1] == {"t": "finished", "page": server.ICLOUD},
          alice.sent[-1:])
    check("...and the session knows they have finished",
          session.hub.finished is True, session.hub.finished)
    check("...and the captures are closed to them",
          session.hub.page == server.ICLOUD, session.hub.page)

    # Back to the beginning, for the tests that follow.
    session.hub.finished = False
    session.hub.page = "/src/1.html"

    # ---- leaving ---------------------------------------------------------
    bob.drop()
    await settle()
    check("a viewer that leaves is forgotten",
          bob not in session.hub.viewers and len(session.hub.viewers) == 2,
          len(session.hub.viewers))
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

    session = server.session_for('the-tests')
    session.hub = server.Hub()
    session.hub.session = session
    session.hub.page = "/src/1.html"
    alice, alice_task = await open_viewer()
    bob, bob_task = await open_viewer()

    count = len(alice.sent)
    bob.feed({"t": "press", "id": "sign-in", "page": "/src/1.html"})
    await settle()
    check("a press the flow mentions moves the app to the next capture",
          session.hub.page == "/src/4.html", session.hub.page)
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
          session.hub.page == "/src/4.html" and len(alice.sent) == count,
          (session.hub.page, alice.sent[count:]))

    # ---- a step that waits for the browser ------------------------------
    # The app does not move on because a button was pressed: it moves when
    # the browser has taken the details and gone on to the next step. Until
    # then the capture stays where it is, and what the browser says about
    # the details is shown on it.
    (flow_root / "flow.json").write_text(json.dumps({
        "steps": [
            {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
             "live": True, "wait": True},
        ]
    }), encoding="utf-8")

    session.hub.page = "/src/4.html"
    session.hub.browser = {"url": "https://idmsa.apple.com/signin",
                           "title": "Sign in to your Apple Account",
                           "fields": {"account_name_text_field": {"value": "x"},
                                      "password_text_field": {"value": "x"}}}
    count = len(alice.sent)
    bob.feed({"t": "press", "id": "sign-in", "page": "/src/4.html"})
    await settle()
    check("a step that waits does not move the app on the press alone",
          session.hub.page == "/src/4.html", session.hub.page)
    check("...and the app is waiting for the browser",
          bool(session.waiting), session.waiting)

    # The browser says no: the form is still there, and the field that was
    # pressed is still there.
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/signin",
        "fields": {"account_name_text_field": {"value": "x"},
                   "password_text_field": {"value": "x"}},
    })
    check("...and it stays where it is while the browser has not moved on",
          session.hub.page == "/src/4.html", session.hub.page)

    # The browser says the details are wrong. The complaint adds elements
    # of its own to the form, and that is not the browser moving on: this is
    # the mistake that carried the person off to the code page while the
    # browser was still asking for the right password.
    bob.feed({"t": "press", "id": "sign-in", "page": "/src/4.html"})
    await settle()
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/signin",
        "fields": {"account_name_text_field": {"value": "x"},
                   "password_text_field": {"value": "x"},
                   "sign-in": {"value": ""},
                   "a-complain-about-this": {"value": ""}},
        "messages": [{"text": "Check the account information you entered "
                              "and try again."}],
    })
    check("...and a complaint from the browser keeps it where it is",
          session.hub.page == "/src/4.html", session.hub.page)

    # Even if the browser says it somewhere else, a complaint means the
    # details are wrong: there is no next step to go to yet.
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/verify",
        "fields": {},
        "messages": [{"text": "Check the account information you entered "
                              "and try again."}],
    })
    check("...even when the browser's address has changed",
          session.hub.page == "/src/4.html", session.hub.page)
    check("...and it is still waiting",
          bool(session.waiting), session.waiting)

    # A page that moved is not a step forward either: the browser can
    # reload, or change its address, and still be asking the same question.
    bob.feed({"t": "press", "id": "sign-in", "page": "/src/4.html"})
    await settle()
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/signin?accountName=x",
        "fields": {"account_name_text_field": {"value": "x"}},
        "messages": [],
    })
    check("...and the browser moving its address does not move it either",
          session.hub.page == "/src/4.html", session.hub.page)

    # The complaint is gone and the form is gone: now it moves on.
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/verify",
        "fields": {},
        "messages": [],
    })
    check("...and it moves on once there is nothing wrong",
          session.hub.page == "/src/2.html", session.hub.page)

    # The browser has moved on: the form is gone.
    await server.check_waiting(session, {"url": "https://idmsa.apple.com/verify",
                                         "fields": {}})
    check("...and it moves on when the browser has moved on",
          session.hub.page == "/src/2.html", session.hub.page)
    check("...and it stops waiting",
          session.waiting is None, session.waiting)

    # ---- the box that stays on -------------------------------------------
    # "Keep me signed in" is still there when the sign-in form turns into
    # the code step. Watching every field of the form would hold the app on
    # the sign-in page for ever - the fields the person filled in are gone,
    # and the browser has moved on.
    session.hub.page = "/src/4.html"
    session.hub.browser = {"url": "https://idmsa.apple.com/signin",
                           "title": "Sign in to your Apple Account",
                           "fields": {
                               "account_name_text_field": {"value": "x",
                                                          "type": "text"},
                               "remember-me": {"value": "on", "checked": False,
                                               "type": "checkbox"}}}
    bob.feed({"t": "press", "id": "sign-in", "page": "/src/4.html"})
    await settle()
    check("the box that stays on is not one of the fields to watch",
          session.waiting.get("fields") == ["account_name_text_field"],
          session.waiting)

    # The form is gone and the code step has taken its place. The box is
    # still there, and the app moves on anyway.
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/verify",
        "fields": {"remember-me": {"value": "on", "checked": True,
                                   "type": "checkbox"},
                   "code": {"value": "", "type": "text"}},
        "messages": [],
    })
    check("...and the app moves on when the browser has moved on",
          session.hub.page == "/src/2.html", session.hub.page)

    # A box that is ticked is not a field the person filled in, so it is
    # not watched: only the fields that were filled in are.
    check("a ticked box is not watched for",
          server.watch_fields({
              "account_name_text_field": {"value": "x", "type": "text"},
              "remember-me": {"value": "on", "checked": True,
                              "type": "checkbox"}}) == ["account_name_text_field"],
          server.watch_fields({
              "account_name_text_field": {"value": "x", "type": "text"},
              "remember-me": {"value": "on", "checked": True,
                              "type": "checkbox"}}))
    check("...and with nothing filled in, the fields on the page are",
          server.watch_fields({
              "account_name_text_field": {"value": "", "type": "text"},
              "remember-me": {"value": "on", "checked": False,
                              "type": "checkbox"}}) == ["account_name_text_field"],
          server.watch_fields({
              "account_name_text_field": {"value": "", "type": "text"},
              "remember-me": {"value": "on", "checked": False,
                              "type": "checkbox"}}))

    # ---- the step the browser names ---------------------------------------
    # The sign-in fields are not a good enough sign that the browser has
    # moved on: Apple's widget can keep the form in the page while the step
    # underneath it changes. So the flow says which page the browser has to
    # be showing, and the app waits for that.
    (flow_root / "flow.json").write_text(json.dumps({
        "steps": [
            {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
             "live": True, "wait": True,
             "until": {"heading": "Two-Factor Authentication"}},
        ]
    }), encoding="utf-8")

    session.hub.page = "/src/4.html"
    bob.feed({"t": "press", "id": "sign-in", "page": "/src/4.html"})
    await settle()
    check("the app is waiting for the page the browser has to be showing",
          (session.waiting or {}).get("until") == {"heading": "Two-Factor Authentication"},
          session.waiting)

    # The browser is still on the sign-in form: it has not moved on.
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/signin",
        "heading": "Sign in to your Apple Account",
        "fields": {"account_name_text_field": {"value": "x"},
                   "password_text_field": {"value": "x"}},
        "messages": [],
    })
    check("...and it stays while the browser is on the sign-in form",
          session.hub.page == "/src/4.html", session.hub.page)

    # The form is gone, but the browser is on some other page.
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/verify",
        "heading": "Verify your identity",
        "fields": {},
        "messages": [],
    })
    check("...and it stays while the browser shows another page",
          session.hub.page == "/src/4.html", session.hub.page)

    # A complaint comes first: it means the details are wrong.
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/signin",
        "heading": "Two-Factor Authentication",
        "fields": {},
        "messages": [{"text": "Check the account information you entered "
                              "and try again."}],
    })
    check("...and a complaint keeps it where it is",
          session.hub.page == "/src/4.html", session.hub.page)

    # The browser is showing the page it was waited for.
    await server.check_waiting(session, {
        "url": "https://idmsa.apple.com/verify",
        "heading": "Two-Factor Authentication",
        "fields": {},
        "messages": [],
    })
    check("...and it moves on when the browser is showing it",
          session.hub.page == "/src/2.html", session.hub.page)
    check("...and it stops waiting",
          session.waiting is None, session.waiting)

    # ---- the account that is locked --------------------------------------
    # Apple locks an account that has been signed in to from somewhere new,
    # and the page it shows says so - "This Apple Account has been locked for
    # security reasons." - instead of asking for the code. Nothing else in the
    # journey is coming, so the person is taken to the capture that says what
    # happened, and the session is marked as one that went wrong.
    (flow_root / "flow.json").write_text(json.dumps({
        "blocked": {"says": "This Apple Account has been locked for security reasons",
                    "go": "/src/5.html"},
        "steps": [
            {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
             "live": True, "wait": True,
             "until": {"heading": "Two-Factor Authentication"}},
        ]
    }), encoding="utf-8")

    check("the flow says which words mean the account is locked",
          server.flow_blocked().get("go") == "/src/5.html" and
          "locked for security reasons" in server.flow_blocked().get("says", ""),
          server.flow_blocked())

    locked_page = {
        "url": "https://idmsa.apple.com/appleauth/auth/signin",
        "title": "Sign in to your Apple Account",
        "heading": "",
        "alert": "This Apple Account has been locked for security reasons.",
        "fields": {},
        "messages": [],
    }
    check("the browser saying it is what is looked for",
          server.account_is_locked(locked_page).startswith(
              "This Apple Account has been locked"),
          server.account_is_locked(locked_page))
    check("...and a page that says something else is not it",
          server.account_is_locked({"alert": "",
                                    "heading": "Two-Factor Authentication"}) == "")

    session.hub.page = "/src/4.html"
    session.state["error"] = ""
    session.waiting = {"field": "sign-in", "fields": ["account_name_text_field"],
                       "heading": "", "url": "", "go": "/src/2.html",
                       "page": "/src/4.html", "since": time.monotonic()}
    check("a locked account is not carried on to the code",
          await server.check_locked(session, locked_page) is True and
          session.hub.page == "/src/5.html", session.hub.page)
    check("...and the step that is not coming is not waited for",
          session.waiting is None, session.waiting)
    check("...and what the browser said is what the panel shows",
          session.state["error"].startswith("This Apple Account has been locked"),
          session.state["error"])

    # And it holds: a person who goes back to the sign-in is taken to the page
    # that says why, rather than to a form that cannot work.
    session.hub.page = "/src/1.html"
    await server.check_locked(session, locked_page)
    check("...and a page that goes back to the form is sent to it again",
          session.hub.page == "/src/5.html", session.hub.page)

    # A flow with no such page says nothing, and nothing is taken away.
    (flow_root / "flow.json").write_text(json.dumps({"steps": []}),
                                         encoding="utf-8")
    session.hub.page = "/src/4.html"
    check("a flow that says nothing about it leaves the capture alone",
          await server.check_locked(session, locked_page) is False and
          session.hub.page == "/src/4.html", session.hub.page)

    # ---- editing the address ---------------------------------------------
    # The password step: the address can be edited there, and when it is,
    # the capture goes back to the address - the way iCloud does it. The
    # flow says which field that is.
    (flow_root / "flow.json").write_text(json.dumps({
        "steps": [
            {"page": "/src/4.html", "press": "sign-in", "go": "/src/2.html",
             "live": True, "wait": True,
             "edits": {"account_name_text_field": "/src/1.html"}},
        ]
    }), encoding="utf-8")

    check("the flow says what editing a field leads to",
          server.edit_go("/src/4.html", "account_name_text_field") == "/src/1.html",
          server.edit_go("/src/4.html", "account_name_text_field"))
    check("...and says nothing about a field it does not name",
          server.edit_go("/src/4.html", "password_text_field") == "",
          server.edit_go("/src/4.html", "password_text_field"))
    check("...nor about a page it does not know",
          server.edit_go("/src/1.html", "account_name_text_field") == "",
          server.edit_go("/src/1.html", "account_name_text_field"))

    session.hub.page = "/src/4.html"
    count = len(alice.sent)
    bob.feed({"t": "edit", "id": "account_name_text_field",
              "value": "e@mail.com", "page": "/src/4.html"})
    await settle()
    check("editing the address takes the capture back to the address",
          session.hub.page == "/src/1.html", session.hub.page)
    check("...and the other viewer goes with it",
          alice.sent[-1] == {"t": "nav", "page": "/src/1.html"}, alice.sent[-1:])

    # A field the flow does not name changes nothing.
    session.hub.page = "/src/4.html"
    bob.feed({"t": "edit", "id": "password_text_field",
              "value": "another", "page": "/src/4.html"})
    await settle()
    check("editing a field the flow does not name changes nothing",
          session.hub.page == "/src/4.html", session.hub.page)

    # ---- the end of the road ----------------------------------------------
    # The code was good: the browser is in iCloud and the sign-in form is
    # gone. Then the person is sent to iCloud, and the captures are closed
    # to them.
    check("a browser still on the sign-in form is not signed in",
          server.browser_is_signed_in({
              "url": "https://idmsa.apple.com/signin",
              "fields": {"account_name_text_field": {"value": "x"}}}) is False)
    check("...and a browser on iCloud with the form still there is not either",
          server.browser_is_signed_in({
              "url": "https://www.icloud.com/",
              "fields": {"password_text_field": {"value": "x"}}}, True) is False)
    check("...but a browser in iCloud with the form gone has signed in",
          server.browser_is_signed_in({
              "url": "https://www.icloud.com/",
              "fields": {"code": {"value": ""}}}, True) is True)
    check("...and a browser that has only just opened icloud.com has not",
          server.browser_is_signed_in({
              "url": "https://www.icloud.com/",
              "fields": {}}) is False)
    check("...because the form has to have been there first",
          server.browser_is_signed_in({
              "url": "https://www.icloud.com/",
              "fields": {}}, True) is True)
    check("...and a browser nowhere is not signed in",
          server.browser_is_signed_in({"url": "", "fields": {}}) is False)

    session.hub.page = "/src/2.html"
    count = len(alice.sent)
    session.hub.go_to_icloud()
    await settle()
    check("a person who has finished is sent to iCloud",
          alice.sent[-1] == {"t": "finished", "page": server.ICLOUD}, alice.sent[-1:])
    check("...and the session knows they have finished",
          session.hub.finished is True)

    # The captures are closed to them: a capture opened now is not served,
    # it is a way back to iCloud.
    answer = await server.captured(make_session_request("the-tests"), "1.html")
    check("a capture for somebody who has finished is sent to iCloud",
          answer.status_code in (301, 302, 307, 308) and
          answer.headers.get("location") == server.ICLOUD,
          (answer.status_code, answer.headers.get("location")))

    # ...and there is no going back to a capture.
    session.hub.navigate("/src/1.html")
    check("...and there is no going back to a capture",
          session.hub.page == server.ICLOUD, session.hub.page)

    # The launcher sends them there as well.
    launcher = await server.index(make_session_request("the-tests"))
    check("the launcher sends them to iCloud as well",
          launcher.status_code in (301, 302, 307, 308) and
          launcher.headers.get("location") == server.ICLOUD,
          (launcher.status_code, launcher.headers.get("location")))

    # "keep me signed in" ----------------------------------------------
    # The box is ticked in the browser, every time - but a box that is
    # already ticked is not looked at again: the browser is not asked about
    # it every half second, and a click on a ticked box unticks it.
    check("a page with no box on it has nothing to tick",
          server.remember_me_is_off({"fields": {}}) is False)
    check("...and neither has a field that is not a box",
          server.remember_me_is_off({"fields": {
              "remember-me": {"value": "on", "type": "text"}}}) is False)
    check("...but a box that is off has to be ticked",
          server.remember_me_is_off({"fields": {
              "remember-me": {"value": "on", "checked": False,
                              "type": "checkbox"}}}) is True)
    check("...and a box that is on does not",
          server.remember_me_is_off({"fields": {
              "remember-me": {"value": "on", "checked": True,
                              "type": "checkbox"}}}) is False)

    # ---- every visitor gets their own browser ---------------------------
    one = server.session_for("visitor-one")
    two = server.session_for("visitor-two")
    check("two visitors have two browsers",
          one.bridge is not two.bridge and one.hub is not two.hub)
    check("two visitors do not share a page",
          one.hub.page != two.hub.page or one.hub is not two.hub)
    one.hub.navigate("/src/4.html", everyone=True)
    check("...and one cannot move the other",
          two.hub.page != "/src/4.html", two.hub.page)
    check("a visitor's browser keeps its own profile",
          one.profile() != two.profile() and one.profile().name == one.id,
          one.profile())

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

    # ---- one person is one visitor --------------------------------------
    # A header called `icloud_session` is not a cookie: the browser keeps no
    # cookie, so every request looked like a new visitor - twenty sessions
    # for one person, each with a browser and a profile of its own.
    cookie = server.session_cookie("the-tests")
    check("the cookie is a real Set-Cookie",
          cookie.startswith(f"{server.SESSION_COOKIE}=the-tests;") and
          "Path=/" in cookie and "Max-Age=" in cookie, cookie)
    check("the cookie is a name, not a secret",
          "HttpOnly" not in cookie and "Secure" not in cookie, cookie)

    # The routes that only read must not make a visitor.
    before = set(server.SESSIONS)
    answer = await server.status(make_session_request("nobody-at-all"))
    check("asking about a stranger makes no session",
          set(server.SESSIONS) == before, set(server.SESSIONS) - before)
    check("...and says there is nothing going on",
          answer.get("status") == "idle" and answer.get("session") == "",
          answer)

    await server.nav(make_request({"page": "/src/4.html"}, session="nobody"))
    check("a beacon from a stranger makes no session",
          set(server.SESSIONS) == before, set(server.SESSIONS) - before)

    # The same name always means the same visitor.
    first = server.session_for("the-tests")
    second = server.session_for("the-tests")
    check("the same name is the same visitor", first is second, (first.id, second.id))
    check("...and the same browser",
          first.bridge is second.bridge and first.profile() == second.profile())

    # The name can also come with the request, the way the page sends it:
    # the page keeps the name in its own storage and says who it is.
    named = Request({"type": "http", "method": "GET", "path": "/",
                     "query_string": b"visitor=the-page-says-so",
                     "headers": []}, None)
    session = await server.index(named)
    check("the page can say who the visitor is",
          session.headers.get("set-cookie", "").startswith(
              f"{server.SESSION_COOKIE}=the-page-says-so;"),
          session.headers.get("set-cookie"))
    check("...and the browser is that visitor's own",
          server.session_for("the-page-says-so").profile().name ==
          "the-page-says-so",
          server.session_for("the-page-says-so").profile())
    check("...and the profile is where the base branch keeps it",
          server.session_for("the-page-says-so").profile().parent.name ==
          "visitors", server.session_for("the-page-says-so").profile())

    # The launcher says who the visitor is, in a cookie the browser keeps.
    launcher = await server.index(make_session_request("the-tests"))
    sent = launcher.headers.get("set-cookie", "")
    check("the launcher sets the cookie",
          sent.startswith(f"{server.SESSION_COOKIE}=the-tests;") and
          "Path=/" in sent, sent)
    check("the launcher does not make a new visitor for the same name",
          launcher.headers.get("set-cookie", "") ==
          server.session_cookie("the-tests"), sent)

    # ---- who is here, and who has gone -----------------------------------
    # The server asking the browser a question is not the person being
    # here. A session that counts the server's own polling as the visitor's
    # presence is a session that is never idle, and a browser that is
    # never closed - so a client who has gone never shows as gone.
    idle = server.session_for("the-one-who-left")
    idle.state["browser_open"] = True
    idle.seen -= server.SESSION_IDLE_SECONDS + 10
    before = len(server.SESSIONS)
    await server.close_idle_sessions()
    check("a client who has gone is closed and remembered",
          "the-one-who-left" not in server.SESSIONS and
          "the-one-who-left" in server.GONE, sorted(server.GONE))
    check("...and their browser is shut",
          idle.state["browser_open"] is False, idle.state)

    # Somebody is here: their browser stays open, however long ago they
    # were last heard from.
    busy = server.session_for("the-one-who-is-here")
    busy.state["browser_open"] = True
    busy.seen -= server.SESSION_IDLE_SECONDS + 10

    class FakeViewer:
        def send(self, message): pass
        def close(self): pass

    busy.hub.viewers.add(FakeViewer())
    await server.close_idle_sessions()
    check("a client somebody is looking at is not closed",
          "the-one-who-is-here" in server.SESSIONS and
          busy.state["browser_open"] is True, busy.state)

    # And a client who comes back is here, not gone.
    server.session_for("the-one-who-left")
    check("a client who comes back is here, not gone",
          "the-one-who-left" in server.SESSIONS and
          "the-one-who-left" not in server.GONE, sorted(server.GONE))

    # The page saying something is the person doing something.
    talking = server.session_for("the-one-who-is-typing")
    talking.seen -= server.SESSION_IDLE_SECONDS + 10
    talking.hub.handle(FakeViewer(), {"t": "where"}, talking)
    check("a page saying something keeps its client alive",
          time.monotonic() - talking.seen < 5, talking.seen)

    # ---- "Trust this browser?", and what signing in leaves behind --------
    # After the verification code is accepted the browser asks whether it
    # may be trusted, and it sits on the question until it is answered. The
    # app answers it, and then the client is in - and what gets them back
    # when they are not here is kept while the browser is open.
    scratch = Path(tempfile.mkdtemp())
    admin.STORE = admin.Store(scratch / "panel.json")
    admin.COOKIE_STORE = scratch / "cookies"

    signed_in_page = {"url": "https://www.icloud.com/",
                      "title": "iCloud", "heading": "", "fields": {},
                      "messages": [], "trust": False}
    trust_page = {"url": "https://idmsa.apple.com/appleauth/auth/2sv/trust",
                  "title": "Two-Factor Authentication", "heading": "",
                  "fields": {}, "messages": [], "trust": True}
    signed_in_cookies = [
        {"domain": ".icloud.com", "name": "X-APPLE-WEBAUTH-TOKEN",
         "value": '"v=2:t=Bw==', "path": "/", "httpOnly": True,
         "secure": True, "expires": None},
    ]

    class FakeBridge:
        """A browser, as far as the watching of it is concerned."""

        def __init__(self, page, after_trust=None, presses=True):
            self.page = dict(page)
            self.after_trust = dict(after_trust or page)
            self.presses = presses
            self.asked = 0
            self.pressed = 0
            self.ticks = 0

        def is_open(self):
            return True

        def detach(self):
            self.page = {}

        async def flush_pending(self):
            pass

        async def snapshot(self):
            return dict(self.page)

        async def tick_remember_me(self):
            self.ticks += 1
            return True

        async def accept_trust(self):
            self.asked += 1
            if not self.presses:
                return False
            self.pressed += 1
            self.page = dict(self.after_trust)
            return True

    class FakeBrowserContext:
        def __init__(self, cookies):
            self.sent = cookies

        async def cookies(self):
            return [dict(cookie) for cookie in self.sent]

    trusting = server.session_for("the-one-who-trusts")
    trusting.saw_sign_in = True                   # they signed in on this
    trusting.bridge = FakeBridge(trust_page, signed_in_page)
    trusting.ctx = FakeBrowserContext(signed_in_cookies)
    await server.poll_browser(trusting)

    check("the browser asking to be trusted is answered",
          trusting.bridge.pressed == 1, trusting.bridge.pressed)
    check("...and the client is through once it is",
          trusting.hub.finished and trusting.hub.page == server.ICLOUD,
          (trusting.hub.finished, trusting.hub.page))

    check("a client who signed in may have their cookies exported",
          admin.STORE.export_allowed("the-one-who-trusts") is True,
          admin.STORE.data.get("exports"))
    check("...and their cookies were read while the browser was open",
          [c["name"] for c in trusting.cookies] == ["X-APPLE-WEBAUTH-TOKEN"],
          trusting.cookies)
    check("...and written down, so they can be had after it is shut",
          admin.kept_cookies("the-one-who-trusts") == trusting.cookies,
          admin.kept_cookies("the-one-who-trusts"))
    kept_file = admin.COOKIE_STORE / "the-one-who-trusts.json"
    check("...in a file that is a secret",
          kept_file.is_file() and
          oct(kept_file.stat().st_mode)[-3:] == "600",
          oct(kept_file.stat().st_mode) if kept_file.exists() else "absent")

    # Polling again is not signing in again: nothing is pressed twice, and
    # the person who is through is not sent through again every half second.
    class Watching:
        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)

        def close(self):
            pass

    watching = Watching()
    trusting.hub.viewers.add(watching)
    await server.poll_browser(trusting)
    check("signing in is kept once, not once a poll",
          trusting.bridge.asked == 1 and trusting.bridge.pressed == 1,
          (trusting.bridge.asked, trusting.bridge.pressed))
    check("...and a client who is through is not sent there again",
          [m for m in watching.sent if m.get("t") == "finished"] == [],
          watching.sent)
    trusting.hub.viewers.discard(watching)

    # And when they go, the record of them holds the cookies.
    trusting.seen -= server.SESSION_IDLE_SECONDS + 10
    await server.close_idle_sessions()
    check("a client who signed in keeps their cookies when they go",
          [c["name"] for c in server.GONE["the-one-who-trusts"].get("cookies", [])]
          == ["X-APPLE-WEBAUTH-TOKEN"], server.GONE.get("the-one-who-trusts"))
    check("...and the panel can read them with nothing but the record",
          len(await admin.cookies_of("the-one-who-trusts")) == 1)

    # With the record gone - the server restarted - the cookies are still
    # there, written down while the browser was open.
    server.GONE.pop("the-one-who-trusts", None)
    kept = await admin.cookies_of("the-one-who-trusts")
    check("...and they are still there after a restart",
          len(kept) == 1 and kept[0]["name"] == "X-APPLE-WEBAUTH-TOKEN", kept)

    # A question that will not go does not hold the person there for ever.
    stuck = server.session_for("the-one-who-will-not-trust")
    stuck.saw_sign_in = True
    stuck.bridge = FakeBridge({**signed_in_page, "trust": True}, presses=False)
    stuck.ctx = FakeBrowserContext([])
    stuck.trust_since = time.monotonic() - server.TRUST_WAIT_SECONDS - 1
    await server.poll_browser(stuck)
    check("a Trust question that will not go is carried on past",
          stuck.bridge.asked == 1 and stuck.hub.finished,
          (stuck.bridge.asked, stuck.hub.finished))


asyncio.run(main())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
