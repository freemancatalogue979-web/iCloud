"""A copy of the app that can live anywhere.

The app is two halves. The server holds the browsers, the sessions and the
sign-in; the pages are what a visitor looks at. The pages can be served by
the server, or by anything else at all - a cPanel, a bucket, a CDN - as
long as they can reach the server.

This module makes that copy: a zip with the pages, the engine that makes
them live, and a note saying what to do with them.

Three things are different in the copy, and all three are because the
pages are somewhere else:

* the engine is found next to the pages, not at ``/sync.js``;
* the pages are told where the server is, so they know who to talk to; and
* the pages are allowed to talk to that server - their own policy does
  not know about it.

Everything else is the same page it was, with the same engine in it, so
what the visitor sees is what they would have seen.
"""

import io
import re
import zipfile
from pathlib import Path

HERE = Path(__file__).parent

# The pages the visitor sees, and the engine that makes them live.
SRC = HERE / "src"
CLIENT = HERE / "client.html"
ENGINE = HERE / "sync.js"

# The name the page carries in the server's own naming. A copy that lives in
# a folder is still this page, as far as the server is concerned.
SERVER_TAG = '<meta name="icloud-server" content="{server}">'

# Where the engine is put in the head. The engine's own tag is put there by
# the server; this is where the note about the server goes, above it.
_HEAD = re.compile(r"<head\\b[^>]*>", re.IGNORECASE)
_NO_HEAD = re.compile(r"<body\\b[^>]*>", re.IGNORECASE)

NOTE = """iCloud - a copy of the app that lives anywhere
================================================

What this is
------------

The zip holds the pages a visitor sees. The server - the thing that holds
the browsers, the sessions and the sign-in - stays where it is: these
pages have been told where it is, and they talk to it from here.

What is in it
-------------

  client.html      the launcher: opens the browser and waits for it
  src/*.html       the captures - the pages of the sign-in
  src/sync.js      the engine that makes the captures live
  src/flow.json    a copy of the server's flow, for reading only - the
                   pages do not use it

How to put it somewhere
-----------------------

1. Unpack the zip into the folder your host serves - often public_html,
   or a subfolder of it.
2. Open client.html through the address of that folder.
3. That is all. There is nothing to run here: this is only pages.

Two things worth knowing
-------------------------

* The pages have to be able to reach the server. The server has to be
  reachable from wherever the pages are served from - and from the
  browser, which is the visitor's own machine, not this host.
* The pages are served over https, so the server has to be as well: a
  page on https talking to a server on http is refused by the browser.
* **The server has to be told where this copy lives.** A browser will not
  let a page on one host ask a server on another unless the server says it
  may. Put the address of this copy - the folder it was unpacked into, as
  the browser sees it - in the server's panel, in the Standalone tab, under
  "Where the copy will live". Until that is done, the pages can be seen
  but the browser will not open.

The question at the door
------------------------

The server asks visitors whether they are people before it opens a
browser for them, and this copy is asked the same question. It asks the
server for the question when the page loads, so:

* nothing about the question is written into this zip - the key the
  server uses is public, and the server is the one that has it;
* a key changed in the server's panel is live in this copy at once, with
  nothing to unpack again and nothing to upload again.

If a visitor is turned away at the door, the server's log says why.

If the pages do not come up
---------------------------

Open the browser's console (F12) and look.

* "Refused to connect" means the server cannot be reached.
* "a policy error means the page was not allowed to talk to it" - and the
  server is what says that. Put this copy's address in the panel.
* "was loaded over HTTPS, but attempted to connect to the insecure
  WebSocket endpoint" means the server's address in the panel is on http.
  It has to be on https.
"""


def origin_of(server: str) -> str:
    """The scheme and the host of the server, and nothing else.

    This is what a page's own policy is allowed to name: an address, not a
    path.
    """
    said = (server or "").strip()
    found = re.match(r"^([a-z][a-z0-9+.-]*://[^/?#]+)", said, re.IGNORECASE)
    return found.group(1) if found else ""


def with_server(page_html: str, server: str) -> str:
    """A page with the server's address in it.

    The address goes in a meta tag in the head, above the engine: the
    engine reads it as soon as it runs.
    """
    tag = SERVER_TAG.format(server=server)
    if _HEAD.search(page_html):
        return _HEAD.sub(lambda m: m.group(0) + tag, page_html, count=1)
    if _NO_HEAD.search(page_html):
        return _NO_HEAD.sub(lambda m: tag + m.group(0), page_html, count=1)
    return tag + page_html


def build(server: str) -> bytes:
    """The zip, ready to be downloaded.

    ``server`` is the address the server is reached at, from the outside
    world: "https://icloud.example.com". It is put in every page, and the
    pages are allowed to talk to it.

    No key is put in the zip. The copy asks the server for the question it
    is to be asked when it loads - the site's key is public, and the
    server is the one that has it - so a key changed in the panel is live
    in the copy without the zip being unpacked again.
    """
    said = (server or "").strip().rstrip("/")
    if not said:
        raise ValueError("the server's address is not known")
    if not re.match(r"^https?://", said, re.IGNORECASE):
        said = "https://" + said

    # The server's own preparation is what a capture needs: the engine in
    # it, its frames reachable, its policy widened for the engine. It lives
    # with the server that serves the pages, so it is asked for here.
    import server  # imported here, not at the top: the server imports this

    allowed = origin_of(said)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # The launcher.
        launcher = CLIENT.read_text(encoding="utf-8")
        zip_file.writestr("client.html", with_server(launcher, said))

        # The captures, with the engine in them and the engine beside them.
        if SRC.is_dir():
            for page in sorted(SRC.glob("*.html")):
                source = page.read_text(encoding="utf-8", errors="replace")
                source = with_server(source, said)
                source = server.with_engine(source, connect=allowed,
                                           script="sync.js")
                zip_file.writestr(f"src/{page.name}", source)

            flow = SRC / "flow.json"
            if flow.is_file():
                zip_file.writestr("src/flow.json",
                                  flow.read_text(encoding="utf-8"))

        # The engine itself, which the captures look for beside them.
        if ENGINE.is_file():
            zip_file.writestr("src/sync.js",
                              ENGINE.read_text(encoding="utf-8"))

        zip_file.writestr("README.txt", NOTE)

    return buffer.getvalue()
