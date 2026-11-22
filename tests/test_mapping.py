"""The parts of server.py that no browser is needed to check.

Run with: python tests/test_mapping.py   (or `npm test`)

Three things are pinned here:

  * a captured page is served byte for byte - nothing added to it, nothing
    taken out of it, its images served next to it;
  * a field in the capture maps onto the element with the same id in the real
    browser, in whichever frame of whichever page that element is; and
  * the app waits for the sign-in form before it shows the capture.

The browser is faked: a frame is an object that knows which fields it has and
records what was written to it. That is the whole surface server.py uses, so
this runs without Playwright, uvicorn or a network. The route functions are the
shipped ones.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from starlette.requests import Request  # noqa: E402

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + str(detail)) if detail else ""))


# --------------------------------------------------------------------------
# The browser, in as much of it as the mapping touches
# --------------------------------------------------------------------------

class FakeFrame:
    """A frame of the real page: the fields it has, and what was done to it."""

    def __init__(self, fields=(), raises=False):
        self.fields = set(fields)
        self.raises = raises
        self.log = []

    async def evaluate(self, script, argument):
        if self.raises:
            raise RuntimeError("frame detached")

        if script is server._FIND:
            return argument.lstrip("#") in self.fields

        if script is server._SET:
            selector, value = argument
            if selector.lstrip("#") not in self.fields:
                return False
            self.log.append(("set", selector, value))
            return True

        if script is server._CLICK:
            if argument.lstrip("#") not in self.fields:
                return False
            self.log.append(("click", argument))
            return True

        raise AssertionError("unexpected script")


class FakePage:
    def __init__(self, frames):
        self.frames = list(frames)


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)


def browser(*pages):
    """Point the mapping at a browser made of FakePages."""
    server.ctx = FakeContext(pages)
    server.page = pages[0] if pages else None
    return server.ctx


def close_browser():
    server.ctx = None
    server.page = None


def status_of(result):
    """A dict return value means 200: FastAPI only sees a payload."""
    return 200 if not hasattr(result, "status_code") else result.status_code


def answer(result):
    """A route that returns a dict has been through FastAPI's serialiser here,
    so it is already the payload; a plain Response still has to be decoded."""
    return json.loads(result.body) if hasattr(result, "body") else result


def make_request(payload, path="/map"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    done = False

    async def receive():
        nonlocal done
        if done:
            return {"type": "http.disconnect"}
        done = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": path, "headers": []}, receive
    )


async def main():
    # ---- what a field maps to -------------------------------------------
    check("a field maps onto the id it was captured with",
          server.field_selector("account_name_text_field") == "#account_name_text_field")
    check("a password field maps the same way",
          server.field_selector("password_text_field") == "#password_text_field")
    for bad, why in [
        ("", "an empty field"),
        ("1account", "a field starting with a digit"),
        ("account name", "a field with a space"),
        ("account/name", "a field with a slash"),
        ("../account", "a path"),
        ("account" + "x" * 200, "an enormous field"),
        (None, "a null field"),
        (7, "a number"),
    ]:
        check(f"{why} is refused", server.field_selector(bad) == "", bad)

    # ---- where the field is looked for ----------------------------------
    top = FakeFrame()
    widget = FakeFrame({"account_name_text_field"})
    second_tab = FakeFrame({"account_name_text_field"})
    browser(FakePage([top, widget]), FakePage([second_tab]))

    check("every frame of every open page is looked at",
          len(server.live_frames()) == 3, len(server.live_frames()))
    check("the field is found in a widget frame, not only the top document",
          await server.field_is_there("#account_name_text_field"))
    check("a field nobody has is not found",
          not await server.field_is_there("#password_text_field"))

    close_browser()
    check("with no browser open there are no frames", server.live_frames() == [])
    check("and nothing is found", not await server.field_is_there("#account_name_text_field"))

    # ---- writing into the real page --------------------------------------
    top = FakeFrame()
    widget = FakeFrame({"account_name_text_field", "sign-in"})
    browser(FakePage([top, widget]))

    check("a typed value is written into the frame that has the field",
          await server.write_field("#account_name_text_field", "someone@example.com"))
    check("the field received the value",
          widget.log == [("set", "#account_name_text_field", "someone@example.com")],
          widget.log)
    check("a frame without the field is left alone", top.log == [], top.log)
    check("typing into a field nobody has goes nowhere",
          not await server.write_field("#nope_text_field", "x") and
          widget.log[1:] == [], widget.log)

    check("a click is mapped onto the same id",
          await server.click_field("#sign-in"))
    check("the button was clicked, not typed into",
          widget.log[-1] == ("click", "#sign-in"), widget.log)
    check("a click on something that is not there goes nowhere",
          not await server.click_field("#create-link"))

    # A frame that is navigating raises; the rest still get their turn.
    broken = FakeFrame(raises=True)
    widget = FakeFrame({"account_name_text_field"})
    browser(FakePage([broken, widget]))
    check("a frame that raises does not stop the search",
          await server.write_field("#account_name_text_field", "kept going"))
    check("the value still landed", widget.log == [
        ("set", "#account_name_text_field", "kept going")], widget.log)

    # ---- waiting for the sign-in form ------------------------------------
    late = FakeFrame()

    async def appears():
        await asyncio.sleep(0.1)
        late.fields.add("account_name_text_field")

    browser(FakePage([late]))
    task = asyncio.create_task(appears())
    check("the wait ends as much as the form appears, not before",
          await server.wait_for_form("#account_name_text_field", timeout=5.0))
    await task

    empty = FakeFrame()
    browser(FakePage([empty]))
    check("a form that never appears times out instead of hanging",
          not await server.wait_for_form("#account_name_text_field", timeout=0.2))
    close_browser()

    # ---- POST /map -------------------------------------------------------
    widget = FakeFrame({"account_name_text_field", "sign-in", "empty"})
    browser(FakePage([FakeFrame(), widget]))

    response = await server.map_to_browser(
        make_request({"field": "account_name_text_field", "value": "someone@example.com"})
    )
    body = answer(response)
    check("a typed value is accepted", status_of(response) == 200, response)
    check("it reports what it mapped and that it landed",
          body == {"ok": True, "field": "account_name_text_field"}, body)
    check("and the real field has it",
          widget.log[-1] == ("set", "#account_name_text_field", "someone@example.com"),
          widget.log)

    response = await server.map_to_browser(
        make_request({"field": "sign-in", "click": True})
    )
    check("a click is accepted and mapped",
          status_of(response) == 200 and
          widget.log[-1] == ("click", "#sign-in"), (status_of(response), widget.log))

    response = await server.map_to_browser(
        make_request({"field": "sign-in", "click": True, "value": "ignored"})
    )
    check("a click is not also typed into",
          status_of(response) == 200 and widget.log[-1] == ("click", "#sign-in"),
          (status_of(response), widget.log))

    response = await server.map_to_browser(
        make_request({"field": "account_name_text_field", "value": "x" * 20000})
    )
    check("an enormous value is cut down",
          len(widget.log[-1][2]) == 10000, len(widget.log[-1][2]))

    response = await server.map_to_browser(
        make_request({"field": "empty", "value": 42})
    )
    check("a value that is not a string is written as nothing",
          status_of(response) == 200 and widget.log[-1] == ("set", "#empty", ""),
          widget.log[-1:])

    for payload, why in [
        (b"not json", "malformed json"),
        ([1, 2, 3], "a list instead of a message"),
        ({"value": "no field"}, "a message with no field"),
        ({"field": "../etc/passwd", "value": "x"}, "a field that is a path"),
        ({"field": "", "value": "x"}, "an empty field"),
    ]:
        response = await server.map_to_browser(make_request(payload))
        check(f"{why} is refused", status_of(response) == 400,
              (why, status_of(response)))

    close_browser()
    response = await server.map_to_browser(
        make_request({"field": "account_name_text_field", "value": "nobody home"})
    )
    check("typing with no browser open is not an error, it just maps nowhere",
          status_of(response) == 200 and
          answer(response)["ok"] is False, response)

    # ---- serving src/ ----------------------------------------------------
    root = Path(tempfile.mkdtemp()).resolve()
    capture = (
        "<!DOCTYPE html><html><head>"
        "<title>Sign in to iCloud</title>"
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
        '<link rel="stylesheet" href="1_files/site.css">'
        "</head><body>"
        "<script>window.__ran = true;</script>"
        '<img src="1_files/apple.png" alt="">'
        '<input id="account_name_text_field">'
        "</body></html>"
    )
    (root / "1.html").write_text(capture, encoding="utf-8")
    (root / "2.html").write_text("<html><body>two</body></html>", encoding="utf-8")
    assets = root / "1_files"
    assets.mkdir()
    (assets / "site.css").write_text("body { color: red; }", encoding="utf-8")
    png = bytes.fromhex("89504e470d0a1a0a") + b"\x00\x01"
    (assets / "apple.png").write_bytes(png)

    elsewhere = Path(tempfile.mkdtemp()).resolve()
    (elsewhere / "secret.html").write_text("TOP SECRET", encoding="utf-8")

    server.SRC = root

    response = await server.captured("1.html")
    served = response.body.decode()
    check("a captured page is served as HTML",
          response.status_code == 200 and response.media_type.startswith("text/html"),
          (response.status_code, response.media_type))
    check("it is the file, byte for byte - nothing added",
          served == capture, served[:300])
    check("no script is injected", "/sync.js" not in served and "/map.js" not in served)
    check("its own script, policy and meta survive",
          "window.__ran" in served and
          'content="default-src \'none\'"' in served and
          'name="mobile-web-app-capable"' not in served, served[:300])
    check("the capture is not cached",
          response.headers.get("cache-control") == "no-store")

    asset = await server.captured("1_files/site.css")
    check("a stylesheet next to the capture is served with its type",
          asset.status_code == 200 and asset.media_type == "text/css" and
          asset.body == b"body { color: red; }", asset.media_type)

    image = await server.captured("1_files/apple.png")
    check("an image next to the capture is served byte for byte",
          image.status_code == 200 and image.media_type == "image/png" and
          image.body == png, (image.media_type, image.body[:8]))

    listing = await server.captured("")
    check("the folder lists the captures",
          listing.status_code == 200 and "1.html" in listing.body.decode() and
          "2.html" in listing.body.decode())

    missing = await server.captured("nope.html")
    check("a missing capture says where to put it",
          missing.status_code == 404 and "src/nope.html" in missing.body.decode() and
          "/sync.js" not in missing.body.decode(), missing.status_code)

    traversal = await server.captured("../" + elsewhere.name + "/secret.html")
    check("nothing outside src/ can be read",
          traversal.status_code == 404 and "TOP SECRET" not in traversal.body.decode(),
          traversal.status_code)

    # ---- the app ---------------------------------------------------------
    app = await server.index()
    check("/ serves the app page", str(app.path).endswith("client.html"), app.path)
    check("and it is not cached, so an edit shows up",
          app.headers.get("cache-control") == "no-store")

    server.state["status"] = "done"
    still_app = await server.index()
    check("/ is still the app once the browser is up - a reload is a reload",
          str(still_app.path).endswith("client.html"), still_app.path)
    server.state["status"] = "idle"

    live = await server.status()
    check("status names the page to show",
          live.get("page") == "/src/1.html", live)
    check("status says whether the form is up",
          "form_up" in live and live["form_up"] is False, live)

    bridge = await server.map_js()
    check("/map.js serves the bridge", str(bridge.path).endswith("map.js"), bridge.path)

    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
