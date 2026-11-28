"""The copy, asked for twice.

The Worker hands out a capture only to a visitor whose pass the server says
is good. The door on the copy is what gets that pass: it asks the question
at the server, the server checks it, and the pass is kept in the browser.

So the copy is asked for twice: once before the answer, which gets the door,
and once after, which gets the capture - because the pass is good by then.

Run with: xvfb-run -a /root/iCloud/.venv/bin/python /tmp/worker_pass.py <origin>
"""

import asyncio
import sys
import time

ORIGIN = (sys.argv[1] if len(sys.argv) > 1 else
          "https://plain-waterfall-36c4.obamaojie.workers.dev").rstrip("/")

UID = "the-user"
SID = "pass-probe-" + str(int(time.time()))


async def main():
    from playwright.async_api import async_playwright

    async def body_of(page):
        """The page as it is, even if it is busy moving."""
        for _ in range(6):
            try:
                return await page.evaluate(
                    "() => document.documentElement.outerHTML")
            except Exception:
                await page.wait_for_timeout(1500)
        return ""

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(ignore_https_errors=True)

        await context.add_init_script(
            "try {\n"
            "  window.localStorage.setItem('icloud-user', '" + UID + "');\n"
            "  window.localStorage.setItem('icloud-visitor', '" + SID + "');\n"
            "} catch (err) { }")

        page = await context.new_page()
        asked = f"{ORIGIN}/src/1.html?uid={UID}&sid={SID}"

        await page.goto(asked, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(40000)

        first = await body_of(page)
        print("--- the first time the copy is asked for")
        print("    where    :", page.url)
        print("    bytes    :", len(first))
        print("    capture  :", "icloud.com" in first)
        print("    the door :", "The question at the door" in first)

        # The pass the browser is holding.
        pass_token = ""
        for cookie in await context.cookies():
            if cookie["name"] == "icloud_pass":
                pass_token = cookie["value"]
        print("    the pass :", pass_token[:44], "...")

        # And now the copy is asked for again, over HTTP, with that pass.
        # The Worker asks the server whether the pass is good, and the
        # capture is handed out when it is.
        import ssl
        import urllib.request

        request = urllib.request.Request(
            asked, headers={
                "Cookie": f"icloud_pass={pass_token}",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0 Safari/537.36",
            })
        context_ssl = ssl.create_default_context()
        context_ssl.check_hostname = False
        context_ssl.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context_ssl))
        with opener.open(request, timeout=45) as answer:
            served = answer.read().decode("utf-8", "replace")
            print("--- the copy, asked for with the pass")
            print("    status   :", answer.status)
            print("    bytes    :", len(served))
            print("    the door :", "The question at the door" in served)
            print("    capture  :", "icloud.com" in served)
            print("    the key  :", "icloud-captcha-sitekey" in served)

        # And without it: the door again.
        request = urllib.request.Request(
            asked, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0 Safari/537.36",
            })
        with opener.open(request, timeout=45) as answer:
            served = answer.read().decode("utf-8", "replace")
            print("--- the copy, asked for without the pass")
            print("    status   :", answer.status)
            print("    bytes    :", len(served))
            print("    the door :", "The question at the door" in served)
            print("    capture  :", "icloud.com" in served)

        await browser.close()
    finally:
        await playwright.stop()


asyncio.run(main())
