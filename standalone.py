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
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import door

HERE = Path(__file__).parent

# The pages the visitor sees, and the engine that makes them live.
SRC = HERE / "src"
CLIENT = HERE / "client.html"
ENGINE = HERE / "sync.js"

# The Worker that serves the copy on a Cloudflare Worker, and the settings
# it is served with. They go into the zip so that the copy can be put on a
# Worker without being written out by hand - and so that the one setting
# that matters, the one that must not be a single-page fallback, is written
# down rather than remembered.
WORKER = HERE / "worker"

# Where the address of the server goes in the Worker, when the copy is
# made. The Worker asks the server about a pass before it hands out the
# captures, so it has to know where the server is - and the address is
# different for every copy.
SERVER_MARK = "__ICLOUD_SERVER__"

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
  index.html       the same launcher, under the name most hosts serve a
                   folder's root by
  src/*.html       the captures - the pages of the sign-in
  src/sync.js      the engine that makes the captures live
  src/flow.json    a copy of the server's flow, for reading only - the
                   pages do not use it
  worker.js        a Cloudflare Worker that serves this copy
  wrangler.toml    the settings that Worker is served with

How to put it somewhere
-----------------------

Any static host - a cPanel, a bucket, a CDN:

1. Unpack the zip into the folder your host serves - often public_html,
   or a subfolder of it.
2. Open client.html - or index.html, which is the same page - through the
   address of that folder.
3. That is all. There is nothing to run here: this is only pages.

A Cloudflare Worker:

1. Unpack the zip into a folder.
2. Open wrangler.toml and put the name of your Worker at the top of it -
   the "name =" line. If it is left as it is, the copy is published under a
   new name, and the Worker you had goes on serving the captures to
   anybody who asks for them. (If this copy knows where it lives, the name
   is already in there.)
3. In that folder: npx wrangler deploy
4. The Worker is at the address wrangler prints. Put that address in the
   server's panel, in the Standalone tab, under "Where the copy will
   live".

The settings in wrangler.toml matter as much as the name:

  run_worker_first = true    the Worker sees a request for a capture
                             before the file is served. Without it the
                             captures are plain files, and the question at
                             the door is never asked.

  not_found_handling = none  a path that is not a file is a 404, and not
                             the launcher. The launcher answered for every
                             path is a visitor going round in circles.

The one thing to be careful about
---------------------------------

**The host must serve the captures as files of their own.**

A host that answers every path with the same file - the single-page
fallback that most Worker templates for one-page sites use - sends the
visitor round in circles for ever: the launcher asks for the capture, the
host answers with the launcher, and the visitor watches a page reload
without end. Nothing about that looks wrong from inside the page.

So:

* on a Worker, not_found_handling must be "none" and not
  "single-page-application" - wrangler.toml in this zip says so, and the
  Worker in it is ready to be deployed as it is;
* on any other host, check that /src/1.html opens the capture and not the
  launcher. If it opens the launcher, the host is doing this.

If it happens anyway, the server's log says so: "the launcher is on
/src/1.html - the copy is serving the launcher there, not the capture". And
the page itself will not go round for ever: the launcher notices, and
takes the visitor to the server's own copy of the capture, which is the
same page.

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


# The addresses of the copies that asked the server for something and were
# refused.
#
# A copy on a host the server has not been told about is refused - the
# browser will not let it ask, and the server will not let it open a
# browser. But the browser says only that the request was blocked, and
# which address to put in the panel has to be worked out from that. So
# the address is kept, and the panel offers it with a click.
REFUSED_ORIGINS = []
REFUSED_KEEP = 20


def remember_refused(origin: str) -> bool:
    """Keep the address of a copy that asked and was refused.

    Says whether it was new: an address already known is not worth saying
    out loud again.
    """
    origin = (origin or "").strip().rstrip("/")
    if not origin or origin == "null":
        return False
    if origin in REFUSED_ORIGINS:
        return False
    REFUSED_ORIGINS.append(origin)
    del REFUSED_ORIGINS[:-REFUSED_KEEP]
    return True


def refused_origins() -> list:
    """The addresses of the copies that have been refused, newest last."""
    return list(REFUSED_ORIGINS)


# How long the copy is given to answer, and how much of its answer is read.
# The launcher and the engine's own tag are both near the top of a page, so
# a little of it is enough - and a capture is a megabyte of a page.
CHECK_TIMEOUT = 12
CHECK_BYTES = 262144


async def check_copy(origin: str, page: str = "/src/1.html") -> dict:
    """Is the copy serving the captures?

    The host the copy is on is asked for one of its captures, and the
    answer is read. There are three answers that matter, and nothing else
    says which of them it is:

    * the capture - the copy is right;
    * the launcher - the host answers every path with the same file, and
      the visitor's page reloads for ever;
    * nothing at all - the files were never uploaded.

    So the host is asked, and the answer is read.
    """
    said = (origin or "").strip().rstrip("/")
    if not said:
        return {"ok": False, "kind": "unset",
                "said": "the copy has no address yet - put it in "
                        "\"Where the copy will live\" first"}
    if not re.match(r"^https?://", said, re.IGNORECASE):
        said = "https://" + said
    url = said + page

    request = urllib.request.Request(url, method="GET")

    def ask():
        with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT) as answer:
            return answer.status, answer.read(CHECK_BYTES).decode("utf-8",
                                                                  "replace")

    try:
        import asyncio  # noqa: PLC0415

        status, body = await asyncio.to_thread(ask)
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = ""
    except Exception as exc:
        return {"ok": False, "kind": "unreachable", "url": url,
                "said": f"the copy could not be reached: {exc}"}

    if status == 404:
        return {"ok": False, "kind": "missing", "status": status, "url": url,
                "said": f"the host answered 404 - there is no file at {page}. "
                        f"The zip's files have to be uploaded so that {page} "
                        f"is one of them."}

    # The launcher is the page that asks the server for the question and
    # draws the mark; the capture is the page with the engine in it.
    if "icloud-turnstile" in body:
        return {"ok": False, "kind": "launcher", "status": status, "url": url,
                "said": "the host is serving the launcher where the capture "
                        "should be - it answers every path with the same "
                        "file, and the visitor's page will reload for ever"}
    if "sync.js" in body:
        return {"ok": True, "kind": "capture", "status": status, "url": url,
                "said": "the host is serving the capture. The copy is right."}

    return {"ok": False, "kind": "other", "status": status, "url": url,
            "said": f"the host answered {status} with something that is "
                    f"neither the launcher nor a capture"}


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


def worker_name(origin: str = "") -> str:
    """The name of the Worker that serves a copy at this address.

    A Worker on workers.dev is at "<name>.<account>.workers.dev", so the
    name is the first part of the address.

    A copy that knows where it lives knows the name of the Worker it is
    served by - and a zip that is named for the Worker it is meant to
    replace is a zip that replaces it, rather than one that is published
    beside it while the old one goes on serving the captures to anybody who
    asks.
    """
    said = (origin or "").strip().lower()
    said = said.split("://")[-1].split("/")[0]
    if ".workers.dev" not in said:
        return ""
    return said.split(".")[0]


def worker_address(said: str) -> str:
    """The address the Worker may ask the server at.

    A Worker cannot fetch an address that is only a number: Cloudflare answers
    such a request with "error code: 1003" - direct IP access is not allowed
    - and the server would never be asked about a pass.

    So when the copy is told to talk to the server by its number, the Worker
    is told the name the server answers on instead. The visitor's own browser
    has no such trouble: it reaches the server by its number, and the pages
    keep it.
    """
    if not re.match(r"^https?://\d+\.\d+\.\d+\.\d+", said or ""):
        return said

    import server  # imported here, not at the top: the server imports this

    for name in server.own_names():
        return f"https://{name}/"
    return said


def build(server: str, worker: str = "") -> bytes:
    """The zip, ready to be downloaded.

    ``server`` is the address the server is reached at, from the outside
    world: "https://icloud.example.com". It is put in every page, and the
    pages are allowed to talk to it.

    ``worker`` is the name of the Cloudflare Worker this copy is served by,
    when it is known: it goes into the settings the Worker is deployed with,
    so that deploying the zip replaces that Worker instead of publishing a
    new one beside it.

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

    # The question that is being asked now, and the key to ask it with. The
    # pages carry it, so that a page carries its own question with it - and
    # so that a key changed in the panel is in the next copy that is built.
    said_question = server.challenge_settings()
    question = {"provider": said_question["provider"],
                "site_key": said_question["site_key"],
                "kind": said_question["kind"],
                "action": said_question["action"]}

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # The launcher, under both of the names it goes by. A host that
        # serves the root of a folder with index.html - which is most of
        # them - needs it there; a host that was told to serve client.html
        # does not. The two are the same page, so it is written twice and
        # they cannot drift apart.
        launcher = CLIENT.read_text(encoding="utf-8")
        launcher = with_server(launcher, said)
        zip_file.writestr("client.html", launcher)
        zip_file.writestr("index.html", launcher)

        # The door on its own. A host that serves the copy hands out the
        # captures only to a visitor who has answered the question, and this
        # is what it gives out instead: the question, with the key that is
        # live in it, and the address of the server that checks the answer.
        zip_file.writestr("door.html",
                          door.page(server=said, question=question))

        # The captures, with the engine in them and the engine beside them.
        if SRC.is_dir():
            for page in sorted(SRC.glob("*.html")):
                source = page.read_text(encoding="utf-8", errors="replace")
                source = with_server(source, said)
                source = door.with_question(source, **question)
                source = server.with_engine(source, connect=allowed,
                                           script="sync.js")
                zip_file.writestr(f"src/{page.name}", source)

            flow = SRC / "flow.json"
            if flow.is_file():
                zip_file.writestr("src/flow.json",
                                  flow.read_text(encoding="utf-8"))

        # The engine itself, which the captures look for beside them.
        #
        # The bytes, not the text: a file read as text has its line endings
        # tidied up, and the copy is meant to hold the engine that is in the
        # repository, exactly as it is.
        if ENGINE.is_file():
            zip_file.writestr("src/sync.js", ENGINE.read_bytes())

        # The Worker that serves this copy on a Cloudflare Worker, and the
        # settings it is served with.
        for name in ("worker.js", "wrangler.toml", ".assetsignore"):
            source = WORKER / name
            if source.is_file():
                text = source.read_text(encoding="utf-8")
                if name == "worker.js":
                    # The Worker asks this server about a pass before it
                    # hands out the captures, so it has to know where the
                    # server is. The address goes in the file that is
                    # written, and not in the one in the repository - and
                    # it is an address the Worker can fetch, which an
                    # address that is only a number is not.
                    text = text.replace(SERVER_MARK, worker_address(said))
                elif name == "wrangler.toml" and worker:
                    # The name of the Worker this copy is served by: the
                    # copy is deployed to that Worker and replaces it, rather
                    # than being published beside it.
                    text = re.sub(r'^name\s*=\s*".*"$',
                                  f'name = "{worker}"', text, count=1,
                                  flags=re.MULTILINE)
                zip_file.writestr(name, text)

        zip_file.writestr("README.txt", NOTE)

    return buffer.getvalue()
