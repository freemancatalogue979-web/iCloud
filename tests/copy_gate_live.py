"""The door on the copy, proved end to end.

The copy lives on a Worker. A capture asked for there is not handed out: the
Worker asks the server whether the pass it is holding is good, and a visitor
without one is given the door - the question, on a page of its own, with
nothing of the capture in it. The page asks the question at the server, the
server checks it, and the capture is handed out after that.

What is reported is what the Worker gave first, what the visitor's browser
asked for, and what it ended up with.

Run with: xvfb-run -a /root/iCloud/.venv/bin/python /tmp/copy_gate.py <origin>
"""

import asyncio
import sys
import time

ORIGIN = (sys.argv[1] if len(sys.argv) > 1 else
          "https://plain-waterfall-36c4.obamaojie.workers.dev").rstrip("/")

UID = "the-user"
SID = "copy-probe-" + str(int(time.time()))


async def main():
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(ignore_https_errors=True)

        # This browser is the one the capture is for.
        await context.add_init_script(
            "try {\n"
            "  window.localStorage.setItem('icloud-user', '" + UID + "');\n"
            "  window.localStorage.setItem('icloud-visitor', '" + SID + "');\n"
            "} catch (err) { }")

        page = await context.new_page()
        asks = []
        problems = []

        def on_request(request):
            url = request.url
            if "/challenge/state" in url or "/start" in url:
                asks.append(url.split("?")[0])

        def on_failed(request):
            problems.append(request.url[:100])

        page.on("request", on_request)
        page.on("requestfailed", on_failed)

        await page.goto(f"{ORIGIN}/src/1.html?uid={UID}&sid={SID}",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(45000)

        body = await page.evaluate("() => document.documentElement.outerHTML")

        print("--- what the visitor asked for")
        for one in asks:
            print("   ", one)
        print("--- what did not go through")
        for one in problems[:5]:
            print("   ", one)
        print("--- where the visitor is")
        print("   ", page.url)
        print("--- the door")
        print("    state   :", await page.evaluate(
            "() => (window.__icloudDoor || {}).state || 'none'"))
        print("    why     :", await page.evaluate(
            "() => (window.__icloudDoor || {}).why || ''"))
        print("--- the page now")
        print("    bytes   :", len(body))
        print("    capture :", "icloud.com" in body)
        print("    engine  :", await page.evaluate("!!window.__icloudSync"))
        print("    refused :", await page.evaluate("!!window.__icloudRefused"))
        print("--- the pass the browser was given")
        for cookie in await context.cookies():
            if cookie["name"] == "icloud_pass":
                print("    icloud_pass:", cookie["value"][:40], "...")
        await browser.close()
    finally:
        await playwright.stop()


asyncio.run(main())
