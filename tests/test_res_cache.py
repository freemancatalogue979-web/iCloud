"""The /res proxy cache.

Run with: .venv/bin/python tests/test_res_cache.py   (or `npm test`)

The cache holds two behaviours that are easy to get subtly wrong: least-recently
-used eviction, and collapsing concurrent requests for the same asset into one
upstream fetch. A bug in either is invisible until a page with a few hundred
assets loads slowly or a mirror serves stale bytes.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
        print("FAIL " + name + (("  " + detail) if detail else ""))


class StubResponse:
    def __init__(self, body, content_type):
        self._body = body
        self.headers = {"content-type": content_type}

    async def body(self):
        return self._body


class StubCtx:
    """Stands in for Playwright's BrowserContext, counting upstream fetches."""

    def __init__(self, table):
        self.table = table
        self.calls = []
        outer = self

        class Request:
            async def get(self, url):
                outer.calls.append(url)
                await asyncio.sleep(0.02)  # long enough for callers to pile up
                entry = outer.table[url]
                if isinstance(entry, Exception):
                    raise entry
                body, content_type = entry
                return StubResponse(body, content_type)

        self.request = Request()


def reset(table):
    server._res_cache.clear()
    server._res_inflight.clear()
    ctx = StubCtx(table)
    server.ctx = ctx
    return ctx


async def main():
    saved_ctx = server.ctx
    try:
        # ---- a repeat request is served from the cache -------------------
        ctx = reset({"https://cdn.test/a.js": (b"console.log(1)", "application/javascript")})
        first = await server.proxy("https://cdn.test/a.js")
        second = await server.proxy("https://cdn.test/a.js")
        check("second request does not hit the network", len(ctx.calls) == 1, str(ctx.calls))
        check("both responses carry the body",
              first.body == second.body == b"console.log(1)", repr(second.body))
        check("content type preserved", first.media_type == "application/javascript",
              str(first.media_type))

        # ---- concurrent requests collapse into one fetch -----------------
        ctx = reset({"https://cdn.test/font.woff2": (b"FONT", "font/woff2")})
        results = await asyncio.gather(*[server.proxy("https://cdn.test/font.woff2") for _ in range(8)])
        check("8 simultaneous requests -> 1 upstream fetch", len(ctx.calls) == 1,
              str(len(ctx.calls)) + " calls")
        check("every waiter got the body", all(r.body == b"FONT" for r in results))
        check("no in-flight entry left behind", not server._res_inflight,
              str(list(server._res_inflight)))

        # ---- CSS is rewritten once, and the rewrite is what gets cached ---
        css_url = "https://cdn.test/site/main.css"
        ctx = reset({css_url: (b".a{background:url(../img/bg.png)}", "text/css")})
        r1 = await server.proxy(css_url)
        r2 = await server.proxy(css_url)
        check("css url() rewritten", b"/res?url=" in r1.body, r1.body.decode())
        check("css fetched once", len(ctx.calls) == 1, str(ctx.calls))
        check("cached css is already rewritten", r2.body == r1.body)

        # ---- a failed fetch is not cached as a success -------------------
        err_url = "https://cdn.test/gone.js"
        ctx = reset({err_url: RuntimeError("net::ERR_NAME_NOT_RESOLVED")})
        r = await server.proxy(err_url)
        check("failure returns 502", r.status_code == 502, str(r.status_code))
        check("failure is not cached", err_url not in server._res_cache)
        check("failure leaves no in-flight entry", not server._res_inflight)
        # and a retry is allowed to succeed
        ctx.table[err_url] = (b"back", "application/javascript")
        r2 = await server.proxy(err_url)
        check("retry after failure succeeds", r2.body == b"back", repr(r2.body))

        # ---- concurrent failure does not poison the waiters --------------
        boom = "https://cdn.test/boom.js"
        ctx = reset({boom: RuntimeError("target closed")})
        results = await asyncio.gather(*[server.proxy(boom) for _ in range(4)])
        check("all waiters get a clean 502 on a shared failure",
              all(r.status_code == 502 for r in results),
              str([r.status_code for r in results]))

        # ---- LRU eviction -------------------------------------------------
        table = {f"https://cdn.test/f{i}.js": (b"x", "application/javascript") for i in range(30)}
        ctx = reset(table)
        server._RES_MAX_ENTRIES = 10
        try:
            for url in table:
                await server.proxy(url)
            check("cache stays within its bound", len(server._res_cache) <= 10,
                  str(len(server._res_cache)))
            check("the oldest entry was evicted",
                  "https://cdn.test/f0.js" not in server._res_cache)
            check("the newest entry is kept",
                  "https://cdn.test/f29.js" in server._res_cache)
            # Touch f20, making it most-recently-used, then add five entries -
            # enough to evict the five least-recently-used but not the touched
            # one. Adding as many entries as the bound would evict everything
            # and prove nothing.
            await server.proxy("https://cdn.test/f20.js")
            for i in range(30, 35):
                table[f"https://cdn.test/f{i}.js"] = (b"x", "application/javascript")
                await server.proxy(f"https://cdn.test/f{i}.js")
            check("a recently used entry survives eviction",
                  "https://cdn.test/f20.js" in server._res_cache,
                  str(list(server._res_cache)))
            check("the least recently used entry is the one evicted",
                  "https://cdn.test/f21.js" not in server._res_cache,
                  str(list(server._res_cache)))
        finally:
            server._RES_MAX_ENTRIES = 256

        # ---- TTL expiry ---------------------------------------------------
        ctx = reset({"https://cdn.test/t.js": (b"v1", "application/javascript")})
        await server.proxy("https://cdn.test/t.js")
        check("served from cache inside the TTL", len(ctx.calls) == 1)
        url = "https://cdn.test/t.js"
        at, body, ct = server._res_cache[url]
        server._res_cache[url] = (at - server._RES_TTL - 1, body, ct)
        ctx.table[url] = (b"v2", "application/javascript")
        r = await server.proxy(url)
        check("stale entry is re-fetched", r.body == b"v2", repr(r.body))
        check("stale entry counted as a second call", len(ctx.calls) == 2, str(ctx.calls))

        # ---- an oversized body is served but not cached -------------------
        server._RES_MAX_BYTES = 10
        try:
            ctx = reset({"https://cdn.test/big.bin": (b"0123456789abcdef", "application/octet-stream")})
            r = await server.proxy("https://cdn.test/big.bin")
            check("oversized body still served", r.body == b"0123456789abcdef")
            check("oversized body not cached", not server._res_cache)
        finally:
            server._RES_MAX_BYTES = 8 * 1024 * 1024

        # ---- guards still run before any of it ---------------------------
        reset({})
        r = await server.proxy("file:///etc/passwd")
        check("non-http scheme still rejected", r.status_code == 400, str(r.status_code))
        server.ctx = None
        r = await server.proxy("https://cdn.test/a.js")
        check("no browser session still reported", r.status_code == 503, str(r.status_code))
    finally:
        server.ctx = saved_ctx


asyncio.run(main())
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
