"""The server's own flow, against a real Chromium and a real WebSocket.

Run with: .venv/bin/python tests/test_server_flow.py   (or `npm test`)

The other suites exercise agent.js, view.html and /res in isolation. This one
runs `server.start()` itself - the function the user actually triggers by
visiting `/` - and checks the whole chain: launch, install the agent, navigate,
click Sign In, then push the capture and its mutations out over the WebSocket to
a real browser client, and forward input back.

Only two things are substituted, because the sandbox has neither:
  * `launch()` returns a headless context instead of a visible window
    (headless=False needs a display server).
  * `https://www.icloud.com/` is answered by a Playwright route handler
    (there is no outbound access to Apple).

Everything else - `install_agent`, `on_emit`, `broadcast`, `/ws`, `/click`,
`/keys`, `request_resync` - is the shipped code, unmodified.

Skips if no Chromium executable is available.
"""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = 0
failed = 0

FAKE_ICLOUD = """<!DOCTYPE html>
<html><head><title>iCloud</title></head>
<body>
  <ui-button class="sign-in-button" id="signin">Sign In</ui-button>
  <ul id="list"><li>a</li></ul>
  <input id="field" value="">
  <script>
    window.__clicks = [];
    document.addEventListener('click', e => {
      window.__clicks.push({ id: e.target.id || e.target.tagName,
                             x: Math.round(e.clientX), y: Math.round(e.clientY) });
    }, true);
  </script>
</body></html>"""


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + detail) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def find_chromium():
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


async def run():
    import server
    import uvicorn
    import httpx

    exe, ld_path = find_chromium()
    if not exe:
        return None

    env = dict(os.environ)
    if ld_path:
        env["LD_LIBRARY_PATH"] = ld_path + os.pathsep + env.get("LD_LIBRARY_PATH", "")

    port = free_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
    uv = uvicorn.Server(config)
    server_task = asyncio.create_task(uv.serve())
    await asyncio.sleep(1.5)
    base = f"http://127.0.0.1:{port}"

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            real_pw = await p.chromium.launch(
                executable_path=exe,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                env=env or None,
            )
            print(f"     engine: Chromium {real_pw.version}")

            # ---- the only substitution: a headless context that answers
            # icloud.com from a route handler.
            fake = await p.chromium.launch(
                executable_path=exe,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                env=env or None,
            )
            fake_ctx = await fake.new_context()
            await fake_ctx.route(
                "**/*",
                lambda r: r.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=FAKE_ICLOUD,
                )
                if "icloud.com" in r.request.url
                else r.continue_(),
            )

            async def fake_launch():
                pg = fake_ctx.pages[0] if fake_ctx.pages else await fake_ctx.new_page()
                return fake_ctx, pg

            server.launch = fake_launch

            # ------------------------------------------------ /start
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(base + "/start")
                check("/start responds 200", r.status_code == 200, str(r.status_code))

                for _ in range(60):
                    st = (await client.get(base + "/status")).json()
                    if st["status"] in ("done", "error"):
                        break
                    await asyncio.sleep(0.25)

                check("the run reaches 'done'", st["status"] == "done",
                      f"{st['status']}: {st.get('error')}")
                check("browser_open is reported", st["browser_open"] is True, str(st))

                # The Sign In click, through the server's own locator.
                clicks = await fake_ctx.pages[0].evaluate("() => window.__clicks")
                check("Sign In was clicked in the page",
                      any(c["id"] == "signin" for c in clicks), str(clicks))

                check("the agent captured the document", server.last_doc is not None)
                frames = (server.last_doc or {}).get("frames") or []
                check("the capture has frames", len(frames) >= 1, str(len(frames)))
                check("the capture carries ids",
                      "data-rb-nid" in (frames[0]["html"] if frames else ""),
                      (frames[0]["html"][:120] if frames else ""))

                # ---------------------------------------- the WebSocket mirror
                # A real browser connects to the real /ws endpoint.
                viewer = await real_pw.new_page()
                await viewer.set_content("<body></body>")
                await viewer.evaluate(
                    """(url) => {
                        window.__msgs = [];
                        window.__ws = new WebSocket(url);
                        window.__ws.onmessage = e => window.__msgs.push(JSON.parse(e.data));
                    }""",
                    base.replace("http://", "ws://") + "/ws",
                )
                await viewer.wait_for_function("() => window.__ws.readyState === 1", timeout=8000)
                await asyncio.sleep(0.6)

                kinds = await viewer.evaluate("() => window.__msgs.map(m => m.kind)")
                check("the viewer receives the capture over WebSocket",
                      "doc" in kinds, str(kinds))
                check("a late viewer is handed the current document at once",
                      kinds.count("doc") >= 1, str(kinds))

                # ---------------------------------------- mutation delivery
                # "when something updates send it to client" - the actual
                # complaint. A live change must arrive as ops, not a rebuild.
                before = await viewer.evaluate("() => window.__msgs.length")
                await fake_ctx.pages[0].evaluate(
                    "() => { const l = document.getElementById('list');"
                    " const li = document.createElement('li'); li.textContent = 'b';"
                    " l.appendChild(li); }"
                )
                for _ in range(40):
                    if await viewer.evaluate("() => window.__msgs.length") > before:
                        break
                    await asyncio.sleep(0.1)

                new = await viewer.evaluate(
                    "() => window.__msgs.slice(%d).map(m => ({ kind: m.kind, ops: m.ops || [] }))"
                    % before
                )
                muts = [m for m in new if m["kind"] == "mut"]
                check("a live mutation reaches the viewer", len(muts) > 0,
                      str([m["kind"] for m in new]))
                ops = [o for m in muts for o in m["ops"]]
                check("it arrives as granular ops",
                      any(o.get("op") == "add" for o in ops), str([o.get("op") for o in ops]))
                check("the op carries the new content",
                      any("b" in str(o.get("html") or o.get("text") or "") for o in ops),
                      str(ops)[:200])
                doc_ids = set()
                import re as _re

                if frames:
                    doc_ids = set(_re.findall(r'data-rb-nid="([^"]+)"', frames[0]["html"]))
                check("the op addresses an id from the capture",
                      all(o.get("parent") in doc_ids or o.get("id") in doc_ids
                          for o in ops if o.get("parent") or o.get("id")),
                      str(ops)[:200])

                # ---------------------------------------- resync round trip
                before = await viewer.evaluate("() => window.__msgs.length")
                await viewer.evaluate("() => window.__ws.send(JSON.stringify({cmd:'resync'}))")
                for _ in range(40):
                    if await viewer.evaluate("() => window.__msgs.length") > before:
                        break
                    await asyncio.sleep(0.1)
                after = await viewer.evaluate("() => window.__msgs.map(m => m.kind)")
                check("a resync request produces a fresh capture",
                      after.count("doc") > kinds.count("doc"), str(after))

                # ---------------------------------------- input forwarding
                pos = await fake_ctx.pages[0].evaluate(
                    """() => { const r = document.getElementById('field').getBoundingClientRect();
                        return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }; }"""
                )
                r = await client.post(base + "/click", json=pos)
                check("/click reports ok", r.json().get("ok") is True, r.text[:120])
                await asyncio.sleep(0.3)
                clicks = await fake_ctx.pages[0].evaluate("() => window.__clicks")
                check("the click landed on the real page at those coordinates",
                      any(c["id"] == "field" and abs(c["x"] - pos["x"]) < 4
                          and abs(c["y"] - pos["y"]) < 4 for c in clicks),
                      str(clicks) + " expected " + str(pos))

                r = await client.post(base + "/keys", json={"text": "hello"})
                check("/keys reports ok", r.json().get("ok") is True, r.text[:120])
                await asyncio.sleep(0.3)
                typed = await fake_ctx.pages[0].evaluate(
                    "() => document.getElementById('field').value")
                check("the typed text reached the real field", typed == "hello", repr(typed))

                r = await client.post(base + "/key", json={"key": "Tab"})
                check("/key reports ok", r.status_code == 200, r.text[:120])

            server.page = None
            await fake.close()
            await real_pw.close()
            return True
    finally:
        uv.should_exit = True
        await asyncio.sleep(0.5)
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    result = asyncio.run(run())
    if result is None:
        print("\n0 passed, 0 failed")
        print("SKIPPED: no Chromium found. Set CHROME_PATH to run this suite.")
        sys.exit(0)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
