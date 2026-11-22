"""End-to-end in a real browser engine.

Run with: .venv/bin/python tests/test_real_browser.py   (or `npm test`)

Every other suite runs in jsdom, which is not a browser: it has no layout, no
style resolution, no shadow-DOM parser, and it is more permissive than Chromium
about how built-in methods are called. This suite closes that gap by driving a
real Chromium with Playwright.

It needs a Chromium executable. It looks for:
  1. $CHROME_PATH
  2. Playwright's own installed Chromium
and skips cleanly if neither exists, so `npm test` still passes on a machine
without one. Set CHROME_PATH to run it.
"""

import asyncio
import os
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AGENT_JS = (ROOT / "agent.js").read_text(encoding="utf-8")
MIRROR_JS = (ROOT / "mirror.js").read_text(encoding="utf-8")

# The mirror is the captured page itself, with the engine that keeps it in
# step with the real browser put into its head - which is what the server
# does when it writes the capture out.
MIRROR_PAGE = (
    "<!DOCTYPE html><html><head><script>" + MIRROR_JS + "</script>"
    "</head><body></body></html>"
)

passed = 0
failed = 0
skipped = False


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + detail) if detail else ""))


def find_chromium():
    """Locate a runnable Chromium, or None to skip."""
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env, os.environ.get("CHROME_LD_PATH", "")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and Path(path).exists():
                return path, ""
    except Exception:
        pass
    return None, ""


# A page with the three things jsdom cannot exercise: a real shadow root, a
# styled element whose computed values only a layout engine can resolve, and
# inline SVG in its own namespace.
SOURCE_PAGE = """<!DOCTYPE html>
<html><head><style>
  .box { background-color: rgb(255, 0, 0); width: 120px; padding-left: 16px;
         font-size: 24px; border-radius: 8px; position: absolute; top: 40px; }
</style></head>
<body>
  <div class="box" id="box">styled</div>
  <div id="host"></div>
  <ul id="list"><li>a</li></ul>
  <svg id="svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">
    <circle id="c" r="4"></circle>
  </svg>
  <script>
    const host = document.getElementById('host');
    const sr = host.attachShadow({ mode: 'open' });
    const btn = document.createElement('button');
    btn.id = 'inner';
    btn.textContent = 'Sign In';
    sr.appendChild(btn);
  </script>
</body></html>"""


# A page with a frame that never answers. The frame is sandboxed without
# allow-scripts, so the agent does not run inside it and it cannot reply to
# the capture request - which makes the capture wait for its timeout.
SLOW_PAGE = """<!DOCTYPE html>
<html><head><title>slow</title></head>
<body>
  <div id="root">root</div>
  <iframe sandbox="allow-same-origin" src="/__frame" style="width:10px;height:10px"></iframe>
</body></html>"""

# A page whose frame holds a field. The frame is same-origin, so it is a
# frame the agent runs inside - the case where the agent has to notice that
# the frame's document was replaced.
FRAMED_PAGE = """<!DOCTYPE html>
<html><head><title>framed</title></head>
<body>
  <div id="root">root</div>
  <iframe src="/__inner" style="width:400px;height:200px"></iframe>
</body></html>"""

INNER_PAGE = """<!DOCTYPE html>
<html><head><title>inner</title></head>
<body><input id="acct" type="text"><p id="status"></p>
<script>
  document.getElementById('acct').addEventListener('input', (ev) => {
    document.getElementById('status').textContent = 'typed: ' + ev.target.value;
  });
</script>
</body></html>"""


async def run():
    from playwright.async_api import async_playwright

    exe, ld_path = find_chromium()
    if not exe:
        return None

    env = dict(os.environ)
    if ld_path:
        env["LD_LIBRARY_PATH"] = ld_path + os.pathsep + env.get("LD_LIBRARY_PATH", "")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=exe,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            env=env or None,
        )
        print(f"     engine: Chromium {browser.version}")
        context = await browser.new_context()

        # ------------------------------------------------ source page
        messages = []
        # What the agent sent, and which page it came from. The page's own
        # copy of the binding cannot be used to watch this: the binding is
        # put back into a frame when the frame is created, and this page has
        # a frame - so a wrapper installed on the page is quietly replaced.
        sent_by_page = []

        def collect(source, msg):
            messages.append(msg)
            frame = source.get("frame") if isinstance(source, dict) else None
            url = frame.url if frame is not None else "?"
            # Every frame's document, not just the top one: a change inside
            # a frame shows up in the frame's own capture.
            html = "".join(f.get("html", "") for f in (msg.get("frames") or []))
            sent_by_page.append((url, msg.get("kind"), html))

        await context.expose_binding("__rbEmit", collect)
        await context.add_init_script(AGENT_JS)

        src = await context.new_page()
        # The agent captures on DOMContentLoaded, so this must be a real
        # navigation. page.set_content() rewrites the document *after* the init
        # script has already captured the empty predecessor, which silently
        # yields a body-less snapshot. Route fulfil gives a genuine navigation.
        await src.route("**/__srcpage", lambda r: r.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=SOURCE_PAGE))
        await src.goto("http://127.0.0.1:9002/__srcpage")
        await src.wait_for_timeout(500)

        # new_page() also captures the initial about:blank, so take the last
        # capture rather than the first.
        docs = [m for m in messages if m.get("kind") == "doc"]
        doc = docs[-1] if docs else None
        check("agent produced a capture in a real browser", doc is not None)
        if not doc:
            await browser.close()
            return False

        frame = doc["frames"][0]
        html = frame["html"]

        # Declarative shadow DOM is only real if the engine parses it.
        check("declarative shadow template emitted",
              'shadowrootmode="open"' in html)
        check("shadow content serialised", "Sign In" in html)

        # Computed styles from an actual layout engine.
        check("computed background inlined",
              "background-color:rgb(255, 0, 0)" in html or "background-color: rgb(255, 0, 0)" in html,
              html[html.find("id=\"box\"") - 20: html.find("id=\"box\"") + 220])
        check("computed width inlined", "width:120px" in html or "width: 120px" in html)
        check("computed font-size inlined", "font-size:24px" in html or "font-size: 24px" in html)
        check("computed position inlined", "position:absolute" in html or "position: absolute" in html)

        # ------------------------------------------------ mirror page
        mirror = await context.new_page()
        # The mirror opens a WebSocket on load; stub it so we drive the
        # mirror directly instead of needing the server in the loop.
        await mirror.add_init_script(
            "class _WS { constructor(){ this.readyState = 1; } send(){} close(){} }"
            "window.WebSocket = _WS;"
        )
        await mirror.set_content(MIRROR_PAGE)
        await mirror.wait_for_timeout(200)

        # Show the capture in the mirror, the way the server's page does.
        await mirror.evaluate(
            "(a) => window.seed(a.frames, a.viewport)",
            {"frames": doc["frames"], "viewport": doc.get("viewport")},
        )
        await mirror.wait_for_timeout(400)

        # Did the engine turn the declarative template into a real shadow root?
        shadow_info = await mirror.evaluate(
            """() => {
                 const host = document.getElementById('host');
                 if (!host) return { host: false };
                 const sr = host.shadowRoot;
                 return {
                   host: true,
                   hasShadowRoot: !!sr,
                   text: sr ? sr.textContent.trim() : null,
                   childTag: sr && sr.firstElementChild ? sr.firstElementChild.tagName : null,
                 };
               }"""
        )
        check("mirror parsed the host element", shadow_info.get("host") is True, str(shadow_info))
        check("declarative shadow DOM attached by the real engine",
              shadow_info.get("hasShadowRoot") is True, str(shadow_info))
        check("shadow content is live and readable",
              shadow_info.get("text") == "Sign In", str(shadow_info))
        check("shadow child is a real element",
              shadow_info.get("childTag") == "BUTTON", str(shadow_info))

        # Styles actually applied, not just present as text.
        applied = await mirror.evaluate(
            """() => {
                 const box = document.getElementById('box');
                 const cs = getComputedStyle(box);
                 return {
                   bg: cs.backgroundColor,
                   width: cs.width,
                   fontSize: cs.fontSize,
                   position: cs.position,
                 };
               }"""
        )
        check("background actually applied in the mirror",
              applied["bg"] == "rgb(255, 0, 0)", str(applied))
        check("width actually applied in the mirror", applied["width"] == "120px", str(applied))
        check("font-size actually applied in the mirror",
              applied["fontSize"] == "24px", str(applied))
        check("position actually applied in the mirror",
              applied["position"] == "absolute", str(applied))

        # SVG kept its namespace across the trip.
        svg_ns = await mirror.evaluate(
            """() => {
                 const s = document.getElementById('svg');
                 const c = document.getElementById('c');
                 return { svg: s && s.namespaceURI, circle: c && c.namespaceURI };
               }"""
        )
        check("svg namespace preserved",
              svg_ns["svg"] == "http://www.w3.org/2000/svg", str(svg_ns))
        check("svg child namespace preserved",
              svg_ns["circle"] == "http://www.w3.org/2000/svg", str(svg_ns))

        # ------------------------------------------------ live change
        # A change on the page is answered with a fresh capture of the whole
        # page. The mirror shows that capture, so it cannot drift out of step
        # with the page and it cannot be patched onto the wrong node.
        messages.clear()
        await src.evaluate("document.querySelector('#list').appendChild(Object.assign(document.createElement('li'), {textContent:'b'}))")
        await src.wait_for_timeout(1500)

        docs2 = [m for m in messages if m.get("kind") == "doc"]
        check("a live change produces a fresh capture", len(docs2) > 0,
              "kinds: " + str([m.get("kind") for m in messages]))
        if docs2:
            capture = docs2[-1]
            check("the capture has the change in it",
                  "b" in capture["frames"][0]["html"], capture["frames"][0]["html"][:160])
            await mirror.evaluate(
                "(a) => window.seed(a.frames, a.viewport)",
                {"frames": capture["frames"], "viewport": capture.get("viewport")},
            )
            await mirror.wait_for_timeout(300)
            items = await mirror.evaluate(
                "() => [...document.querySelectorAll('#list li')].map(n => n.textContent)"
            )
            check("the change reached the real mirror", items == ["a", "b"], str(items))

        # ------------------------------------ a frame that is updated
        # The mirror shows the captured page, and a capture that arrives
        # replaces it. A frame's document must not be replaced under the
        # user while they are typing into it: a keystroke that lands in
        # that moment reaches a document with nothing listening on it, and
        # the character is lost.
        #
        # It was lost every time, and it was always the same character,
        # because the capture always arrives in the same place in the
        # typing. The mirror showed "someone@iclud.com" for
        # "someone@icloud.com", and the real browser had the same wrong
        # value - the two agreed with each other and both were wrong.
        framed_mirror = await context.new_page()
        await framed_mirror.set_content(MIRROR_PAGE)
        await framed_mirror.wait_for_timeout(200)
        # Record what the mirror forwards. set_content() does not run init
        # scripts, so the stub goes in afterwards - before anything is
        # forwarded.
        await framed_mirror.evaluate(
            """() => {
                 window.__sent = [];
                 window.fetch = (path, opts) => {
                   if (String(path).indexOf('/keys') !== -1) {
                     window.__sent.push(JSON.parse(opts.body).text);
                   }
                   return Promise.resolve({ ok: true, json: () => Promise.resolve(null) });
                 };
               }""")

        top_html = ('<!DOCTYPE html><html><body>'
                    '<iframe data-rb-frame="0.0" style="width:300px;height:200px">'
                    '</iframe></body></html>')
        inner_html = ('<!DOCTYPE html><html><body><input id="acct" type="text">'
                      '</body></html>')

        async def seed_mirror(round_no):
            await framed_mirror.evaluate(
                "(a) => window.seed(a.frames, a.viewport)",
                {"frames": [
                    {"windowId": "0", "html": top_html},
                    {"windowId": "0.0",
                     "html": inner_html.replace(
                         "</body>", "<p id=r>%d</p></body>" % round_no)},
                ], "viewport": {"w": 800, "h": 600}})

        await seed_mirror(0)
        await framed_mirror.wait_for_timeout(300)

        # Right after a capture, the frame's document must already be
        # there and already listening. There must be no moment in which a
        # keystroke has nowhere to go.
        watched = await framed_mirror.evaluate(
            """() => {
                 const f = document.querySelector('iframe[data-rb-frame="0.0"]');
                 const doc = f && f.contentDocument;
                 if (!doc) return { doc: false };
                 const input = doc.getElementById('acct');
                 if (!input) return { doc: true, input: false };
                 window.__before = window.__sent.length;
                 input.dispatchEvent(new doc.defaultView.KeyboardEvent(
                   'keydown', { key: 'x', bubbles: true, cancelable: true }));
                 return { doc: true, input: true };
               }""")
        check("the capture put the frame's document in place",
              watched.get("doc") is True and watched.get("input") is True,
              str(watched))
        # The mirror sends input on, one request at a time, so the request
        # has made its way out before the count is read.
        await framed_mirror.wait_for_timeout(200)
        heard = await framed_mirror.evaluate(
            "() => window.__sent.length > window.__before")
        check("the frame's document is listening the moment it is in place",
              heard is True, str(watched))

        # And typing while captures keep arriving must not lose anything.
        await framed_mirror.evaluate("() => { window.__sent = []; }")
        for i, ch in enumerate("abcdefgh"):
            await seed_mirror(i + 1)
            for f in framed_mirror.frames:
                if f == framed_mirror.main_frame:
                    continue
                try:
                    loc = f.locator("#acct").first
                    if await loc.count():
                        await loc.press(ch)
                        break
                except Exception:
                    pass
        await framed_mirror.wait_for_timeout(500)
        sent = await framed_mirror.evaluate("() => window.__sent")
        check("no keystroke is lost while captures arrive",
              "".join(sent) == "abcdefgh", repr(sent))

        # ------------------------------------ bare text-node replacement
        # This is the bug class that caused "the dom is not updating": a
        # childList mutation whose added/removed nodes are TEXT_NODE was
        # skipped entirely, so the mirror froze. Regression-pin it here, in a
        # real engine, because it is the exact symptom the user reported.
        # (a) characterData: editing a text node in place.
        messages.clear()
        await src.evaluate("document.querySelector('#list li').firstChild.nodeValue = 'CHANGED'")
        await src.wait_for_timeout(1500)
        cd_docs = [m for m in messages if m.get("kind") == "doc"]
        check("in-place text edit produces a capture", len(cd_docs) > 0,
              "kinds: " + str([m.get("kind") for m in messages]))
        check("in-place text edit is in the capture",
              any("CHANGED" in m["frames"][0]["html"] for m in cd_docs),
              str([m["frames"][0]["html"][:120] for m in cd_docs])[:200])

        # (b) childList whose added/removed nodes are TEXT_NODE. This is the
        # distinct mutation shape: `li.textContent = x` removes the old text
        # node and inserts a new one. Element-only filters miss it entirely,
        # which is what froze the mirror. It must be exercised separately from
        # (a) because a MutationObserver reports it as a different record type.
        messages.clear()
        await src.evaluate("document.querySelector('#list li').textContent = 'REPLACED'")
        await src.wait_for_timeout(1500)
        cl_docs = [m for m in messages if m.get("kind") == "doc"]
        check("text-node replacement produces a capture", len(cl_docs) > 0,
              "kinds: " + str([m.get("kind") for m in messages]))
        check("text-node replacement is not silently dropped",
              any("REPLACED" in m["frames"][0]["html"] for m in cl_docs),
              str([m["frames"][0]["html"][:120] for m in cl_docs])[:200])

        # ------------------------------------ mutation inside a shadow root
        # A shadow root created by a script at runtime is never reported by a
        # MutationObserver, so discovery depends on the sweep. Prove a change
        # *inside* one reaches the mirror.
        messages.clear()
        await src.evaluate("document.getElementById('host').shadowRoot.querySelector('#inner').textContent = 'Sign Out'")
        await src.wait_for_timeout(1500)
        shadow_docs = [m for m in messages if m.get("kind") == "doc"]
        check("a change inside a runtime shadow root produces a capture",
              len(shadow_docs) > 0, "kinds: " + str([m.get("kind") for m in messages]))
        check("the capture carries the change inside the shadow root",
              any("Sign Out" in m["frames"][0]["html"] for m in shadow_docs),
              str([m["frames"][0]["html"][:200] for m in shadow_docs])[:300])

        # ------------------------------------ the mirror cannot drift
        # There is no patch to misapply: the mirror is rebuilt from the
        # capture. So what is worth pinning is that a rebuild shows the page
        # as it is, and that a rebuild cannot leave half a page behind.
        await src.evaluate("document.getElementById('box').textContent = 'changed text'")
        await src.wait_for_timeout(1500)
        docs3 = [m for m in messages if m.get("kind") == "doc"]
        if docs3:
            capture = docs3[-1]
            await mirror.evaluate(
                "(a) => window.seed(a.frames, a.viewport)",
                {"frames": capture["frames"], "viewport": capture.get("viewport")},
            )
            await mirror.wait_for_timeout(300)
        text = await mirror.evaluate("() => document.getElementById('box').textContent")
        check("the mirror shows the page as it is", text == "changed text", str(text))
        copies = await mirror.evaluate("() => document.querySelectorAll('#box').length")
        check("the rebuild replaced the page rather than adding to it",
              copies == 1, str(copies) + " copies of #box")

        # ------------------------------- a change must not overtake its document
        # A capture reads the page. If the page changes while the capture is
        # being taken, the capture that is already on its way does not
        # contain the change - so another capture has to follow it. Dropping
        # the change would leave the mirror showing a page that is not there.
        #
        # A frame that never answers makes the capture wait for its timeout,
        # which is long enough for a change to happen in the middle of one.
        slow = await context.new_page()
        await slow.route("**/__slowpage", lambda r: r.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=SLOW_PAGE))
        await slow.goto("http://127.0.0.1:9002/__slowpage")
        # The page's own capture has to be over before the scenario starts:
        # it waits for the frame that never answers.
        await slow.wait_for_timeout(2200)

        # The scenario has to start from a quiet agent, or the request below
        # lands on a capture that is already running and nothing that
        # happens afterwards can be told apart from what was already in
        # flight. The agent says what it is doing.
        async def wait_idle(page, timeout=20.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = await page.evaluate(
                    "() => window.__rbInternals ? window.__rbInternals.state() : null")
                if state and not state["capturing"] and not state["pending"] \
                        and not state["dirty"]:
                    return True
                await page.wait_for_timeout(50)
            return False

        check("the agent settles before the scenario",
              await wait_idle(slow), "the agent was still busy")

        before = len(sent_by_page)

        # Started but not awaited: the capture is in the middle of its frame
        # timeout, and the change has to happen while it is.
        await slow.evaluate("() => { window.__rbResync(); }")
        await slow.wait_for_timeout(120)
        await slow.evaluate(
            "() => { const d = document.createElement('div');"
            " d.id = 'during'; document.body.appendChild(d); }"
        )
        # Long enough for the capture to finish and the change to be sent.
        # Each capture waits for the frame that never answers, so this is
        # two capture round-trips, not one.
        await slow.wait_for_timeout(4000)

        sent = [m for m in sent_by_page[before:]
                if m[0].endswith("__slowpage") and m[1] == "doc"]
        check("a change made during a capture is not lost", len(sent) >= 2, str(sent))
        check("the last capture carries the change that happened during the first",
              bool(sent) and "during" in sent[-1][2],
              str(sent[-1][2][:200]) if sent else "no capture")

        # A capture asked for while another one is still running. The running
        # one read the document before the request, so it may already be out
        # of date - dropping the request would leave the mirror stale with
        # nothing to show for it.
        check("the agent settles again", await wait_idle(slow), "the agent was still busy")
        before = len(sent_by_page)
        await slow.evaluate("() => { window.__rbResync(); }")
        await slow.wait_for_timeout(150)
        await slow.evaluate("() => { window.__rbResync(); }")
        await slow.wait_for_timeout(4000)
        sent = [m for m in sent_by_page[before:]
                if m[0].endswith("__slowpage") and m[1] == "doc"]
        check("a capture asked for during a capture is not lost",
              len(sent) >= 2, str(sent))

        # ------------------------- a frame's document is replaced underneath
        # A frame is born as an empty document and then navigates to the page
        # it is really going to show. Chromium keeps the SAME window across
        # that swap, and the script that started the agent is not run again
        # for it - so an agent that only ever looked at the document it
        # started with is left watching a document that is no longer on
        # screen. Its observer sees nothing, and the form controls it
        # listens to are not the ones the user is typing into.
        #
        # The mirror has to notice, and the capture that follows has to
        # carry what changed.
        framed = await context.new_page()
        await framed.route("**/__framed", lambda r: r.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=FRAMED_PAGE))
        await framed.route("**/__inner", lambda r: r.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=INNER_PAGE))
        await framed.goto("http://127.0.0.1:9002/__framed")
        await framed.wait_for_timeout(2500)

        inner = None
        for f in framed.frames:
            if f.url.endswith("__inner"):
                inner = f
        check("the frame is loaded", inner is not None,
              str([f.url for f in framed.frames]))
        if inner is None:
            await browser.close()
            return True

        before = len(sent_by_page)
        # Type into the field in the frame, in the real browser.
        await inner.locator("#acct").click()
        await inner.locator("#acct").press_sequentially("hello", delay=40)
        await framed.wait_for_timeout(3000)

        frames_seen = [m for m in sent_by_page[before:] if m[1] == "doc"]
        check("a change inside a frame produces a capture",
              len(frames_seen) > 0,
              str([m[0] for m in sent_by_page[before:]]))
        check("the capture carries what changed inside the frame",
              any("typed: hello" in m[2] for m in frames_seen),
              str([m[2][-200:] for m in frames_seen])[-300:])

        await browser.close()
        return True


if __name__ == "__main__":
    result = asyncio.run(run())
    if result is None:
        print("\n0 passed, 0 failed")
        print("SKIPPED: no Chromium found. Set CHROME_PATH to run this suite.")
        sys.exit(0)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
