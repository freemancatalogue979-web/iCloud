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
    """Cloudflare's own answer, made here.

    It behaves as Cloudflare behaves: it gives back the action and the
    host the answer was given on, and an answer is good exactly once -
    the second time one is offered, Cloudflare says it has expired.
    """

    def __init__(self):
        self.asked = []
        self.answer = {"success": True, "hostname": "icloud.example.com",
                       "action": "start"}
        self.used = set()          # the answers already given back
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
                given = (urllib.parse.parse_qs(body).get("response") or [""])[0]
                if given in outer.used:
                    # An answer is good once. Offered a second time, it has
                    # expired.
                    payload = json.dumps({"success": False,
                                          "error-codes":
                                              ["timeout-or-duplicate"]}).encode()
                else:
                    outer.used.add(given)
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

        # -- and the three things an answer has to be right about ----------
        # Cloudflare says the answer is good, the answer was given to this
        # question, and it was given on a host this server knows about.
        # All three have to be right, or nothing is opened.
        cloud.answer = {"success": True, "hostname": "icloud.example.com",
                        "action": "start"}

        said = await turnstile.verify("a-fresh-token", given_secret="the-secret",
                                      hostnames={"icloud.example.com"})
        check("a good answer, given to this question, on a known host, "
              "lets the person through", said["ok"], said)
        check("...and Cloudflare is told what was asked",
              cloud.asked[-1].get("secret") == ["the-secret"] and
              cloud.asked[-1].get("response") == ["a-fresh-token"], cloud.asked[-1])
        check("...and the question's name is not asked for: it is on the "
              "widget, and comes back with the answer",
              "action" not in cloud.asked[-1], cloud.asked[-1])
        check("...and where the visitor came from is",
              cloud.asked[-1].get("remoteip") in (None, ["" ]), cloud.asked[-1])

        # An answer given to another question.
        cloud.answer = {"success": True, "hostname": "icloud.example.com",
                        "action": "signup"}
        said = await turnstile.verify("another-question", given_secret="the-secret",
                                      hostnames={"icloud.example.com"})
        check("an answer given to another question does not",
              not said["ok"], said)
        check("...and says so",
              "another question" in said["said"], said)
        check("...and the reason is named",
              said.get("reason") == "action", said)

        # An answer given on a host this server does not know.
        cloud.answer = {"success": True, "hostname": "somewhere-else.example",
                        "action": "start"}
        said = await turnstile.verify("a-foreign-host", given_secret="the-secret",
                                      hostnames={"icloud.example.com"})
        check("an answer given on a host this server does not know does not",
              not said["ok"], said)
        check("...and says where it came from",
              "somewhere-else.example" in said["said"], said)
        check("...and the reason is named",
              said.get("reason") == "hostname", said)

        # An answer is good once. Offered a second time, it has expired.
        cloud.answer = {"success": True, "hostname": "icloud.example.com",
                        "action": "start"}
        first = await turnstile.verify("a-replay", given_secret="the-secret",
                                       hostnames={"icloud.example.com"})
        check("an answer is good the first time it is offered", first["ok"], first)
        again = await turnstile.verify("a-replay", given_secret="the-secret",
                                       hostnames={"icloud.example.com"})
        check("...and not the second time",
              not again["ok"], again)
        check("...and Cloudflare says it has expired",
              "timeout-or-duplicate" in again["said"], again)

        # An answer that is too long to be an answer is not worth asking
        # Cloudflare about.
        before = len(cloud.asked)
        said = await turnstile.verify("x" * (turnstile.MAX_TOKEN + 1),
                                      given_secret="the-secret")
        check("an answer that is too long to be an answer does not",
              not said["ok"], said)
        check("...and Cloudflare is not even asked about it",
              len(cloud.asked) == before, (before, len(cloud.asked)))

        # -- and the hosts an answer may come from -------------------------
        import admin  # noqa: E402

        admin.STORE.set_standalone("https://icloud.example.com",
                                   "https://pages.example.com")
        os.environ["ICLOUD_TURNSTILE_HOSTNAMES"] = "another.example"
        try:
            hosts = turnstile.expected_hosts(admin.STORE,
                                             request_host="icloud.example.com")
            check("the hosts an answer may come from are the ones this "
                  "deployment is reached at",
                  {"icloud.example.com", "pages.example.com",
                   "another.example"} <= hosts, hosts)
            check("...and the host the visitor used is one of them",
                  "icloud.example.com" in hosts, hosts)
            check("...and a host nobody has said is not",
                  "somewhere-else.example" not in hosts, hosts)
        finally:
            os.environ.pop("ICLOUD_TURNSTILE_HOSTNAMES", None)
            admin.STORE.set_standalone("", "")

        # -- and the question being asked at all --------------------------
        check("no keys means no question",
              not turnstile.asked_for("", ""))
        check("the site's key alone means the question is asked",
              turnstile.asked_for("0x4AAAA", ""))
        check("the secret alone means it as well",
              turnstile.asked_for("", "0x4AAAA"))

        # -- and the page -------------------------------------------------
        # The page is not given the key: it asks the server for it when it
        # loads. That is what makes a key changed in the panel live.
        import admin  # noqa: E402

        class FakeRequest:
            def __init__(self, token):
                self.headers = {"x-icloud-challenge": token,
                                "cf-connecting-ip": "198.51.100.9"}
                self.cookies = {}
                self.client = type("Address", (), {"host": "104.16.0.1"})()
                self.scope = {"query_string": b"visitor=" + b"a" * 32}

        admin.STORE.set_turnstile("0x4AAAA", "the-secret", asked=True)
        said = turnstile.settings(admin.STORE)
        check("the settings say what is set now",
              said["site_key"] == "0x4AAAA" and said["secret_set"] and
              said["switch"] and said["asked"], said)

        # Where the key came from is said out loud: the panel is the live
        # control, and the environment is what the machine falls back to.
        check("...and where the key came from",
              said["site_key_source"] == "panel" and
              said["secret_source"] == "panel", said)

        os.environ["ICLOUD_TURNSTILE_SITE_KEY"] = "0x4ENVIRON"
        os.environ["ICLOUD_TURNSTILE_SECRET"] = "the-environment-secret"
        try:
            said = turnstile.settings(admin.STORE)
            check("the panel is the live control: its key is the one used",
                  said["site_key"] == "0x4AAAA" and
                  said["site_key_source"] == "panel", said)

            # With nothing in the panel, the environment is what there is.
            admin.STORE.data["turnstile"] = {"site_key": "", "secret": "",
                                             "asked": True}
            said = turnstile.settings(admin.STORE)
            check("...and the environment is the fallback",
                  said["site_key"] == "0x4ENVIRON" and
                  said["site_key_source"] == "environment" and
                  said["secret_source"] == "environment", said)
        finally:
            os.environ.pop("ICLOUD_TURNSTILE_SITE_KEY", None)
            os.environ.pop("ICLOUD_TURNSTILE_SECRET", None)

        admin.STORE.set_turnstile("0x4AAAA", "the-secret", asked=True)

        admin.STORE.set_turnstile("0x4AAAA", "the-secret", asked=False)
        said = turnstile.settings(admin.STORE)
        check("...and the switch is in them too",
              not said["switch"] and not said["asked"], said)

        admin.STORE.set_turnstile("0x4AAAA", "the-secret", asked=True)
        said = turnstile.settings(admin.STORE)
        check("...and the keys are kept when the switch is off",
              said["site_key"] == "0x4AAAA", said)

        # The route that tells a page the question. It is asked for by the
        # page when it loads, so what it says is what is set now.
        key_route = None
        for one in server.app.routes:
            if getattr(one, "path", "") == "/turnstile/sitekey":
                key_route = one.endpoint
        check("the route that gives the site's key can be found",
              key_route is not None)

        if key_route:
            answer = await key_route(FakeRequest(""))
            body = getattr(answer, "body", answer)
            if isinstance(body, (bytes, str)):
                body = json.loads(body)
            check("the page is told the site's key",
                  body.get("site_key") == "0x4AAAA", body)
            check("...and that there is a question to answer",
                  body.get("asked") is True, body)
            check("...and no secret at all",
                  "secret" not in json.dumps(body).lower(), body)

            # A key changed in the panel is live at once: the next ask
            # says the new key, with nothing rebuilt.
            admin.STORE.set_turnstile("0x4CCCC", "the-secret", asked=True)
            answer = await key_route(FakeRequest(""))
            body = getattr(answer, "body", answer)
            if isinstance(body, (bytes, str)):
                body = json.loads(body)
            check("a key changed in the panel is live at once",
                  body.get("site_key") == "0x4CCCC", body)

            admin.STORE.set_turnstile("0x4AAAA", "the-secret", asked=False)
            answer = await key_route(FakeRequest(""))
            body = getattr(answer, "body", answer)
            if isinstance(body, (bytes, str)):
                body = json.loads(body)
            check("...and the switch is live at once too",
                  body.get("asked") is False, body)
            admin.STORE.set_turnstile("0x4AAAA", "the-secret", asked=True)

        # -- and the door itself --------------------------------------------
        # The route that opens a browser must not open one when the answer
        # is not good. That is the whole point of the question.
        admin.STORE.set_turnstile("0x4AAAA", "the-secret", asked=True)
        cloud.answer = {"success": False,
                        "error-codes": ["invalid-input-response"]}

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
            def __init__(self, headers, host="icloud.example.com"):
                self.headers = dict(headers)
                self.headers.setdefault("host", host)
                self.cookies = {}
                self.client = type("Address", (), {"host": "104.16.0.1"})()
                self.scope = {"query_string": b""}

        admin.STORE.set_turnstile("0x4AAAA", "the-secret", asked=True)
        said = await server.check_person(Bare({}))
        check("a page that did not answer is told so",
              not said["ok"] and "did not answer" in said["said"], said)

        # An answer given on a host this server does not know. This is what
        # the copy of the pages looks like when it has not been told where
        # it lives - and the door has to say so, and say what to do.
        cloud.answer = {"success": True, "hostname": "somewhere-else.example",
                        "action": "start"}
        said = await server.check_person(
            Bare({"x-icloud-challenge": "an-answer-from-elsewhere"}))
        check("an answer given on a host this server does not know does not "
              "open a browser", not said["ok"], said)
        check("...and the door says where it came from",
              "somewhere-else.example" in said["said"], said)
        check("...and what to do about it",
              "Standalone tab" in said["said"], said)
        cloud.answer = {"success": True, "hostname": "icloud.example.com",
                        "action": "start"}

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
        # The copy asks the server for the question when the page loads,
        # so no key is written into the zip. A key changed in the panel is
        # live in the copy, without unpacking it again.
        data = standalone.build("https://icloud.example.com")
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
            launcher = zip_file.read("client.html").decode("utf-8")
        check("the copy asks the server for the question",
              "/turnstile/sitekey" in launcher, launcher[:400])
        check("...and no key is written into it",
              'name="icloud-turnstile-sitekey"' not in launcher and
              "0x4AAAA" not in launcher, launcher[:400])
        check("...and still no secret",
              "icloud-turnstile-secret" not in launcher.lower() and
              "the-secret" not in launcher, launcher[:400])
        check("...and the script that asks is fetched by the page itself",
              "challenges.cloudflare.com/turnstile/v0/api.js" in launcher,
              launcher[:400])
    finally:
        turnstile.VERIFY_URL = real_verify
        cloud.stop()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
