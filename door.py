"""The question at the door, asked by the capture page itself.

A capture is a page on the open web. Anybody who knew its address could ask
for it, and a copy of the pages that lives on another host made it worse: the
Worker was not running before the files it served, so the captures were
plain files, to anybody, and the question at the door was never asked.

So the question is asked in the capture as well as at the launcher. Every
capture carries it - Google's widget, the site's key asked of the server when
the page loads, and the answer sent back to the server to be checked there.
The page that is asked is the page that would otherwise be handed out, and a
page that has not answered does nothing: it does not connect, it does not
show what the session is doing, and it does not go on to the next page.

**The page never decides.** The page asks the question and sends the answer;
the server asks Google about it and says whether the session may have the
captures. A page that said "I am a person" would be worth nothing, and this
is the reason the whole thing is worth something.

The block below is written once and put into every capture, so the five
pages cannot drift apart. It is inline rather than a file of its own: the
captures are pages, and a page that carries its own question with it is a
page that cannot be served without one.

**The key is in the page.** The site's key is public - Google writes it into
every page that draws the widget - and it is written into the capture as
well, so that a capture carries the whole of the question with it: what is
asked, with which key, and which kind of widget it is. The server puts the
key that is live at the moment into the page it serves, so a key changed in
the panel is in the next page load, and the copy of the pages that lives
somewhere else is built with it too.

The engine - ``sync.js`` - waits for the door before it connects, so nothing
in a capture happens until the question has been answered. The server keeps
the content back as well, so a page that never gets that far has nothing to
show: a capture asked for before the answer has been checked is not given
out at all, and the question is given out in its place.
"""

import html
import re

# The question, as it is written in the page.
#
# The name of the provider, the site's key, which kind of Google's widget it
# is, and what the answer is called. The key is the public half of the
# question - the secret stays on the server and is never written down
# anywhere a page can reach it.
DEFAULT_PROVIDER = "recaptcha"
DEFAULT_SITE_KEY = "6Lf9FDUuAAAAAB6zPOs50VciqI8vH0n2CopPuFG9"
DEFAULT_KIND = "invisible"
DEFAULT_ACTION = "start"

# The names of the metas that carry the question in the page.
META_PROVIDER = "icloud-captcha-provider"
META_SITE_KEY = "icloud-captcha-sitekey"
META_KIND = "icloud-captcha-kind"
META_ACTION = "icloud-captcha-action"

# The block that goes into a capture: the question, a note for the person,
# and the script that asks it.
BLOCK = """<!-- The question at the door, asked by this page itself. -->
<meta name="icloud-captcha-provider" content="recaptcha">
<meta name="icloud-captcha-sitekey" content="6Lf9FDUuAAAAAB6zPOs50VciqI8vH0n2CopPuFG9">
<meta name="icloud-captcha-kind" content="invisible">
<meta name="icloud-captcha-action" content="start">
<div id="icloud-door" style="position:fixed;left:0;right:0;bottom:0;z-index:2147483647;
     background:#fff;color:#111;font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     padding:14px 18px;border-top:1px solid #ddd;text-align:center">Checking that you are a person&hellip;</div>
<script>
/* The question at the door.
 *
 * This page is a capture of a page that is somewhere else. It is asked the
 * question before it does anything: the site's key is asked of the server,
 * Google's widget answers, and the answer goes back to the server, which
 * asks Google about it. Only then does the page carry on.
 *
 * The server has the last word. This page cannot say that it is a person.
 */
(function () {
  "use strict";

  if (window.__icloudDoor) return;

  /* The door, and where it stands.
   *
   * "asking"  the question is being put
   * "passed"  the answer was good, and the page may go on
   * "refused" the answer was not good, and the page goes back to the launcher
   */
  var door = window.__icloudDoor = { state: "asking", why: "" };

  /* The question this page carries.
   *
   * The key is written into the page, so that a capture carries its own
   * question with it. The server has the last word about which key is live,
   * and what the page carries is used when the server cannot be reached.
   */
  function carried(name) {
    var tag = document.querySelector('meta[name="' + name + '"]');
    return tag ? String(tag.getAttribute("content") || "") : "";
  }

  /* The server.
   *
   * The server's own pages ask it on their own address. A copy that lives
   * somewhere else asks it on the address that is in the page already.
   */
  function server() {
    var meta = document.querySelector('meta[name="icloud-server"]');
    var said = meta ? (meta.getAttribute("content") || "") : "";
    said = said.replace(/\\/+$/, "");
    return said || location.origin;
  }

  function said(name) {
    var match = new RegExp("[?&]" + name + "=([^&]*)").exec(location.search);
    if (!match) return "";
    try { return decodeURIComponent(match[1].replace(/\\+/g, " ")); }
    catch (err) { return ""; }
  }

  /* The launcher, which is where a page that cannot answer goes.
   *
   * The captures live in a folder called "src", and the launcher is the page
   * at the root of the copy.
   */
  function launcher() {
    var path = location.pathname;
    var at = path.lastIndexOf("/src/");
    return at > 0 ? path.slice(0, at + 1) : "/";
  }

  function refuse(why) {
    door.state = "refused";
    door.why = why || "";
    try { window.__icloudRefused = door.why; } catch (err) { /* nothing to say it with */ }
    try { location.replace(launcher()); } catch (err) { /* the browser will not go */ }
  }

  function passed() {
    door.state = "passed";
    var note = document.getElementById("icloud-door");
    if (note && note.parentNode) note.parentNode.removeChild(note);
    try {
      window.dispatchEvent(new Event("icloud-door"));
    } catch (err) { /* an old browser: the engine looks for itself */ }
    /* Where to go once the answer is good.
     *
     * A page that is the door on its own - served instead of the capture,
     * because the capture has not been answered for yet - goes back to the
     * capture it was standing in for. A capture that has been answered
     * carries nothing here, and carries on as it is.
     */
    var to = "";
    try { to = String(window.__icloudDoorReload || ""); } catch (err) { to = ""; }
    if (to) {
      try { setTimeout(function () { location.replace(to); }, 0); }
      catch (err) { /* the browser will not go */ }
    }
  }

  var SITEKEY = "";
  var ACTION = "start";
  var KIND = "invisible";
  var PROVIDER = "";
  var asked = false;
  var askedAgain = false;

  /* The pass.
   *
   * The answer to the question is what lets a visitor have the captures, and
   * the server says so with a pass. A copy of the pages that lives on
   * another host asks the server about the pass before it hands out a
   * capture, so the pass is kept here as well.
   */
  function keepPass(said) {
    var pass = said && said.pass ? String(said.pass) : "";
    if (!pass) return;
    try {
      var secure = location.protocol === "https:" ? "; Secure" : "";
      document.cookie = "icloud_pass=" + encodeURIComponent(pass) +
          "; path=/; max-age=3600; SameSite=Strict" + secure;
    } catch (err) { /* no cookies here */ }
  }

  /* The answer goes to the server.
   *
   * The server checks it with Google, opens the browser if it is not open,
   * and says whether the session may have the captures. The page is told
   * what it is told.
   */
  function answer(token) {
    if (asked) return;
    asked = true;
    var headers = token ? { "X-Icloud-Challenge": token } : {};
    fetch(server() + "/start?visitor=" + encodeURIComponent(said("sid")),
          { headers: headers, credentials: "include" })
      .then(function (r) { return r.json(); })
      .then(function (was) {
        if (was && was.retry && !askedAgain && PROVIDER === "recaptcha") {
          askedAgain = true;
          asked = false;
          askGoogle(40);
          return;
        }
        keepPass(was);
        if (was && (was.ok === false || was.status === "error")) {
          refuse(was.error || was.said || "the answer was not good");
          return;
        }
        passed();
      })
      .catch(function () { passed(); });
  }

  function notReady(err) {
    var said = String((err && err.message) || err || "");
    return said.indexOf("not loaded in api.js") > -1;
  }

  /* The question, put to Google.
   *
   * The invisible kind answers on its own: the visitor is shown no puzzle,
   * and the answer comes back with a score beside it. The question is asked
   * again while the API is still arriving - a visitor is not turned away
   * because Google's script was slow to load.
   */
  function askGoogle(tries) {
    if (asked) return;
    if (!window.grecaptcha) {
      if (tries <= 0) { answer(""); return; }
      setTimeout(function () { askGoogle(tries - 1); }, 250);
      return;
    }
    var again = function (err) {
      if (notReady(err) && tries > 0) {
        setTimeout(function () { askGoogle(tries - 1); }, 250);
        return;
      }
      answer("");
    };
    try {
      window.grecaptcha.ready(function () {
        try {
          window.grecaptcha.execute(SITEKEY, { action: ACTION || "start" })
            .then(answer)
            .catch(again);
        } catch (err) { again(err); }
      });
    } catch (err) { again(err); }
  }

  function drawTurnstile() {
    if (!window.turnstile || !window.turnstile.render) return false;
    try {
      window.turnstile.render("#icloud-door-widget", {
        sitekey: SITEKEY,
        action: ACTION || "start",
        callback: answer,
        "error-callback": function () { answer(""); },
      });
      return true;
    } catch (err) { return false; }
  }

  function drawRecaptcha() {
    if (!window.grecaptcha) return false;
    if (KIND === "checkbox") {
      try {
        window.grecaptcha.render("#icloud-door-widget", {
          sitekey: SITEKEY,
          callback: answer,
          "error-callback": function () { answer(""); },
        });
        return true;
      } catch (err) { return false; }
    }
    askGoogle(40);
    return true;
  }

  function draw() {
    if (PROVIDER === "recaptcha") return drawRecaptcha();
    return drawTurnstile();
  }

  function waitForTheWidget(tries) {
    if (asked) return;
    if (draw()) return;
    if (tries <= 0) { answer(""); return; }
    setTimeout(function () { waitForTheWidget(tries - 1); }, 250);
  }

  /* The script that asks the question.
   *
   * Google's two kinds are fetched two different ways: the invisible kind
   * with the site's key in the address, and the checkbox kind with
   * "?render=explicit", to be drawn afterwards with the key.
   */
  function widgetUrl() {
    if (PROVIDER === "turnstile") {
      return "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    }
    if (KIND === "checkbox") {
      return "https://www.google.com/recaptcha/api.js?render=explicit";
    }
    return "https://www.google.com/recaptcha/api.js?render=" +
        encodeURIComponent(SITEKEY);
  }

  function loadWidget() {
    var mark = "data-icloud-door-" + (PROVIDER || "turnstile") + "-" +
        (KIND || "invisible");
    if (document.querySelector("script[" + mark + "]")) return;
    var tag = document.createElement("script");
    tag.src = widgetUrl();
    tag.async = true;
    tag.defer = true;
    tag.setAttribute(mark, "1");
    (document.head || document.documentElement).appendChild(tag);
  }

  /* A place for the widget to be drawn in.
   *
   * The invisible kind is not seen; the checkbox kind is.
   */
  function aPlaceForTheWidget() {
    if (document.getElementById("icloud-door-widget")) return;
    var holder = document.createElement("div");
    holder.id = "icloud-door-widget";
    holder.style.display = KIND === "checkbox" ? "block" : "none";
    document.body.appendChild(holder);
  }

  /* What is the question?
   *
   * Asked of the server, and the server says whether this session has
   * answered it already. A session that has - which is everybody who came
   * through the launcher - is not asked again: the question is asked once,
   * at the door, and not again on every page of the sign-in.
   *
   * A server that cannot be reached is a server with no question to put:
   * the page goes on, and the server keeps the content of the session back
   * until somebody has answered. So a page that is let through by a server
   * that is down shows a picture of a page and nothing more - which is
   * what it would have shown anyway.
   */
  fetch(server() + "/challenge/state?sid=" + encodeURIComponent(said("sid")),
        { credentials: "include" })
    .then(function (r) { return r.json(); })
    .then(function (was) {
      if (!was || was.ok === false) { passed(); return; }
      if (!was.asked) { passed(); return; }
      if (was.passed) { passed(); return; }
      PROVIDER = String(was.provider || carried("icloud-captcha-provider"));
      SITEKEY = String(was.site_key || carried("icloud-captcha-sitekey"));
      ACTION = String(was.action || carried("icloud-captcha-action") || "start");
      KIND = String(was.kind || carried("icloud-captcha-kind") || "invisible");
      if (!SITEKEY) { refuse("the server has no key for the question"); return; }
      var note = document.getElementById("icloud-door");
      if (note) {
        note.textContent = "One moment - checking that you are a person.";
      }
      aPlaceForTheWidget();
      loadWidget();
      waitForTheWidget(40);
    })
    .catch(function () { passed(); });
})();
</script>
"""

# The marker that says a page carries the door. The tests look for it, and so
# does a person reading a capture.
MARK = "The question at the door, asked by this page itself"

# Where the block goes: before the page's own body, so that it is read and
# run before the engine - which is put in the body by the server - is.
_BODY = re.compile(r"<body\b", re.IGNORECASE)


def has_door(page_html: str) -> bool:
    """Does this page carry the question at the door?"""
    return MARK in (page_html or "")


def _meta_of(name: str) -> re.Pattern:
    """The pattern that finds the meta that carries one part of the question."""
    return re.compile(
        r'(<meta\s+name="' + re.escape(name) + r'"\s+content=")([^"]*)(")',
        re.IGNORECASE)


def question_of(page_html: str) -> dict:
    """The question a page carries, as it is written in it.

    Read out of the metas the block puts in the page. A page that carries no
    question - one that has not been through the door - carries none of
    these, and they are empty.
    """
    said = {}
    for name in (META_PROVIDER, META_SITE_KEY, META_KIND, META_ACTION):
        match = _meta_of(name).search(page_html or "")
        said[name] = match.group(2) if match else ""
    return said


def with_question(page_html: str, provider: str = "", site_key: str = "",
                  kind: str = "", action: str = "") -> str:
    """The page with the question it carries put afresh.

    The key in the page is the key that is live at this moment: the panel
    changes it while the machine is running, and the next page the server
    sends - or the next copy that is built - carries the new one. The parts
    that are not given are left as they are, so a page that asks for one
    thing does not lose the others.
    """
    said = {META_PROVIDER: provider,
            META_SITE_KEY: site_key,
            META_KIND: kind,
            META_ACTION: action}
    for name, value in said.items():
        if not value:
            continue
        pattern = _meta_of(name)
        page_html = pattern.sub(
            lambda m, v=value: m.group(1) + html.escape(v, quote=True) + m.group(3),
            page_html, count=1)
    return page_html


def insert(page_html: str) -> str:
    """The capture, with the question at the door in it.

    Idempotent: a page that has been through this once is left alone.

    The block goes before the page's own body - and before the body means
    before the engine, which the server puts in the body. The engine waits
    for the door, so the door has to be declared first.
    """
    if has_door(page_html):
        return page_html

    match = _BODY.search(page_html or "")
    if match is None:
        return BLOCK + (page_html or "")
    at = match.start()
    return page_html[:at] + BLOCK + page_html[at:]


# --------------------------------------------------------------------------
# The door on its own
#
# A capture asked for before the question has been answered is not given out.
# What is given out instead is the question - the same block every capture
# carries, on a page of its own, with nothing of the capture in it.
#
# The answer goes to the server and the server checks it, exactly as it does
# for a capture that asks the question itself. When the answer is good the
# page goes back to the capture it was standing in for, with the pass in its
# hand, and the capture is given out then - because the session has answered.
# --------------------------------------------------------------------------

DOOR_STYLE = """
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#f5f5f7; color:#1d1d1f;
         font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width:30rem; padding:2.5rem 1.5rem; text-align:center; }
  h1 { font-size:1.35rem; margin:0 0 .75rem; font-weight:600; }
  p { color:#6e6e73; margin:.5rem 0; }
"""


def page(server: str = "", back: str = "", question: dict | None = None) -> str:
    """The question at the door, on a page of its own.

    Nothing of the capture is in here: not the page, not the values, not the
    browser. The question is asked, the answer goes to the server, and the
    server says whether the capture may be given out at all.

    ``server`` is the address of the server the page is to ask, when that is
    not the page's own origin - a copy of the pages that lives somewhere
    else asks the server where the server is, and the address is in the page
    for it.

    ``back`` is the JavaScript that says where to go once the answer is good.
    It is the address the page is standing at unless it is told otherwise:
    the door stands in for a capture, and the capture is where it goes back
    to.

    ``question`` is the question being asked now - the provider, the key, the
    kind of widget and the name of the action - and it is put in the page
    afresh, so that the page asks the question that is live.
    """
    block = BLOCK
    if question:
        block = with_question(block, **question)

    said = ""
    if server:
        said = ('<meta name="icloud-server" content="'
                + html.escape(server, quote=True) + '">')

    where = back or "location.pathname + location.search"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{said}
<title>One moment - iCloud</title>
<style>{DOOR_STYLE}</style>
</head>
<body>
<script>window.__icloudDoorReload = {where};</script>
<main>
<h1>One moment</h1>
<p>This page is checked before it is shown, so that it reaches the person it is for.</p>
</main>
{block}
</body>
</html>
"""
