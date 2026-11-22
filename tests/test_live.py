"""The bridge between a capture and the browser the server opened.

Run with: python tests/test_live.py   (or `npm test`)

What is pinned here:

  * what is typed into a capture is written into the browser, in the field
    with the same id, and a button pressed in the capture is pressed in the
    browser,
  * the sign-in form is found even though it is not in the browser's top
    document - it is a frame, and the frame may be in a tab of its own,
  * and what the browser is showing is handed to the viewers, so the capture
    shows the session rather than the moment it was taken.

The browser is a stub, so this runs without Chromium, without a network and
without uvicorn. The scripts the bridge runs in the browser are the shipped
ones; only the browser that answers them is not real.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402
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
        print("FAIL " + name + (("  " + str(detail)) if detail else ""))


async def settle(rounds=8):
    for _ in range(rounds):
        await asyncio.sleep(0.01)


class FakeFrame:
    """A frame of the browser, with the ids it holds."""

    def __init__(self, ids=(), fields=None, url="https://www.icloud.com/", title="iCloud"):
        self.ids = set(ids)
        self.fields = dict(fields or {})
        self.url = url
        self.title = title
        self.written = []
        self.pressed = []

    async def evaluate(self, script, argument=None):
        if "querySelectorAll" in script:
            return {
                "url": self.url,
                "title": self.title,
                "fields": {
                    name: {"value": value, "checked": False}
                    for name, value in self.fields.items()
                },
            }

        if "el.click()" in script:
            self.pressed.append(argument)
            return argument in self.ids

        # The write: [id, value]
        element, value = argument
        self.written.append((element, value))
        if element in self.ids:
            self.fields[element] = value
            return True
        return False


class FakePage:
    def __init__(self, frames):
        self.frames = frames


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


async def main():
    # ---- the element a capture's control maps to -------------------------
    check("an id we are willing to act on is accepted",
          live.element_id("account_name_text_field") == "account_name_text_field")
    check("an id with a space in it is refused",
          live.element_id("account name") == "")
    check("an id that is not an id is refused", live.element_id("a b; c") == "")
    check("an id that is too long is refused", live.element_id("a" * 200) == "")
    check("something that is not a string is refused", live.element_id(None) == "")

    # ---- writing into the browser ----------------------------------------
    # The sign-in form is not in the browser's top document: it is a widget
    # frame on another origin, and the sign-in flow may open a tab of its
    # own. So every frame of every page is searched.
    top = FakeFrame(url="https://www.icloud.com/", title="iCloud")
    form = FakeFrame(
        ids=["account_name_text_field", "sign-in"],
        fields={"account_name_text_field": ""},
        url="https://idmsa.apple.com/signin",
        title="Sign in to your Apple Account",
    )
    live.attach(FakeContext([FakePage([top]), FakePage([form])]), None)

    check("a value is written into the field with the same id",
          await live.write_field("account_name_text_field", "someone@example.com"),
          form.written)
    check("...and the browser holds it",
          form.fields["account_name_text_field"] == "someone@example.com",
          form.fields)
    check("a field the browser does not have is not written",
          not await live.write_field("no_such_field", "x"))
    check("a value that is not text is written as nothing",
          await live.write_field("account_name_text_field", None) and
          form.fields["account_name_text_field"] == "", form.fields)
    check("an id that is not an id writes nothing",
          not await live.write_field("a b", "x"))

    check("a button is pressed in the browser",
          await live.press("sign-in"), form.pressed)
    check("a button the browser does not have is not pressed",
          not await live.press("no_such_button"))

    # ---- what the browser is showing -------------------------------------
    await live.write_field("account_name_text_field", "someone@example.com")
    state = await live.snapshot()
    check("the browser's url is read", state["url"] == "https://idmsa.apple.com/signin", state)
    check("the browser's title is read",
          state["title"] == "Sign in to your Apple Account", state)
    check("the browser's fields are read",
          state["fields"].get("account_name_text_field", {}).get("value")
          == "someone@example.com", state)

    # ---- no browser at all ------------------------------------------------
    live.detach()
    check("with no browser there is nothing to write to",
          not await live.write_field("account_name_text_field", "x"))
    check("with no browser there is nothing to press",
          not await live.press("sign-in"))
    check("with no browser there is nothing to show",
          (await live.snapshot()) == {"url": "", "title": "", "fields": {}})

    # ---- through the hub --------------------------------------------------
    # The same two things, as the socket carries them.
    server.hub = server.Hub()
    form = FakeFrame(
        ids=["account_name_text_field", "sign-in"],
        fields={"account_name_text_field": ""},
        url="https://idmsa.apple.com/signin",
    )
    live.attach(FakeContext([FakePage([form])]), None)

    class FakeWebSocket:
        def __init__(self):
            self.incoming = asyncio.Queue()
            self.sent = []
            self.accepted = False

        async def accept(self):
            self.accepted = True

        async def receive_text(self):
            item = await self.incoming.get()
            if isinstance(item, BaseException):
                raise item
            return item

        async def send_json(self, message):
            self.sent.append(message)

        def feed(self, message):
            self.incoming.put_nowait(__import__("json").dumps(message))

        def drop(self):
            self.incoming.put_nowait(server.WebSocketDisconnect(code=1000))

    websocket = FakeWebSocket()
    task = asyncio.create_task(server.sync_socket(websocket))
    await settle()

    check("a viewer is told what to show",
          websocket.sent and websocket.sent[0]["t"] == "state", websocket.sent[:2])

    websocket.feed({"t": "field", "id": "account_name_text_field",
                    "value": "someone@example.com"})
    await settle(20)
    check("a value typed in the capture reaches the browser",
          form.fields.get("account_name_text_field") == "someone@example.com",
          form.fields)

    websocket.feed({"t": "press", "id": "sign-in"})
    await settle(20)
    check("a button pressed in the capture reaches the browser",
          "sign-in" in form.pressed, form.pressed)

    websocket.feed({"t": "field", "id": "not an id", "value": "x"})
    websocket.feed({"t": "press", "id": "not an id"})
    await settle(20)
    check("an id that is not an id is refused",
          form.written == [("account_name_text_field", "someone@example.com")] and
          form.pressed == ["sign-in"], (form.written, form.pressed))

    # ---- the browser state reaches the viewers ----------------------------
    server.hub.browser = {"url": "https://idmsa.apple.com/signin",
                          "title": "Sign in to your Apple Account",
                          "fields": {"account_name_text_field": {"value": "someone@example.com",
                                                                 "checked": False}}}
    late = FakeWebSocket()
    late_task = asyncio.create_task(server.sync_socket(late))
    await settle()
    joined = [m for m in late.sent if m.get("t") == "browser"]
    check("a viewer that joins late is told where the browser is",
          joined and joined[0]["url"] == "https://idmsa.apple.com/signin", late.sent[:3])
    check("...and what its fields hold",
          joined and joined[0]["fields"]["account_name_text_field"]["value"]
          == "someone@example.com", joined[:1])

    live.detach()
    websocket.drop()
    late.drop()
    await asyncio.gather(task, late_task, return_exceptions=True)


asyncio.run(main())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
