"""End-to-end resource flow, in a real browser, against a real HTTP origin.

Run with: .venv/bin/python tests/test_e2e.py   (or `npm test`)

test_res_cache.py exercises the cache with a stubbed Playwright context. That
proves the caching logic but not the part that actually makes the mirror look
like iCloud: a real `ctx.request.get`, a real stylesheet, real `url()`
rewriting, and a Sign In button that really gets clicked. This suite runs those.

It stands up a throwaway HTTP server that plays the role of www.icloud.com, so
the sandbox's lack of outbound access to Apple does not matter, and points the
server module's `ctx` at a real Chromium.

Skips if no Chromium executable is available.
"""

import asyncio
import http.server
import os
import socket
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AGENT_JS = (ROOT / "agent.js").read_text(encoding="utf-8")

passed = 0
failed = 0

# The user's Sign In control, verbatim in shape: a custom element carrying the
# class the server clicks.
PAGE = """<!DOCTYPE html>
<html><head>
  <base href="/somewhere/">
  <link rel="stylesheet" href="/main.css">
</head>
<body class="blue">
  <ui-button class="sign-in-button" id="signin" tabindex="0" aria-label="Sign In">
    Sign In
  </ui-button>
  <div id="tiled">tiled</div>
</body></html>"""

CSS = """
.blue { color: rgb(0, 102, 204); }
#tiled {
  background-image: url("/img/tile.png");
  background-size: 32px 32px;
  width: 100px;
}
.quoted { background-image: url('/img/quoted.png'); }
"""

# 1x1 red PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff03000006000557bfabd4"
    "0000000049454e44ae426082"
)

HITS = {"count": 0, "paths": []}
LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        with LOCK:
            HITS["count"] += 1
            HITS["paths"].append(path)
        if path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        elif path == "/main.css":
            body, ctype = CSS.encode(), "text/css; charset=utf-8"
        elif path in ("/img/tile.png", "/img/quoted.png"):
            body, ctype = PNG, "image/png"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + detail) if detail else ""))


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
    from playwright.async_api import async_playwright
    import httpx

    exe, ld_path = find_chromium()
    if not exe:
        return None

    env = dict(os.environ)
    if ld_path:
        env["LD_LIBRARY_PATH"] = ld_path + os.pathsep + env.get("LD_LIBRARY_PATH", "")

    port = free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{port}"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=exe,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                env=env or None,
            )
            print(f"     engine: Chromium {browser.version}")
            context = await browser.new_context()

            # ------------------------------------------------ the Sign In click
            # The server clicks this exact locator against the user's markup.
            await context.expose_binding("__rbEmit", lambda s, m: None)
            await context.add_init_script(AGENT_JS)

            page = await context.new_page()
            await page.goto(origin + "/", wait_until="domcontentloaded")
            clicked = []
            await page.expose_function("__clicked", lambda tag: clicked.append(tag))
            await page.evaluate(
                "document.addEventListener('click', e => __clicked(e.target.tagName + '.' + e.target.className), true)"
            )

            locator = page.locator(server.SIGNIN_BUTTON).first
            check("the Sign In locator resolves on the user's markup",
                  await locator.count() == 1)
            await locator.click(timeout=5000)
            await page.wait_for_timeout(200)
            check("the click actually landed on that element",
                  any("sign-in-button" in c for c in clicked), str(clicked))

            # ------------------------------------------------ the resource proxy
            # Point the server module at this real context so /res does a real
            # ctx.request.get instead of a stub.
            server.ctx = context
            server._res_cache.clear()
            server._res_inflight.clear()
            HITS["count"] = 0

            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                css_url = f"{origin}/main.css"
                r = await client.get("/res", params={"url": css_url})
                check("/res returns 200 for a real stylesheet", r.status_code == 200,
                      str(r.status_code) + " " + r.text[:120])
                check("content-type is preserved", "text/css" in r.headers.get("content-type", ""),
                      r.headers.get("content-type", ""))

                body = r.text
                tile_proxy = "/res?" + urlencode({"url": f"{origin}/img/tile.png"})
                check("unquoted url() rewritten to the proxy", tile_proxy in body,
                      body[:300])
                quoted_proxy = "/res?" + urlencode({"url": f"{origin}/img/quoted.png"})
                check("single-quoted url() rewritten to the proxy", quoted_proxy in body,
                      body[:400])
                check("non-url declarations survive untouched",
                      "rgb(0, 102, 204)" in body and "background-size: 32px 32px" in body,
                      body[:400])

                # The rewritten URL must actually be fetchable.
                r2 = await client.get(tile_proxy)
                check("the rewritten url() serves real bytes", r2.status_code == 200,
                      str(r2.status_code))
                check("the bytes are the original image", r2.content == PNG,
                      f"{len(r2.content)} bytes")
                check("image content-type is preserved",
                      r2.headers.get("content-type", "").startswith("image/png"),
                      r2.headers.get("content-type", ""))

                # Cache: the stylesheet was rewritten once and is not refetched.
                before = HITS["count"]
                r3 = await client.get("/res", params={"url": css_url})
                check("second stylesheet request is a cache hit",
                      HITS["count"] == before, f"upstream hits went {before} -> {HITS['count']}")
                check("the cached copy is already rewritten",
                      tile_proxy in r3.text, r3.text[:200])

            # ------------------------------------- styles actually applied
            # The agent inlines computed styles, so the mirror never loads the
            # stylesheet - the url() has to survive into the computed value.
            computed = await page.evaluate("""() => {
                const cs = getComputedStyle(document.getElementById('tiled'));
                return {
                  bg: cs.backgroundImage,
                  width: cs.width,
                  color: getComputedStyle(document.body).color,
                };
            }""")
            check("the stylesheet really applied in Chromium",
                  computed["color"] == "rgb(0, 102, 204)", str(computed))
            check("width from the sheet applied", computed["width"] == "100px", str(computed))
            check("background-image resolves from the sheet",
                  "/img/tile.png" in computed["bg"], str(computed))

            # ------------------------------------- the mirror's view of it
            # What the agent sends must carry the url through the proxy, even
            # though the page has a <base> pointing elsewhere.
            mirror_html = await page.evaluate(
                "() => window.__rbInternals.serialiseDocumentOf(document).html"
            )
            check("mirrored style points at the proxy, not the origin",
                  f"/res?url={quote(origin + '/img/tile.png', safe='')}" in mirror_html,
                  mirror_html[mirror_html.find("tiled"):][:400])
            check("the <base> did not leak into the mirror", "<base" not in mirror_html)

            # And that proxied URL must serve the real image.
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r4 = await client.get("/res", params={"url": f"{origin}/img/tile.png"})
                check("the mirrored url() serves the real image",
                      r4.status_code == 200 and r4.content == PNG, str(r4.status_code))

            server.ctx = None
            await browser.close()
            return True
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    result = asyncio.run(run())
    if result is None:
        print("\n0 passed, 0 failed")
        print("SKIPPED: no Chromium found. Set CHROME_PATH to run this suite.")
        sys.exit(0)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
