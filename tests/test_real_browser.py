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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AGENT_JS = (ROOT / "agent.js").read_text(encoding="utf-8")
VIEW_HTML = (ROOT / "view.html").read_text(encoding="utf-8")

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
        await context.expose_binding("__rbEmit", lambda source, msg: messages.append(msg))
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
        # view.html opens a WebSocket on load; stub it so we drive the applier
        # directly instead of needing the server in the loop.
        await mirror.add_init_script(
            "class _WS { constructor(){ this.readyState = 1; } send(){} close(){} }"
            "window.WebSocket = _WS;"
        )
        await mirror.set_content(VIEW_HTML)
        await mirror.wait_for_timeout(200)

        # Load the captured document into the mirror the way view.html does.
        await mirror.evaluate(
            """(frame) => {
                 const holder = document.createElement('div');
                 document.body.appendChild(holder);
                 const f = document.createElement('iframe');
                 f.setAttribute('sandbox', 'allow-same-origin');
                 document.body.appendChild(f);
                 f.srcdoc = frame.html;
                 window.__mirrorFrame = f;
               }""",
            frame,
        )
        await mirror.wait_for_timeout(400)

        # Did the engine turn the declarative template into a real shadow root?
        shadow_info = await mirror.evaluate(
            """() => {
                 const f = window.__mirrorFrame;
                 const d = f.contentDocument;
                 const host = d && d.getElementById('host');
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
                 const d = window.__mirrorFrame.contentDocument;
                 const box = d.getElementById('box');
                 const cs = d.defaultView.getComputedStyle(box);
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
                 const d = window.__mirrorFrame.contentDocument;
                 const s = d.getElementById('svg');
                 const c = d.getElementById('c');
                 return { svg: s && s.namespaceURI, circle: c && c.namespaceURI };
               }"""
        )
        check("svg namespace preserved",
              svg_ns["svg"] == "http://www.w3.org/2000/svg", str(svg_ns))
        check("svg child namespace preserved",
              svg_ns["circle"] == "http://www.w3.org/2000/svg", str(svg_ns))

        # ------------------------------------------------ live mutation
        messages.clear()
        await src.evaluate("document.querySelector('#list').appendChild(Object.assign(document.createElement('li'), {textContent:'b'}))")
        await src.wait_for_timeout(400)

        muts = [m for m in messages if m.get("kind") == "mut"]
        ops = [o for m in muts for o in m.get("ops", [])]
        check("mutation streamed from the real page", len(ops) > 0,
              "kinds: " + str([m.get("kind") for m in messages]))
        check("it was a granular add, not a subtree replace",
              any(o["op"] == "add" for o in ops), str([o["op"] for o in ops]))

        # Apply it in the mirror with the shipped applier and check convergence.
        if ops:
            await mirror.evaluate(
                """(ops) => {
                   const d = window.__mirrorFrame.contentDocument;
                   const ids = new Map();
                   window.adopt(d, ids);
                   window.applyOps({ doc: d, ids }, ops);
                 }""",
                ops,
            )
            items = await mirror.evaluate(
                "() => [...window.__mirrorFrame.contentDocument.querySelectorAll('#list li')]"
                ".map(n => n.textContent)"
            )
            check("added node landed in the real mirror", items == ["a", "b"], str(items))

        # ------------------------------------ bare text-node replacement
        # This is the bug class that caused "the dom is not updating": a
        # childList mutation whose added/removed nodes are TEXT_NODE was
        # skipped entirely, so the mirror froze. Regression-pin it here, in a
        # real engine, because it is the exact symptom the user reported.
        # (a) characterData: editing a text node in place.
        messages.clear()
        await src.evaluate("document.querySelector('#list li').firstChild.nodeValue = 'CHANGED'")
        await src.wait_for_timeout(400)
        cd_ops = [o for m in messages if m.get("kind") == "mut" for o in m.get("ops", [])]
        check("in-place text edit produces ops", len(cd_ops) > 0, "got: " + str(cd_ops))
        check("in-place text edit is addressed by id",
              any(o["op"] in ("text", "children") for o in cd_ops),
              str([o["op"] for o in cd_ops]))

        # (b) childList whose added/removed nodes are TEXT_NODE. This is the
        # distinct mutation shape: `li.textContent = x` removes the old text
        # node and inserts a new one. Element-only filters miss it entirely,
        # which is what froze the mirror. It must be exercised separately from
        # (a) because a MutationObserver reports it as a different record type.
        messages.clear()
        await src.evaluate("document.querySelector('#list li').textContent = 'REPLACED'")
        await src.wait_for_timeout(400)
        cl_ops = [o for m in messages if m.get("kind") == "mut" for o in m.get("ops", [])]
        check("text-node replacement produces ops", len(cl_ops) > 0, "got: " + str(cl_ops))
        check("text-node replacement is not silently dropped",
              any(o["op"] in ("text", "children") for o in cl_ops),
              str([o["op"] for o in cl_ops]))

        # ------------------------------------ mutation inside a shadow root
        # A shadow root created by a script at runtime is never reported by a
        # MutationObserver, so discovery depends on the sweep. Prove a change
        # *inside* one reaches the mirror.
        messages.clear()
        await src.evaluate("document.getElementById('host').shadowRoot.querySelector('#inner').textContent = 'Sign Out'")
        await src.wait_for_timeout(600)
        shadow_ops = [o for m in messages if m.get("kind") == "mut" for o in m.get("ops", [])]
        check("mutation inside a runtime shadow root is observed",
              any("Sign Out" in str(o.get("text") or o.get("html") or "") for o in shadow_ops),
              str(shadow_ops)[:200])

        # ------------------------------------ drift must resync, not wipe
        # A text op whose index no longer matches must be refused. Applying it
        # the old way replaced the parent's whole child list with one string.
        resync = await mirror.evaluate(
            """(ops) => {
                 const d = window.__mirrorFrame.contentDocument;
                 const ids = new Map();
                 window.adopt(d, ids);
                 const boxId = [...ids].find(([, n]) => n.nodeType === 1 && n.id === 'box');
                 const bad = [{ op: 'text', parent: boxId[0], index: 99, text: 'WIPED' }];
                 return { applied: window.applyOps({ doc: d, ids }, bad) !== false,
                          text: d.getElementById('box').textContent };
               }"""
        )
        check("mis-indexed text op is refused", resync["applied"] is False, str(resync))
        check("mis-indexed text op leaves the subtree intact",
              resync["text"] == "styled", str(resync))

        # ------------------------------- a change must not overtake its document
        # The first capture is a snapshot, and the changes that follow it are
        # delivered as ops. If an op were emitted while the snapshot was
        # still being taken, it would arrive before the document it belongs
        # to and be dropped by the mirror.
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

        # The agent emits through __rbEmit; record the order of what it sends.
        await slow.evaluate(
            """() => {
                 window.__order = [];
                 const real = window.__rbEmit;
                 window.__rbEmit = (m) => { window.__order.push(m.kind); return real(m); };
               }""")

        # Started but not awaited: the capture is in the middle of its frame
        # timeout, and the change has to happen while it is.
        await slow.evaluate("() => { window.__rbResync(); }")
        await slow.wait_for_timeout(120)
        await slow.evaluate(
            "() => { const d = document.createElement('div');"
            " d.id = 'during'; document.body.appendChild(d); }"
        )
        # Long enough for the capture to finish and the change to be sent.
        await slow.wait_for_timeout(2000)

        order = await slow.evaluate("() => window.__order")
        doc_at = next((i for i, k in enumerate(order) if k == "doc"), None)
        mut_at = next((i for i, k in enumerate(order) if k == "mut"), None)
        check("a change made during a capture is delivered",
              mut_at is not None, str(order))
        check("the change does not arrive before the document it belongs to",
              doc_at is not None and mut_at is not None and doc_at < mut_at,
              "doc at %r, mut at %r, order %r" % (doc_at, mut_at, order))

        # A capture asked for while another one is still running. The running
        # one read the document before the request, so it may already be out
        # of date - dropping the request would leave the mirror stale with
        # nothing to show for it.
        await slow.evaluate("() => { window.__order = []; }")
        await slow.evaluate("() => { window.__rbResync(); }")
        await slow.wait_for_timeout(150)
        await slow.evaluate("() => { window.__rbResync(); }")
        await slow.wait_for_timeout(3000)
        order = await slow.evaluate("() => window.__order")
        check("a capture asked for during a capture is not lost",
              order.count("doc") >= 2, str(order))

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
