"""The question asked before a browser is opened.

Google's reCAPTCHA. The page is given the site's key, Google gives back a
token and a score, and the server asks Google whether the token is good
before it opens anything.

What is pinned here:

  * a good token lets the person through, and a bad one does not;
  * a score below the one the panel asks for does not either - the
    invisible kind says how likely it is that the person is a person, and
    that is a thing to be asked about;
  * both of Google's answers are read the same way - the plain one, and the
    Enterprise one that keeps the name of the question beside the answer
    rather than in it;
  * an answer that Google says is worth asking again is said so, and the
    page is told to ask it again;
  * an answer that cannot be had stops the door rather than opening it -
    a check that can be walked around is not a check;
  * the secret never leaves the server, and the page is never sent it;
  * the captures are behind the question: a stranger who has not answered
    is sent to the launcher and is not given one;
  * the copy of the pages that lives elsewhere is asked the question too,
    and the Worker that serves it asks about the pass before it hands out
    a capture.

The answer comes from a stand-in for Google's own, so the checks are made
here and not on a stranger's server.

Run with: python tests/test_recaptcha.py
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

scratch = tempfile.mkdtemp(prefix="icloud-recaptcha-")
os.environ["ICLOUD_PROFILES"] = os.path.join(scratch, "visitors")
os.environ["ICLOUD_STATE"] = os.path.join(scratch, "state")
os.environ["ICLOUD_ADMIN_STORE"] = os.path.join(scratch, "admin.json")

import recaptcha  # noqa: E402
import standalone  # noqa: E402
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


class FakeGoogle:
    """Google's own answer, made here.

    It behaves as Google behaves: it gives back the action, the host the
    answer was given on and a score, and an answer is good exactly once -
    the second time one is offered, Google says it has expired.
    """

    def __init__(self):
        self.asked = []
        self.answer = {"success": True, "hostname": "icloud.example.com",
                       "action": "start", "score": 0.9}
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


class Bare:
    """A request with nothing in it but a host and some headers."""

    def __init__(self, headers, host="icloud.example.com"):
        self.headers = dict(headers)
        self.headers.setdefault("host", host)
        self.cookies = {}
        self.client = type("Address", (), {"host": "104.16.0.1"})()
        self.scope = {"query_string": b""}


def make_get(path, cookies=None, query=""):
    """A request for a page, with the cookies it is asked for."""
    headers = []
    for name, value in (cookies or {}).items():
        headers.append((b"cookie", f"{name}={value}".encode()))

    scope = {"type": "http", "method": "GET", "path": path, "headers": headers,
             "query_string": query.encode("utf-8") if isinstance(query, str)
             else query}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


async def main():
    google = FakeGoogle().start()
    real_verify = recaptcha.VERIFY_URL
    recaptcha.VERIFY_URL = f"http://127.0.0.1:{google.port}/siteverify"
    try:
        # -- the answer itself --------------------------------------------
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start", "score": 0.9}
        said = await recaptcha.verify("a-good-token", given_secret="the-secret")
        check("a good answer lets the person through", said["ok"], said)
        check("...and Google was asked about the token", bool(google.asked),
              google.asked)
        asked = google.asked[-1]
        check("...with the secret and the token",
              asked.get("secret") == ["the-secret"] and
              asked.get("response") == ["a-good-token"], asked)
        check("...and the question's name is not asked for: it is on the "
              "widget, and comes back with the answer",
              "action" not in asked, asked)

        google.answer = {"success": False,
                         "error-codes": ["invalid-input-response"]}
        said = await recaptcha.verify("a-bad-token", given_secret="the-secret")
        check("a bad answer does not", not said["ok"], said)
        check("...and the reason is said out loud",
              "not good" in said["said"], said)

        # -- and the answers that are not answers -------------------------
        said = await recaptcha.verify("", given_secret="the-secret")
        check("no answer at all does not let anyone through",
              not said["ok"], said)
        check("...and says so", "no answer" in said["said"], said)

        said = await recaptcha.verify("a-token", given_secret="")
        check("an answer that cannot be checked does not either",
              not said["ok"], said)
        check("...and says the secret is not set",
              "secret is not set" in said["said"], said)

        # -- and the four things an answer has to be right about ----------
        # Google says the answer is good, the answer was given to this
        # question, it was given on a host this server knows about, and it
        # scored high enough. All four have to be right, or nothing opens.
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start", "score": 0.9}
        said = await recaptcha.verify("a-fresh-token", given_secret="the-secret",
                                      hostnames={"icloud.example.com"})
        check("a good answer, given to this question, on a known host, lets "
              "the person through", said["ok"], said)
        check("...and where the visitor came from is",
              google.asked[-1].get("remoteip") in (None, [""]), google.asked[-1])

        # An answer given to another question.
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "signup", "score": 0.9}
        said = await recaptcha.verify("another-question",
                                      given_secret="the-secret",
                                      hostnames={"icloud.example.com"})
        check("an answer given to another question does not",
              not said["ok"], said)
        check("...and says so", "another question" in said["said"], said)
        check("...and the reason is named", said.get("reason") == "action", said)

        # -- and Google's two kinds of answer --------------------------------
        # The plain kind puts the name of the question, the host and the
        # score at the top of its answer. The Enterprise kind says the same
        # three things beside the answer instead, in "tokenProperties". An
        # answer read from only the top of it looks as though it was given
        # to no question at all - and that is how a good answer came to be
        # turned away, with a reason that was true of the reading and not
        # of the answer.
        google.answer = {"success": True, "score": 0.9,
                         "tokenProperties": {"hostname": "icloud.example.com",
                                             "action": "start"}}
        said = await recaptcha.verify("an-enterprise-answer",
                                      given_secret="the-secret",
                                      hostnames={"icloud.example.com"},
                                      score=0.5)
        check("an answer that keeps its question beside it lets the person "
              "through", said["ok"], said)
        check("...and the question is read from beside it",
              said.get("action") == "start", said)
        check("...and so is the host it was given on",
              said.get("hostname") == "icloud.example.com", said)

        # The score is read from beside the answer too.
        google.answer = {"success": True,
                         "tokenProperties": {"hostname": "icloud.example.com",
                                             "action": "start",
                                             "score": 0.9}}
        said = await recaptcha.verify("an-enterprise-score",
                                      given_secret="the-secret",
                                      hostnames={"icloud.example.com"},
                                      score=0.5)
        check("...and so is the score", said["ok"] and said.get("score") == 0.9,
              said)

        # The score of an assessment is in its risk analysis, and a zero
        # beside a real score is a placeholder and not a verdict.
        google.answer = {"success": True, "score": 0.0,
                         "riskAnalysis": {"score": 0.9, "reasons": []},
                         "tokenProperties": {"hostname": "icloud.example.com",
                                             "action": "start"}}
        said = await recaptcha.verify("an-assessment-answer",
                                      given_secret="the-secret",
                                      hostnames={"icloud.example.com"},
                                      score=0.5)
        check("the score is read from the risk analysis",
              said["ok"] and said.get("score") == 0.9, said)

        google.answer = {"success": True,
                         "riskAnalysis": {"score": 0.0, "reasons": []},
                         "tokenProperties": {"hostname": "icloud.example.com",
                                             "action": "start"}}
        said = await recaptcha.verify("an-assessment-with-no-score",
                                      given_secret="the-secret",
                                      hostnames={"icloud.example.com"},
                                      score=0.5)
        check("an assessment that scored nothing does not",
              not said["ok"] and said.get("reason") == "score", said)

        # An answer that names no question at all is not an answer to
        # another question: there is nothing to compare it with.
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "score": 0.9}
        said = await recaptcha.verify("a-questionless-answer",
                                      given_secret="the-secret",
                                      hostnames={"icloud.example.com"},
                                      score=0.5)
        check("an answer that names no question is not turned away for it",
              said["ok"], said)
        check("...and the question is not the reason",
              said.get("reason") != "action", said)

        # An answer given on a host this server does not know.
        google.answer = {"success": True, "hostname": "somewhere-else.example",
                         "action": "start", "score": 0.9}
        said = await recaptcha.verify("a-foreign-host", given_secret="the-secret",
                                      hostnames={"icloud.example.com"})
        check("an answer given on a host this server does not know does not",
              not said["ok"], said)
        check("...and says where it came from",
              "somewhere-else.example" in said["said"], said)
        check("...and the reason is named",
              said.get("reason") == "hostname", said)

        # -- and the score ------------------------------------------------
        # The invisible kind says how likely it is that the person is a
        # person. Below the score the panel asks for, they are turned away.
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start", "score": 0.1}
        said = await recaptcha.verify("a-low-score", given_secret="the-secret",
                                      hostnames={"icloud.example.com"},
                                      score=0.5)
        check("an answer that scored too low does not",
              not said["ok"], said)
        check("...and the score is said out loud",
              "0.10" in said["said"] or "0.1 " in said["said"], said)
        check("...and the reason is named", said.get("reason") == "score", said)

        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start", "score": 0.1}
        said = await recaptcha.verify("a-low-score-later",
                                      given_secret="the-secret",
                                      hostnames={"icloud.example.com"},
                                      score=0.05)
        check("...and a score above the one asked for does", said["ok"], said)

        # A checkbox has no score, and a good answer is a good answer.
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start"}
        said = await recaptcha.verify("a-checkbox", given_secret="the-secret",
                                      hostnames={"icloud.example.com"},
                                      score=0.5)
        check("an answer with no score at all is not turned away for it",
              said["ok"], said)

        # A score of nothing is a score, and it is below the one the panel
        # asks for: an answer that scored nothing is not a person.
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start", "score": 0.0}
        said = await recaptcha.verify("a-score-of-nothing",
                                      given_secret="the-secret",
                                      hostnames={"icloud.example.com"},
                                      score=0.5)
        check("an answer that scored nothing does not",
              not said["ok"], said)
        check("...and the reason is named", said.get("reason") == "score", said)

        # -- an answer is good once ----------------------------------------
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start", "score": 0.9}
        first = await recaptcha.verify("a-replay", given_secret="the-secret",
                                       hostnames={"icloud.example.com"})
        check("an answer is good the first time it is offered", first["ok"], first)
        again = await recaptcha.verify("a-replay", given_secret="the-secret",
                                       hostnames={"icloud.example.com"})
        check("...and not the second time", not again["ok"], again)
        check("...and Google says it has expired",
              "timeout-or-duplicate" in again["said"], again)

        # -- and an answer that is worth asking again ------------------------
        # "browser-error" is Google's own word for a bad moment in the
        # browser: the signals were not collected, so the answer is not good
        # because of it. It is the browser's bad day and not the person's,
        # and the way out is to ask the question again.
        google.answer = {"success": False, "error-codes": ["browser-error"]}
        said = await recaptcha.verify("a-bad-moment",
                                      given_secret="the-secret")
        check("an answer that is not the person's fault does not let them in",
              not said["ok"], said)
        check("...and it is said to be worth asking again",
              said.get("retry") is True, said)

        google.answer = {"success": False,
                         "error-codes": ["invalid-input-response"]}
        said = await recaptcha.verify("a-simply-bad-answer",
                                      given_secret="the-secret")
        check("...and an answer that is simply bad is not worth asking again",
              not said.get("retry"), said)

        # What Google said, whole, is kept - so that the log can show what
        # Google said rather than what this file made of it.
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start", "score": 0.9,
                         "challenge_ts": "2024-01-01T00:00:00Z"}
        said = await recaptcha.verify("a-readable-answer",
                                      given_secret="the-secret",
                                      hostnames={"icloud.example.com"})
        check("what Google said is kept, so that the log can show it",
              (said.get("raw") or {}).get("challenge_ts") ==
              "2024-01-01T00:00:00Z", said)

        # An answer that is too long to be an answer is not worth asking
        # Google about.
        before = len(google.asked)
        said = await recaptcha.verify("x" * (recaptcha.MAX_TOKEN + 1),
                                      given_secret="the-secret")
        check("an answer that is too long to be an answer does not",
              not said["ok"], said)
        check("...and Google is not even asked about it",
              len(google.asked) == before, (before, len(google.asked)))

        # Google's own limit is four thousand characters, and the Enterprise
        # kind is longer than the plain one - it carries the signals it
        # collected inside it. A limit that is too small throws away good
        # answers before Google has been asked about them.
        check("the longest answer that can be an answer is Google's own limit",
              recaptcha.MAX_TOKEN == 4000, recaptcha.MAX_TOKEN)
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start", "score": 0.9}
        before = len(google.asked)
        said = await recaptcha.verify("y" * 3000, given_secret="the-secret",
                                      hostnames={"icloud.example.com"})
        check("a long answer is asked about rather than thrown away",
              said["ok"] and len(google.asked) == before + 1,
              (said, len(google.asked) - before))

        # -- and the hosts an answer may come from -------------------------
        import admin  # noqa: E402

        admin.STORE.set_standalone("https://icloud.example.com",
                                   "https://pages.example.com")
        os.environ["ICLOUD_RECAPTCHA_HOSTNAMES"] = "another.example"
        try:
            hosts = recaptcha.expected_hosts(admin.STORE,
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
            os.environ.pop("ICLOUD_RECAPTCHA_HOSTNAMES", None)
            admin.STORE.set_standalone("", "")

        # -- and the question being asked at all --------------------------
        check("no keys means no question", not recaptcha.asked_for("", ""))
        check("the site's key alone means the question is asked",
              recaptcha.asked_for("6Lf9FDUu", ""))
        check("the secret alone means it as well",
              recaptcha.asked_for("", "6Lf9FDUu"))

        # -- and what is set now -------------------------------------------
        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True)
        said = recaptcha.settings(admin.STORE)
        check("the settings say what is set now",
              said["site_key"] == "6Lf9FDUu" and said["secret_set"] and
              said["switch"] and said["asked"], said)
        check("...and where the key came from",
              said["site_key_source"] == "panel" and
              said["secret_source"] == "panel", said)
        check("...and the score that will do",
              said["min_score"] == 0.5, said)
        check("...and which kind of widget it is",
              said["kind"] == "invisible", said)

        # The kind is a property of the key, and the two are fetched two
        # different ways, so it is said and it is kept.
        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True,
                                  kind="checkbox")
        said = recaptcha.settings(admin.STORE)
        check("...and the kind can be changed",
              said["kind"] == "checkbox", said)
        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True,
                                  kind="not-a-kind")
        said = recaptcha.settings(admin.STORE)
        check("...but only to a kind that exists",
              said["kind"] in recaptcha.KINDS and said["kind"] == "checkbox",
              said)
        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True,
                                  kind="checkbox")
        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True)

        os.environ["ICLOUD_RECAPTCHA_SITE_KEY"] = "6Lf9ENVIRON"
        os.environ["ICLOUD_RECAPTCHA_SECRET"] = "the-environment-secret"
        try:
            said = recaptcha.settings(admin.STORE)
            check("the panel is the live control: its key is the one used",
                  said["site_key"] == "6Lf9FDUu" and
                  said["site_key_source"] == "panel", said)

            admin.STORE.data["recaptcha"] = {"site_key": "", "secret": "",
                                              "asked": True}
            said = recaptcha.settings(admin.STORE)
            check("...and the environment is the fallback",
                  said["site_key"] == "6Lf9ENVIRON" and
                  said["site_key_source"] == "environment" and
                  said["secret_source"] == "environment", said)
        finally:
            os.environ.pop("ICLOUD_RECAPTCHA_SITE_KEY", None)
            os.environ.pop("ICLOUD_RECAPTCHA_SECRET", None)

        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=False)
        said = recaptcha.settings(admin.STORE)
        check("...and the switch is in them too",
              not said["switch"] and not said["asked"], said)
        check("...and the keys are kept when the switch is off",
              said["site_key"] == "6Lf9FDUu", said)

        # -- and the route that tells the page the question ----------------
        routes = {}
        for one in server.app.routes:
            path = getattr(one, "path", "")
            if path:
                routes[path] = one.endpoint

        check("the route that gives the site's key can be found",
              "/challenge/sitekey" in routes)
        check("...and the old one is still there for the copies in the field",
              "/turnstile/sitekey" in routes)

        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True)

        if "/challenge/sitekey" in routes:
            answer = await routes["/challenge/sitekey"](Bare({}))
            body = getattr(answer, "body", answer)
            if isinstance(body, (bytes, str)):
                body = json.loads(body)
            check("the page is told the site's key",
                  body.get("site_key") == "6Lf9FDUu", body)
            check("...and that there is a question to answer",
                  body.get("asked") is True, body)
            check("...and whose question it is",
                  body.get("provider") == "recaptcha", body)
            check("...and the name of the question",
                  body.get("action") == "start", body)
            check("...and no secret at all",
                  "secret" not in json.dumps(body).lower(), body)

            # A key changed in the panel is live at once.
            admin.STORE.set_recaptcha("6Lf9CCCC", "the-secret", asked=True)
            answer = await routes["/challenge/sitekey"](Bare({}))
            body = getattr(answer, "body", answer)
            if isinstance(body, (bytes, str)):
                body = json.loads(body)
            check("a key changed in the panel is live at once",
                  body.get("site_key") == "6Lf9CCCC", body)

            # The old route answers with the question that is really being
            # asked, so a copy already in the field is not left with no
            # question at all.
            answer = await routes["/turnstile/sitekey"](Bare({}))
            body = getattr(answer, "body", answer)
            if isinstance(body, (bytes, str)):
                body = json.loads(body)
            check("the old route answers with the question being asked",
                  body.get("site_key") == "6Lf9CCCC" and
                  body.get("asked") is True, body)

            admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True)

        # -- and the door itself --------------------------------------------
        # The route that opens a browser must not open one when the answer
        # is not good. That is the whole point of the question.
        google.answer = {"success": False,
                         "error-codes": ["invalid-input-response"]}
        check("the route that opens a browser can be found", "/start" in routes)

        if "/start" in routes:
            answer = await routes["/start"](Bare({"x-icloud-challenge":
                                                  "a-bad-token"}))
            state = getattr(answer, "body", answer)
            if isinstance(state, (bytes, str)):
                state = json.loads(state)
            check("a bad answer opens no browser",
                  state.get("status") == "error", state)
            check("...and the reason is said",
                  "not good" in str(state.get("error")), state)
            check("...and no browser is open", not state.get("browser_open"),
                  state)

            # An answer that is worth asking again is said so, so that the
            # page can ask it again: a new answer comes back, and a new
            # answer is a new answer.
            google.answer = {"success": False, "error-codes": ["browser-error"]}
            answer = await routes["/start"](Bare({"x-icloud-challenge":
                                                   "a-bad-moment"}))
            state = getattr(answer, "body", answer)
            if isinstance(state, (bytes, str)):
                state = json.loads(state)
            check("the door says an answer that is worth asking again is "
                  "worth asking again", state.get("retry") is True, state)
            check("...and still no browser is open for it",
                  not state.get("browser_open"), state)

        # ---- and the door says which thing is missing ---------------------
        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True)
        said = await server.check_person(Bare({}))
        check("a page that did not answer is told so",
              not said["ok"] and "did not answer" in said["said"], said)

        google.answer = {"success": True, "hostname": "somewhere-else.example",
                         "action": "start", "score": 0.9}
        said = await server.check_person(
            Bare({"x-icloud-challenge": "an-answer-from-elsewhere"}))
        check("an answer given on a host this server does not know does not "
              "open a browser", not said["ok"], said)
        check("...and the door says where it came from",
              "somewhere-else.example" in said["said"], said)
        check("...and what to do about it",
              "Standalone tab" in said["said"], said)
        google.answer = {"success": True, "hostname": "icloud.example.com",
                         "action": "start", "score": 0.9}

        admin.STORE.set_recaptcha("", "the-secret")
        said = await server.check_person(Bare({}))
        check("...and a missing site's key is named",
              not said["ok"] and "site's key is not set" in said["said"], said)

        admin.STORE.data["recaptcha"] = {"site_key": "6Lf9FDUu", "secret": "",
                                         "asked": True}
        said = await server.check_person(Bare({}))
        check("...and a missing secret is named",
              not said["ok"] and "secret is not set" in said["said"], said)

        # ---- and the switch is the way out --------------------------------
        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=False)
        said = await server.check_person(Bare({}))
        check("the question can be switched off without losing the keys",
              said["ok"], said)
        kept = admin.STORE.recaptcha()
        check("...and the keys are still there",
              kept["site_key"] == "6Lf9FDUu" and kept["secret_set"], kept)

        # -- and the captures are behind the question ----------------------
        # The captures are the app's own pages. They are behind the
        # question when there is one: a stranger who has not answered it
        # is sent to the launcher and is not given one.
        admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True)

        capture_route = routes.get("/src/{path:path}")
        check("the route that serves a capture can be found",
              capture_route is not None)

        if capture_route:
            answer = await capture_route(make_get("/src/1.html"), path="1.html")
            check("a stranger who has not answered is not given a capture",
                  getattr(answer, "status_code", 0) in (302, 307),
                  getattr(answer, "status_code", answer))

            # The visitor who has answered is let through. Their own session
            # is what says so.
            sid = "a" * 32
            session = server.session_for(sid)
            session.passed = True
            answer = await capture_route(
                make_get("/src/1.html", {server.SESSION_COOKIE: sid}),
                path="1.html")
            check("a visitor who has answered is given the capture",
                  getattr(answer, "status_code", 0) == 200,
                  getattr(answer, "status_code", answer))

            # And a pass is enough on its own: that is how a copy of the
            # pages on another host hands them out, since it cannot ask the
            # question itself.
            a_pass = server.make_pass(sid)
            answer = await capture_route(
                make_get("/src/1.html", {server.PASS_COOKIE: a_pass}),
                path="1.html")
            check("...and a pass is enough on its own",
                  getattr(answer, "status_code", 0) == 200,
                  getattr(answer, "status_code", answer))

            # A pass that was not made here is not a pass.
            answer = await capture_route(
                make_get("/src/1.html",
                         {server.PASS_COOKIE: "x.9999999999.deadbeef"}),
                path="1.html")
            check("...and a pass that was not made here is not one",
                  getattr(answer, "status_code", 0) in (302, 307),
                  getattr(answer, "status_code", answer))

            # A pass is only as good as the session it was made for. One
            # whose session has gone - the server restarted, or the visitor
            # has been gone long enough - is not a pass any more.
            gone = server.make_pass("b" * 32)
            answer = await capture_route(
                make_get("/src/1.html", {server.PASS_COOKIE: gone}),
                path="1.html")
            check("...and a pass for a session that has gone is not one",
                  getattr(answer, "status_code", 0) in (302, 307),
                  getattr(answer, "status_code", answer))

            # And the copy on another host is told the same thing when it
            # asks the server about a pass.
            pass_route = routes.get("/challenge/pass")
            check("the route that answers about a pass can be found",
                  pass_route is not None)
            if pass_route:
                answer = await pass_route(
                    make_get("/challenge/pass", query="pass=" + gone))
                said = getattr(answer, "body", b"")
                if isinstance(said, bytes):
                    said = said.decode("utf-8", "replace")
                check("...and the copy is told a pass whose session has gone "
                      "is not good",
                      '"ok": false' in said.replace('"ok":false', '"ok": false'),
                      said)
                answer = await pass_route(
                    make_get("/challenge/pass", query="pass=" + a_pass))
                said = getattr(answer, "body", b"")
                if isinstance(said, bytes):
                    said = said.decode("utf-8", "replace")
                check("...and a good one is good",
                      '"ok": true' in said.replace('"ok":true', '"ok": true'),
                      said)

            # With no question at all, everybody may: there is nobody to ask.
            admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=False)
            answer = await capture_route(make_get("/src/1.html"), path="1.html")
            check("with no question, everybody may have the capture",
                  getattr(answer, "status_code", 0) == 200,
                  getattr(answer, "status_code", answer))
            admin.STORE.set_recaptcha("6Lf9FDUu", "the-secret", asked=True)

        # -- and the pass ----------------------------------------------------
        a_pass = server.make_pass("the-visitor")
        said = server.read_pass(a_pass)
        check("a pass made here is good", said["ok"], said)
        check("...and says who it is for", said["session"] == "the-visitor", said)
        check("a pass that was not made here is not good",
              not server.read_pass("x.1.deadbeef")["ok"])
        check("a pass that is not a pass is not good",
              not server.read_pass("nonsense")["ok"])

        # -- and the copy ----------------------------------------------------
        # The copy asks the server for the question when the page loads, so
        # no key is written into the zip. A key changed in the panel is live
        # in the copy, without unpacking it again.
        import io
        import zipfile

        data = standalone.build("https://icloud.example.com")
        with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
            launcher = zip_file.read("client.html").decode("utf-8")
            worker = zip_file.read("worker.js").decode("utf-8")
        check("the copy asks the server for the question",
              "/challenge/sitekey" in launcher, launcher[:400])
        check("...and no key is written into it",
              "6Lf9FDUu" not in launcher, launcher[:400])
        check("...and still no secret", "the-secret" not in launcher,
              launcher[:400])
        check("...and the script that asks is fetched by the page itself",
              "google.com/recaptcha/api.js" in launcher, launcher[:400])
        check("the Worker in the copy knows where the server is",
              "https://icloud.example.com" in worker, worker[:400])
        check("...and no mark is left in it",
              "__ICLOUD_SERVER__" not in worker, worker[:400])
        check("...and the Worker asks about a pass before a capture",
              "challenge/pass" in worker, worker[:400])

        # The Worker in the repository has no server in it: a copy made by
        # hand cannot ask anybody, and so hands out nothing.
        check("the Worker in the repository has no server in it",
              "__ICLOUD_SERVER__" in
              (Path(standalone.WORKER) / "worker.js").read_text(encoding="utf-8"))
    finally:
        recaptcha.VERIFY_URL = real_verify
        google.stop()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
