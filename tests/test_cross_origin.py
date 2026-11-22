"""Typing into a cross-origin frame, through the mirror, in a real browser.

Run with: .venv/bin/python tests/test_cross_origin.py   (or `npm test`)

The sign-in form on icloud.com is not in the page. It is a frame on
idmsa.apple.com - a different origin, so the page cannot read it and the
mirror cannot reach into it. Two things have to work for that form to be
usable:

  1. The mirror has to SHOW it. The frame serialises itself and posts its
     document back to its parent, which is the only way across an origin
     boundary.
  2. Typing into it has to work. Click the field in the mirror and the click
     has to land on the real field in the real browser, and what is typed in
     the mirror has to arrive in that field.

This suite stands up two genuine origins - www.icloud.com and
idmsa.apple.com - in a real Chromium, and checks both.

Nothing here is site-specific: the two origins stand in for any page that
puts a form in a frame.

Skips if no Chromium executable is available.
"""

import asyncio
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = 0
failed = 0

# The landing page, with the Sign In control the server clicks.
LANDING = """<!DOCTYPE html>
<html><head><title>iCloud</title></head>
<body>
  <h1>iCloud</h1>
  <ui-button class="sign-in-button" id="signin">Sign In</ui-button>
  <script>
    document.getElementById('signin').addEventListener('click', () => {
      location.href = '/signin';
    });
  </script>
</body></html>"""

# The sign-in page. The form itself is a frame on another origin.
PAGE = """<!DOCTYPE html>
<html><head><title>Sign in - iCloud</title></head>
<body>
  <h1>Sign in</h1>
  <iframe id="signin" src="https://idmsa.apple.com/auth/signin"
          style="width:420px;height:240px;border:0"></iframe>
</body></html>"""

# The form, on the other origin.
FIELD_PAGE = """<!DOCTYPE html>
<html><head><title>Sign in</title></head>
<body style="font-family:sans-serif">
  <form>
    <label for="acct">Apple ID</label>
    <input id="acct" type="text" style="width:280px">
    <label for="pw">Password</label>
    <input id="pw" type="password" style="width:280px">
    <button id="go">Sign In</button>
  </form>
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
    from playwright.async_api import async_playwright

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
        server.state.update(status="idle", error="", browser_open=False)
        server._capture_ready = False
        server._signin_clicked = False
        # Long enough for the cross-origin frame to have loaded and answered.
        server.SETTLE_SECONDS = 1.5

        async with async_playwright() as p:
            real_pw = await p.chromium.launch(
                executable_path=exe,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                env=env or None,
            )
            fake = await p.chromium.launch(
                executable_path=exe,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                env=env or None,
            )
            # Deliberately not 1280x800. The browser window is opened
            # maximised, so the real page is whatever size the screen is, and
            # a click has to land in the same place whatever that is.
            VIEWPORT = {"width": 1600, "height": 900}
            fake_ctx = await fake.new_context(viewport=VIEWPORT)

            def route_handler(r):
                url = r.request.url
                if "idmsa.apple.com" in url:
                    return r.fulfill(status=200, content_type="text/html; charset=utf-8",
                                     body=FIELD_PAGE)
                if "icloud.com" in url:
                    body = PAGE if "/signin" in url else LANDING
                    return r.fulfill(status=200, content_type="text/html; charset=utf-8",
                                     body=body)
                return r.continue_()

            await fake_ctx.route("**/*", route_handler)

            async def fake_launch():
                pg = fake_ctx.pages[0] if fake_ctx.pages else await fake_ctx.new_page()
                return fake_ctx, pg

            server.launch = fake_launch

            async with httpx.AsyncClient(timeout=60) as client:
                # ---------------------------------------------- /start
                r = await client.get(base + "/start")
                check("/start responds 200", r.status_code == 200, str(r.status_code))
                for _ in range(60):
                    st = (await client.get(base + "/status")).json()
                    if st["status"] in ("done", "error"):
                        break
                    await asyncio.sleep(0.25)
                check("the run reaches 'done'", st["status"] == "done",
                      f"{st['status']}: {st.get('error')}")

                # ---------------------------------------------- the capture
                frames = (server.last_doc or {}).get("frames") or []
                check("the capture has frames", len(frames) >= 1, str(len(frames)))
                ids = [f.get("windowId") for f in frames]
                check("the cross-origin frame is in the capture",
                      any(str(i).startswith("0.") for i in ids), str(ids))
                field_frames = [f for f in frames
                                if "acct" in (f.get("html") or "")]
                check("the cross-origin form's document is captured",
                      bool(field_frames), str(ids))
                if field_frames:
                    html = field_frames[0]["html"]
                    check("the captured form carries its ids",
                          "data-rb-nid" in html, html[:140])
                    check("the captured form has the field",
                          'id="acct"' in html, html[:200])

                # ---------------------------------------------- typing in it
                # The real page, and the frame inside it.
                real_page = fake_ctx.pages[0]
                frame = next((f for f in real_page.frames
                              if "idmsa.apple.com" in f.url), None)
                check("the cross-origin frame is really loaded", frame is not None,
                      str([f.url for f in real_page.frames]))
                if frame is None:
                    await fake.close()
                    await real_pw.close()
                    return False

                # Where the field is, in the page's own coordinates.
                box = await real_page.evaluate(
                    """() => { const f = document.getElementById('signin');
                         const r = f.getBoundingClientRect();
                         return { x: r.x, y: r.y, w: r.width, h: r.height }; }"""
                )
                field = await frame.evaluate(
                    """() => { const i = document.getElementById('acct');
                         const r = i.getBoundingClientRect();
                         return { x: r.x, y: r.y, w: r.width, h: r.height }; }"""
                )
                x = box["x"] + field["x"] + field["w"] / 2
                y = box["y"] + field["y"] + field["h"] / 2

                # The mirror sends the position as a fraction of the page.
                fx = x / VIEWPORT["width"]
                fy = y / VIEWPORT["height"]

                # The mirror's click: generic coordinates, no selector.
                r = await client.post(base + "/click", json={"x": fx, "y": fy})
                check("/click reports ok", r.json().get("ok") is True, r.text[:120])
                await asyncio.sleep(0.3)

                focused = await frame.evaluate(
                    """() => document.activeElement && document.activeElement.id"""
                )
                check("the click landed on the field inside the cross-origin frame",
                      focused == "acct", repr(focused))
                check("the click landed on the field at a window size that is not 1280x800",
                      focused == "acct", repr(focused))

                r = await client.post(base + "/keys", json={"text": "someone@icloud.com"})
                check("/keys reports ok", r.json().get("ok") is True, r.text[:120])
                await asyncio.sleep(0.4)

                typed = await frame.evaluate(
                    """() => document.getElementById('acct').value"""
                )
                check("what was typed in the mirror reached the real cross-origin field",
                      typed == "someone@icloud.com", repr(typed))

                # The password field, so nothing about this is specific to
                # the first input on the page.
                pw_field = await frame.evaluate(
                    """() => { const i = document.getElementById('pw');
                         const r = i.getBoundingClientRect();
                         return { x: r.x, y: r.y, w: r.width, h: r.height }; }"""
                )
                px = box["x"] + pw_field["x"] + pw_field["w"] / 2
                py = box["y"] + pw_field["y"] + pw_field["h"] / 2
                await client.post(base + "/click",
                                  json={"x": px / VIEWPORT["width"],
                                        "y": py / VIEWPORT["height"]})
                await asyncio.sleep(0.2)
                await client.post(base + "/keys", json={"text": "hunter2"})
                await asyncio.sleep(0.3)
                pw_typed = await frame.evaluate(
                    """() => document.getElementById('pw').value"""
                )
                check("the second field in the cross-origin frame takes text too",
                      pw_typed == "hunter2", repr(pw_typed))

                # The two fields are different, so the first click did not
                # leave the text somewhere else.
                first = await frame.evaluate(
                    """() => document.getElementById('acct').value"""
                )
                check("the first field kept what was typed in it",
                      first == "someone@icloud.com", repr(first))

                # ------------------------------------ the mirror's own click
                # The last piece: drive the real client - the page the user
                # opens, which loads the captured page as a document of its
                # own - and click inside the cross-origin form. The
                # coordinates the mirror sends have to land on the field in
                # the real browser.
                seen = []

                async def grab(route):
                    # Record the coordinates, and pass the click on to the
                    # server - the mirror's click has to reach the real
                    # browser, not stop here.
                    try:
                        seen.append(route.request.post_data_json)
                    except Exception:
                        seen.append(None)
                    response = await route.fetch()
                    await route.fulfill(response=response)

                viewer = await real_pw.new_page()
                await viewer.route("**/click", grab)
                # The client page loads the captured page, and the captured
                # page loads the cross-origin form inside it.
                await viewer.goto(base + "/")
                await viewer.wait_for_timeout(2500)

                mirror_frame = next(
                    (f for f in viewer.frames if f.parent_frame == viewer.main_frame), None)
                check("the client loaded the captured page as a document",
                      mirror_frame is not None, str([f.url for f in viewer.frames]))
                inner = [f for f in viewer.frames if f.parent_frame == mirror_frame]
                check("the mirror shows the cross-origin form in a frame",
                      bool(inner), str([f.url for f in viewer.frames]))
                if mirror_frame and inner:
                    inner_frame = inner[0]
                    fbox = await mirror_frame.evaluate(
                        """() => { const f = document.querySelector('iframe');
                             const r = f.getBoundingClientRect();
                             return { x: r.x, y: r.y }; }"""
                    )
                    ibox = await inner_frame.evaluate(
                        """() => { const i = document.getElementById('acct');
                             const r = i.getBoundingClientRect();
                             return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; }"""
                    )
                    outer = await viewer.evaluate(
                        """() => { const m = document.getElementById('remote');
                             const r = m.getBoundingClientRect();
                             return { x: r.x, y: r.y, w: r.width, h: r.height }; }"""
                    )
                    metrics = await viewer.evaluate("() => window.mirrorMetrics()")
                    # The mirror lays the captured page out at the size it
                    # was captured and scales it down to fit the window. The
                    # content inside it has to be laid out the same way as
                    # the real page, or a click lands in the wrong place.
                    probe = """() => {
                        const i = document.getElementById('acct');
                        const cs = getComputedStyle(i);
                        const b = document.body.getBoundingClientRect();
                        const lab = document.querySelector('label');
                        const lr = lab.getBoundingClientRect();
                        return { input: [i.getBoundingClientRect().x, i.getBoundingClientRect().width],
                                 font: cs.fontFamily + '/' + cs.fontSize,
                                 body: [b.x, b.width],
                                 label: [lr.x, lr.width],
                                 inner: [window.innerWidth, window.innerHeight] };
                    }"""
                    check("the mirror lays the captured page out at the captured size",
                          metrics["w"] == VIEWPORT["width"],
                          "captured %r, page %r" % (metrics, VIEWPORT))
                    check("the mirror scales the page down to fit",
                          metrics["scale"] < 1, str(metrics))
                    check("the page inside the mirror is laid out like the real one",
                          await inner_frame.evaluate(probe) == await frame.evaluate(probe),
                          str(await inner_frame.evaluate(probe)))

                    # In the viewer's own coordinates. The mirror shows the
                    # page at the size it was captured and scales it down to
                    # fit, so the position in the captured page has to be
                    # scaled on the way out.
                    scale = metrics["scale"]
                    vx = outer["x"] + (fbox["x"] + ibox["x"]) * scale
                    vy = outer["y"] + (fbox["y"] + ibox["y"]) * scale
                    await viewer.mouse.click(vx, vy)
                    await viewer.wait_for_timeout(500)

                    check("the mirror sends a click for a click inside the nested frame",
                          bool(seen), str(seen))
                    if seen:
                        frac = seen[-1] or {}
                        check("the click is sent as a fraction of the page",
                              0 <= frac.get("x", -1) <= 1 and 0 <= frac.get("y", -1) <= 1,
                              str(frac))

                # The focus is somewhere else first, so the mirror's click
                # has to move it. The password field, through the server.
                pw_direct = await frame.evaluate(
                    """() => { const i = document.getElementById('pw');
                         const r = i.getBoundingClientRect();
                         return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; }"""
                )
                await client.post(base + "/click",
                                  json={"x": (box["x"] + pw_direct["x"]) / VIEWPORT["width"],
                                        "y": (box["y"] + pw_direct["y"]) / VIEWPORT["height"]})
                await asyncio.sleep(0.3)
                before = await frame.evaluate(
                    """() => document.activeElement && document.activeElement.id"""
                )
                check("the focus starts on the password field", before == "pw", repr(before))

                # Now the mirror's own click, on the account field.
                await viewer.mouse.click(vx, vy)
                await viewer.wait_for_timeout(800)
                after = await frame.evaluate(
                    """() => document.activeElement && document.activeElement.id"""
                )
                check("the mirror's click moved the focus to the account field",
                      after == "acct", repr(after))

                await client.post(base + "/keys", json={"text": "typed-in-mirror"})
                await asyncio.sleep(0.4)
                typed = await frame.evaluate(
                    """() => document.getElementById('acct').value"""
                )
                check("text typed in the mirror reached the real cross-origin field",
                      typed.endswith("typed-in-mirror"), repr(typed))

            server.page = None
            await fake.close()
            await real_pw.close()
            return True
    finally:
        uv.should_exit = True
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    result = asyncio.run(run())
    if result is None:
        print("\n0 passed, 0 failed")
        print("SKIPPED: no Chromium found. Set CHROME_PATH to run this suite.")
        sys.exit(0)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if result is False or failed else 0)
