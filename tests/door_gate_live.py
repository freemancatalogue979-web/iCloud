"""The door in front of the capture, proved end to end.

The capture is asked for by its address. Nobody has answered the question
yet, so what the server gives is the door - the question on a page of its
own, with nothing of the capture in it. The page asks the question, Google's
widget answers, the server checks the answer with Google, and the capture is
given out after that - and not before.

What is reported is what the server gave first, what it gave after the
answer, and where the visitor ended up.

Run with: xvfb-run -a /root/iCloud/.venv/bin/python /tmp/door_gate.py <origin>
"""

import asyncio
import sys
import time

ORIGIN = (sys.argv[1] if len(sys.argv) > 1 else
          "https://172.105.85.250").rstrip("/")

UID = "the-user"
SID = "gate-probe-" + str(int(time.time()))

# What a capture looks like when it is in the page: the saved page is a
# megabyte of iCloud, and none of it is in the door.
MARK = "icloud.com"


async def main():
    import urllib.request

    from playwright.async_api import async_playwright

    # First, without a browser: what does the server give to somebody who
    # has the names and no answer?
    asked = f"{ORIGIN}/src/1.html?uid={UID}&sid={SID}"
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler())
    try:
        import ssl
        opener.add_context = None
    except Exception:
        pass
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context))
        with opener.open(asked, timeout=30) as answer:
            first = answer.read().decode("utf-8", "replace")
            print("--- what the server gave first")
            print("    bytes   :", len(first))
            print("    the door:", "The question at the door" in first)
            print("    the key :", "icloud-captcha-sitekey" in first)
            print("    capture :", MARK in first)
    except Exception as exc:
        print("--- what the server gave first: failed:", exc)
        first = ""

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

        def on_request(request):
            url = request.url
            if "/challenge/state" in url or "/start" in url:
                asks.append(url.replace(ORIGIN, "")[:80])

        page.on("request", on_request)

        await page.goto(asked, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(40000)

        body = await page.evaluate("() => document.documentElement.outerHTML")

        print("--- what the visitor asked for")
        for one in asks:
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
        print("    capture :", MARK in body)
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
