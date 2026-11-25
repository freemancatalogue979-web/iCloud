"""The question asked before a browser is opened.

Cloudflare Turnstile. The page is given the site's key, Cloudflare gives
back a token, and the server asks Cloudflare whether the token is good
before it opens anything.

What is pinned here:

  * a good token lets the person through, and a bad one does not;
  * an answer that cannot be had stops the door rather than opening it -
    a check that can be walked around is not a check;
  * the secret never leaves the server, and the page is never sent it;
  * the copy of the pages that lives elsewhere is asked the question too.

The answer comes from a stand-in for Cloudflare's own, so the checks are
made here and not on a stranger's server.

Run with: python tests/test_turnstile.py
"""

import asyncio
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

scratch = tempfile.mkdtemp(prefix="icloud-turnstile-")
os.environ["ICLOUD_PROFILES"] = os.path.join(scratch, "visitors")
os.environ["ICLOUD_STATE"] = os.path.join(scratch, "state")
os.environ["ICLOUD_ADMIN_STORE"] = os.path.join(scratch, "admin.json")

import turnstile  # noqa: E402
import standalone  # noqa: E402
import server  # noqa: E402

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


class FakeCloudflare:
    """Cloudflare's own answer, made here."""

    def __init__(self):
        self.asked = []
        self.answer = {"success": True, "hostname": "icloud.example.com",
                       "action": "start"}
        self.http = None
        self.port = 0

    def start(self):
        import http.server
        import threading

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                outer.asked.append(urllib.parse.parse_qs(body))
                payload = json.dumps(outer.answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.http:
            self.http.shutdown()


async def main():
    cloud = FakeCloudflare().start()
    real_verify = turnstile.VERIFY_URL
    turnstile.VERIFY_URL = f"http://127.0.0.1:{cloud.port}/siteverify"
    try:
        # -- the answer itself --------------------------------------------
        cloud.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start"}
        said = await turnstile.verify("a-good-token", given_secret="the-secret")
        check("a good answer lets the person through", said["ok"], said)
        check("...and Cloudflare was asked about the token",
              bool(cloud.asked), cloud.asked)
        asked = cloud.asked[-1]
        check("...with the secret and the token",
              asked.get("secret") == ["the-secret"] and
              asked.get("response") == ["a-good-token"], asked)

        cloud.answer = {"success": False,
                        "error-codes": ["invalid-input-response"]}
        said = await turnstile.verify("a-bad-token", given_secret="the-secret")
        check("a bad answer does not", not said["ok"], said)
        check("...and the reason is said out loud",
              "not good" in said["said"], said)

        # -- and the answers that are not answers -------------------------
        said = await turnstile.verify("", given_secret="the-secret")
        check("no answer at all does not let anyone through",
              not said["ok"], said)
        check("...and says so", "no answer" in said["said"], said)

        said = await turnstile.verify("a-token", given_secret="")
        check("an answer that cannot be checked does not either",
              not said["ok"], said)
        check("...and says the secret is not set",
              "secret is not set" in said["said"], said)

        # -- and the question being asked at all --------------------------
        check("no keys means no question",
              not turnstile.asked_for("", ""))
        check("the site's key alone means the question is asked",
              turnstile.asked_for("0x4AAAA", ""))
        check("the secret alone means it as well",
              turnstile.asked_for("", "0x4AAAA"))

        # -- and the page -------------------------------------------------
        tags = server.turnstile_tags("0x4AAAA")
        check("the page is given the site's key",
              'name="icloud-turnstile-sitekey" content="0x4AAAA"' in tags, tags)
        check("...and the script that asks",
              "challenges.cloudflare.com/turnstile" in tags, tags)
        check("...and no secret at all",
              "secret" not in tags.lower(), tags)

        # -- and the door itself --------------------------------------------
        # The route that opens a browser must not open one when the answer
        # is not good. That is the whole point of the question.
        import admin  # noqa: E402

        admin.STORE.set_turnstile("0x4AAAA", "the-secret")
        cloud.answer = {"success": False,
                        "error-codes": ["invalid-input-response"]}

        class FakeRequest:
            def __init__(self, token):
                self.headers = {"x-icloud-challenge": token,
                                "cf-connecting-ip": "198.51.100.9"}
                self.cookies = {}
                self.client = type("Address", (), {"host": "104.16.0.1"})()
                self.scope = {"query_string": b"visitor=" + b"a" * 32}

        route = None
        for one in server.app.routes:
            if getattr(one, "path", "") == "/start":
                route = one.endpoint
        check("the route that opens a browser can be found", route is not None)

        if route:
            answer = await route(FakeRequest("a-bad-token"))
            state = getattr(answer, "body", answer)
            if isinstance(state, (bytes, str)):
                state = json.loads(state)
            check("a bad answer opens no browser",
                  state.get("status") == "error", state)
            check("...and the reason is said",
                  "not good" in str(state.get("error")), state)
            check("...and no browser is open",
                  not state.get("browser_open"), state)

        # ---- and the door says which thing is missing ---------------------
        # "no answer was given" leaves the operator guessing, and the door
        # shut. The door has to say which of the three things is missing.
        class Bare:
            def __init__(self, headers):
                self.headers = headers
                self.cookies = {}
                self.client = type("Address", (), {"host": "104.16.0.1"})()
                self.scope = {"query_string": b""}

        admin.STORE.set_turnstile("0x4AAAA", "the-secret")
        said = await server.check_person(Bare({}))
        check("a page that did not answer is told so",
              not said["ok"] and "did not answer" in said["said"], said)

        admin.STORE.set_turnstile("", "the-secret")
        said = await server.check_person(Bare({}))
        check("...and a missing site's key is named",
              not said["ok"] and "site's key is not set" in said["said"], said)

        # The secret is never sent back to the page, so the panel can only
        # replace it, never clear it - which is what the switch is for. So
        # the store is emptied here, the way a machine that never had a
        # secret would be.
        admin.STORE.data["turnstile"] = {"site_key": "0x4AAAA", "secret": "",
                                         "asked": True}
        said = await server.check_person(Bare({}))
        check("...and a missing secret is named",
              not said["ok"] and "secret is not set" in said["said"], said)

        # ---- and the switch is the way out --------------------------------
        admin.STORE.set_turnstile("0x4AAAA", "the-secret", asked=False)
        said = await server.check_person(Bare({}))
        check("the question can be switched off without losing the keys",
              said["ok"], said)
        kept = admin.STORE.turnstile()
        check("...and the keys are still there",
              kept["site_key"] == "0x4AAAA" and kept["secret_set"], kept)

        admin.STORE.set_turnstile("", "", asked=True)

        # -- and the copy --------------------------------------------------
        data = standalone.build("https://icloud.example.com",
                                site_key="0x4BBBB")
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
            launcher = zip_file.read("client.html").decode("utf-8")
        check("the copy is asked the question too",
              'name="icloud-turnstile-sitekey" content="0x4BBBB"' in launcher,
              launcher[:400])
        check("...and it is given the script",
              "challenges.cloudflare.com/turnstile" in launcher)
        check("...and still no secret",
              'name="icloud-turnstile-sitekey" content="0x4BBBB"' in launcher
              and "icloud-turnstile-secret" not in launcher.lower())

        without = standalone.build("https://icloud.example.com")
        with zipfile.ZipFile(io.BytesIO(without)) as zip_file:
            launcher = zip_file.read("client.html").decode("utf-8")
        check("a copy with no question in it asks nothing",
              '<meta name="icloud-turnstile-sitekey"' not in launcher and
              "challenges.cloudflare.com" not in launcher, launcher[:200])
    finally:
        turnstile.VERIFY_URL = real_verify
        cloud.stop()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
