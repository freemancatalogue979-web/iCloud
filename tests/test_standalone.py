"""A copy of the app that lives somewhere else.

The copy has to do three things the pages on the server do not have to:
find the engine beside them, be told where the server is, and be allowed
to talk to it. Those three are what is pinned here - along with the one
that matters most: the pages are the same pages.

Run with: python tests/test_standalone.py
"""

import asyncio
import io
import re
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import standalone  # noqa: E402

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


SERVER = "https://icloud.example.com"


class Host(BaseHTTPRequestHandler):
    """A host that answers with whatever it was told to answer with."""

    pages: dict = {}

    def do_GET(self):
        body = Host.pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>nothing here</body></html>")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(pages):
    """A host on a free port, answering with these pages."""
    Host.pages = pages
    host = HTTPServer(("127.0.0.1", 0), Host)
    threading.Thread(target=host.serve_forever, daemon=True).start()
    return host, f"http://127.0.0.1:{host.server_port}"


def main():
    data = standalone.build(SERVER)
    zip_file = zipfile.ZipFile(io.BytesIO(data))
    names = zip_file.namelist()

    # -- what is in it ---------------------------------------------------
    check("the launcher is in it", "client.html" in names, names)
    check("the captures are in it",
          "src/1.html" in names and "src/4.html" in names, names)
    check("the engine is in it, beside the captures",
          "src/sync.js" in names, names)
    check("a note is in it", "README.txt" in names, names)
    check("the zip is not empty", len(data) > 1000, len(data))

    # -- the launcher under both of its names -----------------------------
    # A host that serves the root of a folder with index.html - which is
    # most of them - needs the launcher there. The two are the same page,
    # so they cannot drift apart.
    check("the launcher is in it under both of its names",
          "index.html" in names, names)
    check("...and the two are the same page",
          zip_file.read("index.html") == zip_file.read("client.html"))

    # -- and the Worker that serves it -----------------------------------
    # A host that answers every path with the same file sends the visitor
    # round in circles for ever. The Worker in the zip is ready to be
    # deployed as it is, and it does not do that.
    check("the Worker that serves the copy is in it",
          "worker.js" in names and "wrangler.toml" in names, names)
    worker_settings = zip_file.read("wrangler.toml").decode("utf-8")
    check("...and it is not a single-page fallback",
          'not_found_handling = "none"' in worker_settings
          and "single-page-application" in worker_settings,
          worker_settings)
    check("...and it serves the files as they are",
          "env.SITE.fetch(request)" in
          zip_file.read("worker.js").decode("utf-8"))

    # -- the pages are told where the server is --------------------------
    capture = zip_file.read("src/1.html").decode("utf-8")
    launcher = zip_file.read("client.html").decode("utf-8")

    check("a capture says where the server is",
          f'<meta name="icloud-server" content="{SERVER}">' in capture)
    check("the launcher says where the server is",
          f'<meta name="icloud-server" content="{SERVER}">' in launcher)
    check("the note is in the head of the capture, above the engine",
          capture.index("icloud-server") < capture.index("sync.js"),
          (capture.index("icloud-server"), capture.index("sync.js")))

    # -- and they are allowed to talk to it ------------------------------
    policy = re.search(r'content="([^"]*connect-src[^"]*)"', capture)
    check("the capture's own policy names the server",
          policy and SERVER in policy.group(1),
          policy.group(1) if policy else "no policy found")
    check("...and still allows its own origin and its socket",
          policy and "'self'" in policy.group(1) and "wss:" in policy.group(1),
          policy.group(1) if policy else "")

    # -- and the note says where the copy has to be registered -----------
    note = zip_file.read("README.txt").decode("utf-8")
    check("the note says the server has to be told where the copy lives",
          "Where the copy will live" in note, note[:200])
    check("...and says what happens without it",
          "will not let" in note or "policy error" in note, note[:200])

    # -- the engine is beside them ---------------------------------------
    check("the engine is found beside the capture, not at /sync.js",
          '<script src="sync.js"></script>' in capture)
    check("...and not at the server's own address",
          '<script src="/sync.js"></script>' not in capture)
    check("the engine in the zip is the engine in the repo",
          zip_file.read("src/sync.js") ==
          standalone.ENGINE.read_bytes())

    # -- the pages are the same pages ------------------------------------
    # The engine is in them, the frames are reachable, and the font is
    # still inside - none of which the copy may have lost.
    check("the copy still has the engine's own tag", "sync.js" in capture)
    check("the copy still has the frame the form is in",
          "srcdoc" in capture)
    check("the copy still carries its font inside",
          "data:font" in capture)
    check("the copy did not lose the capture's own policy",
          "content-security-policy" in capture)

    # -- the address is put in it right ----------------------------------
    check("the address the server is at is put in as it was given",
          f'content="{SERVER}"' in capture)
    again = zipfile.ZipFile(io.BytesIO(standalone.build(SERVER + "/")))
    check("a trailing slash is not put in twice",
          f'content="{SERVER}"' in again.read("src/1.html").decode("utf-8"))
    bare = zipfile.ZipFile(io.BytesIO(standalone.build("icloud.example.com")))
    check("an address with no scheme gets one",
          'content="https://icloud.example.com"'
          in bare.read("src/1.html").decode("utf-8"))

    # -- and it is refused when there is no address ----------------------
    try:
        standalone.build("")
        check("an empty address is refused", False, "it built one anyway")
    except ValueError:
        check("an empty address is refused", True)

    # -- and the copy is asked whether it is serving the captures -------
    # A host that answers every path with the same file sends the visitor
    # round in circles for ever. Nothing else says that it is doing it, so
    # the host is asked and the answer is read.
    launcher = b'<html><body><div id="icloud-turnstile"></div></body></html>'
    capture = b'<html><head><script src="sync.js"></script></head><body>the sign-in</body></html>'

    host, origin = serve({"/": launcher, "/src/1.html": launcher})
    try:
        answer = asyncio.run(standalone.check_copy(origin))
        check("a host answering every path with the launcher is caught",
              answer["kind"] == "launcher" and not answer["ok"], answer)
        check("...and it says what is wrong with it",
              "reload for ever" in answer["said"], answer["said"])
    finally:
        host.shutdown()

    host, origin = serve({"/src/1.html": capture})
    try:
        answer = asyncio.run(standalone.check_copy(origin))
        check("a host serving the capture is said to be right",
              answer["kind"] == "capture" and answer["ok"], answer)
    finally:
        host.shutdown()

    host, origin = serve({})
    try:
        answer = asyncio.run(standalone.check_copy(origin))
        check("a host with no file there is caught",
              answer["kind"] == "missing", answer)
    finally:
        host.shutdown()

    answer = asyncio.run(standalone.check_copy("http://127.0.0.1:1"))
    check("a host that cannot be reached is caught",
          answer["kind"] == "unreachable", answer)

    answer = asyncio.run(standalone.check_copy(""))
    check("a copy with no address is not checked",
          answer["kind"] == "unset", answer)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(main())
