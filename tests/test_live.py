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
import tempfile
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


class FakeLocator:
    """What Playwright's locator does, as far as the bridge uses it.

    The bridge types into the browser with a locator, so a stub that only
    answers evaluate() would say nothing about whether typing works.
    """

    def __init__(self, frame, elements, index=0):
        self.frame = frame
        self.elements = elements
        self.index = index

    def nth(self, index):
        return FakeLocator(self.frame, self.elements, index)

    @property
    def first(self):
        return FakeLocator(self.frame, self.elements, 0)

    def _element(self):
        if self.index < len(self.elements):
            return self.elements[self.index]
        return None

    async def count(self):
        return len(self.elements)

    async def input_value(self, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        return self.frame.fields.get(name, "")

    async def click(self, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        self.frame.clicks.append(name)

    async def is_checked(self, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        return bool(self.frame.checked.get(name))

    async def is_visible(self, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        return name not in self.frame.hidden

    async def check(self, timeout=None):
        # What Playwright does with a box: it clicks it, unless it is
        # already ticked - a click on a ticked box unticks it.
        name = self._element()
        if name is None:
            raise Exception("no such control")
        if not self.frame.checked.get(name):
            self.frame.clicks.append(name)
            self.frame.checked[name] = True

    async def fill(self, value, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        self.frame.filled.append((name, value))
        self.frame.fields[name] = value

    async def evaluate(self, script, argument=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        if "getAttribute('type')" in script:
            return self.frame.types.get(name, "text")
        if "el.checked = true" in script:
            # The box the page hides behind a picture of one is set as it is.
            self.frame.checked[name] = True
            return True
        if "setSelectionRange" in script:
            at = argument if isinstance(argument, int) else \
                len(self.frame.fields.get(name, ""))
            self.frame.carets.append((name, at))
            return None
        if "document.activeElement" in script:
            # The cursor is in the field that was written into - and only
            # there: a field that has not been touched has no cursor.
            return name in self.frame.focused
        return None

    async def press_sequentially(self, value, delay=None, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        self.frame.focused.add(name)
        self.frame.typed.append((name, value))
        # What a real input does: the letters are added to what is there.
        self.frame.fields[name] = self.frame.fields.get(name, "") + value

    async def press(self, key, timeout=None):
        name = self._element()
        if name is None:
            raise Exception("no such control")
        self.frame.keys.append((name, key))
        # What a real input does with backspace: the letter before the
        # cursor goes.
        if key == "Backspace":
            text = self.frame.fields.get(name, "")
            at = self.frame.carets[-1][1] if self.frame.carets else len(text)
            if at > 0:
                self.frame.fields[name] = text[:at - 1] + text[at:]
                # The cursor goes back with the letter it took.
                self.frame.carets.append((name, at - 1))


class FakeFrame:
    """A frame of the browser, with the controls it holds.

    ``ids`` are the controls that have an id. ``kinds`` are the controls
    that have none - Apple's verification code boxes - named by their place,
    the same way the capture names them.
    """

    def __init__(self, ids=(), fields=None, kinds=None, messages=None,
                 types=None, url="https://www.icloud.com/", title="iCloud"):
        self.ids = set(ids)
        self.fields = dict(fields or {})
        self.kinds = dict(kinds or {})
        self.types = dict(types or {})
        self.url = url
        self.title = title
        self.messages = list(messages or [])
        self.written = []
        self.pressed = []
        self.clicks = []      # every click, into a field or on a button
        self.typed = []       # what was typed, key by key
        self.keys = []        # the keys that were pressed
        self.carets = []      # where the cursor was put
        self.focused = set()  # the fields the cursor is in
        self.filled = []      # what was cleared
        self.checked = {}     # the boxes that are ticked
        self.hidden = set()   # the boxes the page hides behind a picture
        self.heading = ""     # what the step calls itself

    def locator(self, selector):
        if selector.startswith("#"):
            name = selector[1:]
            elements = [name] if name in self.ids else []
        else:
            elements = list(self.kinds.get(selector, []))
        return FakeLocator(self, elements)

    async def evaluate(self, script, argument=None):
        if "out.slice" in script:
            return list(self.ids)

        if "querySelectorAll" in script:
            return {
                "url": self.url,
                "title": self.title,
                "heading": self.heading,
                "fields": {
                    name: {
                        "value": value,
                        "checked": bool(self.checked.get(name)),
                        "type": self.types.get(name, "text"),
                    }
                    for name, value in self.fields.items()
                },
                "messages": list(self.messages),
            }

        if "el.click()" in script:
            self.pressed.append(argument)
            return argument in self.ids

        # The write: [target, value]
        target, value = argument
        name = target.get("id") if isinstance(target, dict) else target
        self.written.append((name, value))
        if name in self.ids:
            self.fields[name] = value
            return True
        return False


class FakePage:
    def __init__(self, frames, url="https://www.icloud.com/"):
        self.frames = frames
        self.url = url


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
          form.typed)
    check("...and the browser holds it",
          form.fields["account_name_text_field"] == "someone@example.com",
          form.fields)
    check("...and it was typed, key by key, not assigned behind the widget's back",
          form.typed == [("account_name_text_field", "someone@example.com")],
          form.typed)
    check("a field the browser does not have is not written",
          await live.write_field("no_such_field", "x") == "missing")
    await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
    check("a value that is not text is written as nothing",
          await live.write_field("account_name_text_field", None) == "written" and
          form.fields["account_name_text_field"] == "", form.fields)
    check("an id that is not an id writes nothing",
          await live.write_field("a b", "x") == "missing")

    # ---- "keep me signed in" ---------------------------------------------
    # The box on the password step is always ticked: the session is the
    # whole point of the browser being here, and a browser that forgets the
    # sign-in is a browser that has to be signed into again.
    form.ids.add("remember-me")
    form.types["remember-me"] = "checkbox"
    await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)

    check("'keep me signed in' is ticked, whatever the capture says",
          await live.write_field("remember-me", "false") == "written" and
          form.checked.get("remember-me") is True, form.checked)
    check("...and it is ticked by clicking it, the way a person does",
          "remember-me" in form.clicks, form.clicks)
    check("...and it is not typed into",
          ("remember-me", "false") not in form.typed and
          ("remember-me", "true") not in form.typed, form.typed)

    clicks = len(form.clicks)
    check("...and a box that is already ticked is not clicked again",
          await live.write_field("remember-me", "false") == "written" and
          len(form.clicks) == clicks, form.clicks)

    check("...and the bridge ticks it on its own, with nothing asked of it",
          await live.tick_remember_me() is True and
          form.checked.get("remember-me") is True, form.checked)

    form.hidden.add("remember-me")
    form.checked.pop("remember-me", None)
    clicks = len(form.clicks)
    check("...and a box the page hides is set rather than clicked",
          await live.tick_remember_me() is True and
          form.checked.get("remember-me") is True and
          len(form.clicks) == clicks, (form.checked, form.clicks))
    form.hidden.discard("remember-me")

    check("a button is pressed in the browser",
          await live.press("sign-in"), form.clicks)
    check("...with a real click, so the page takes it as a person",
          "sign-in" in form.clicks, form.clicks)
    check("a button the browser does not have is not pressed",
          await live.press("no_such_button") == "missing")

    # ---- what the browser is showing -------------------------------------
    # Every frame is read, not just the first one: the sign-in form is in a
    # frame, and the document above it has no fields at all. Reading only
    # the first frame is how a capture kept a stale value that the browser
    # had already cleared.
    await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
    await live.write_field("account_name_text_field", "someone@example.com")
    await live.flush_pending()
    state = await live.snapshot()
    check("the browser's url is read", state["url"] == "https://idmsa.apple.com/signin", state)
    check("the browser's title is read",
          state["title"] == "Sign in to your Apple Account", state)
    check("the browser's fields are read",
          state["fields"].get("account_name_text_field", {}).get("value")
          == "someone@example.com", state)

    # A browser whose top document has no fields at all, and whose form is
    # in a frame below it.
    bare_top = FakeFrame(url="https://www.icloud.com/", title="iCloud")
    bare_form = FakeFrame(
        ids=["account_name_text_field"],
        fields={"account_name_text_field": "someone@example.com"},
        url="https://idmsa.apple.com/signin",
        title="Sign in to your Apple Account",
    )
    live.attach(FakeContext([FakePage([bare_top, bare_form])]), None)
    merged = await live.snapshot()
    check("the fields of a frame below the top one are read too",
          merged["fields"].get("account_name_text_field", {}).get("value")
          == "someone@example.com", merged)
    check("...and the frame that has them says where the browser is",
          merged["url"] == "https://idmsa.apple.com/signin", merged)

    # ---- a control with no id --------------------------------------------
    # Apple's verification code boxes have no ids at all: six inputs, and
    # the third one is named by being the third one.
    boxes = FakeFrame(
        ids=[],
        kinds={"input": ["input#1", "input#2", "input#3"]},
        fields={"input#1": "", "input#2": "", "input#3": ""},
    )
    live.attach(FakeContext([FakePage([boxes])]), None)

    check("a control with no id is written by its place",
          await live.write_at("input", 3, 3, "7"), boxes.typed)
    check("...and it is typed, not assigned",
          boxes.typed == [("input#3", "7")], boxes.typed)

    # ...but only when the page has the same number of them. If it does
    # not, the right box cannot be told from the wrong one.
    await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
    check("a control is not written when the page has a different number",
          await live.write_at("input", 3, 6, "7") == "missing", boxes.typed)

    check("a place that is not a place is refused",
          await live.write_at("input", 0, 3, "7") == "missing")
    check("a place beyond the end is refused",
          await live.write_at("input", 9, 3, "7") == "missing")

    # ---- a control with no id, of a kind of its own -----------------------
    # Apple's verification code boxes are inputs like any other, and the
    # page can hold other inputs too - the widget keeps the sign-in form in
    # the page while the step underneath it changes. So the boxes are named
    # by their class: "the third form-security-code-input", which is the
    # same box in two pages that do not hold quite the same inputs.
    code = FakeFrame(
        ids=[],
        kinds={"input.form-security-code-input":
               ["code#1", "code#2", "code#3"]},
        fields={"code#1": "", "code#2": "", "code#3": ""},
    )
    live.attach(FakeContext([FakePage([code])]), None)

    await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
    check("a control of a kind of its own is written by its place",
          await live.write_at("input", 2, 3, "7", "form-security-code-input"),
          code.typed)
    check("...and it is the box of that kind that is written",
          code.typed == [("code#2", "7")], code.typed)
    check("...and the page may hold other inputs besides",
          await live.write_at("input", 1, 3, "8", "form-security-code-input")
          == "written", code.typed)
    await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
    check("...but not a different number of them",
          await live.write_at("input", 1, 4, "8", "form-security-code-input")
          == "missing", code.typed)

    live.detach()

    # ---- a letter that is taken away --------------------------------------
    # The person pressed backspace. The letter that is gone is taken away
    # with the same key, one at a time - not by emptying the field and
    # typing it all again, which is a different thing altogether as far as
    # the widget is concerned.
    address = FakeFrame(ids=["account_name_text_field"],
                         fields={"account_name_text_field": "me@mail.com"})
    live.attach(FakeContext([FakePage([address])]), None)

    await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
    check("a letter that is taken away is taken away with a key",
          await live.write_field("account_name_text_field", "e@mail.com") == "written",
          address.typed)
    check("...and it is one key, not the whole address again",
          address.keys == [("account_name_text_field", "Backspace")], address.keys)
    check("...and the cursor went where the letter was",
          ("account_name_text_field", 1) in address.carets, address.carets)
    check("...and the field holds what is left of it",
          address.fields["account_name_text_field"] == "e@mail.com",
          address.fields)

    live.detach()

    # ---- the capture is not the session ----------------------------------
    # A capture holds the same ids as the page it was taken from. Open the
    # capture in the same browser as the session and both hold
    # account_name_text_field - and the value must go to the session, not
    # back into the copy the user is looking at.
    session = FakeFrame(ids=["account_name_text_field"], fields={})
    copy = FakeFrame(ids=["account_name_text_field"], fields={})
    live.attach(
        FakeContext([
            # The capture was opened after the session, so it is the newer
            # page - and the one that would be written into by mistake.
            FakePage([session]),
            FakePage([copy], url="http://127.0.0.1:8000/src/4.html"),
        ]),
        None,
    )
    live.exclude_host("127.0.0.1:8000")

    await live.write_field("account_name_text_field", "someone@example.com")
    check("the value goes to the session, not to the capture",
          session.fields.get("account_name_text_field") == "someone@example.com"
          and "account_name_text_field" not in copy.fields,
          (session.fields, copy.fields))

    live.detach()

    # ---- typing on, the way a person types --------------------------------
    # Each letter arrives on its own. The first one goes in at once - which
    # is what puts the cursor in the field - and the rest are held back
    # until the typing stops, and then written as one. A field that is
    # emptied and filled again for every letter is a field the widget
    # never gets to the end of, and a password field that is rewritten
    # letter by letter looks broken to the person watching.
    typed_field = FakeFrame(ids=["account_name_text_field"], fields={})
    live.attach(FakeContext([FakePage([typed_field])]), None)

    first = await live.write_field("account_name_text_field", "s")
    check("the first letter goes in at once", first == "written", first)
    check("...and it is typed, not assigned",
          typed_field.typed == [("account_name_text_field", "s")], typed_field.typed)

    await live.write_field("account_name_text_field", "so")
    await live.write_field("account_name_text_field", "som")
    check("the letters that follow are held back",
          typed_field.typed == [("account_name_text_field", "s")], typed_field.typed)

    await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
    await live.flush_pending()
    check("...and they go in as one, when the typing stops",
          typed_field.typed == [("account_name_text_field", "s"),
                                ("account_name_text_field", "om")],
          typed_field.typed)
    check("...and the field ends up holding the whole address",
          typed_field.fields.get("account_name_text_field") == "som",
          typed_field.fields)

    # A value that is not an extension of what is there is typed whole.
    typed_field.typed.clear()
    await asyncio.sleep(live.DEBOUNCE_SECONDS + 0.1)
    await live.write_field("account_name_text_field", "other@example.com")
    check("a value that is not more of the same is typed whole",
          typed_field.typed == [("account_name_text_field", "other@example.com")],
          typed_field.typed)

    live.detach()

    # ---- what the browser is saying ---------------------------------------
    # The browser says when the details are wrong, and the capture has to
    # say it too.
    saying = FakeFrame(
        ids=["account_name_text_field"],
        fields={},
        messages=[{
            "text": "Check the account information you entered and try again.",
            "html": ("<span>Check the account information you entered and "
                     "try again.</span>"),
            "className": "form-message",
            "kind": "",
            "style": {"color": "rgb(215, 0, 21)", "fontSize": "12px"},
            "field": "account_name_text_field",
            "before": "",
            "after": "",
            "has_link": False,
        }],
    )
    live.attach(FakeContext([FakePage([saying])]), None)
    state = await live.snapshot()
    check("what the browser is saying is read",
          state["messages"] and
          state["messages"][0]["text"].startswith("Check the account"),
          state.get("messages"))
    check("...and the way it looks comes with it",
          state["messages"][0]["style"].get("color") == "rgb(215, 0, 21)",
          state["messages"][0].get("style"))
    live.detach()

    # ---- a press by the way the page names it ----------------------------
    # The sign-in button on the landing page has no id: it is a ui-button
    # with a class. When the capture is further along than the browser,
    # pressing Continue means taking the sign-in further, and that is what
    # the sign-in button does.
    class SelectorFrame(FakeFrame):
        def locator(self, selector):
            if selector.startswith("#"):
                return FakeLocator(self, [selector[1:]] if selector[1:] in self.ids else [])
            return FakeLocator(self, list(self.kinds.get(selector, [])))

    landing = SelectorFrame(ids=[], kinds={"ui-button.sign-in-button": ["signin"]})
    live.attach(FakeContext([FakePage([landing])]), None)
    check("a control the browser has but has no id for can still be pressed",
          await live.press_selector("ui-button.sign-in-button"))
    check("...and it is a real click",
          landing.clicks == ["signin"], landing.clicks)
    check("a control the browser does not have is not pressed",
          not await live.press_selector("ui-button.no-such-button"))
    live.detach()

    # ---- a thing that has nowhere to go is said once ---------------------
    # The console is for the person watching. The same line every second is
    # not news.
    nothing = FakeFrame(ids=[], fields={})
    live.attach(FakeContext([FakePage([nothing])]), None)
    await live.write_field("no_such_field", "x")
    check("a thing that has nowhere to go is said",
          live.mark_reported({"id": "no_such_field"}) is True)
    check("...and it is not said again",
          live.mark_reported({"id": "no_such_field"}) is False)
    live.detach()

    # ---- no browser at all ------------------------------------------------
    live.detach()
    check("with no browser there is nothing to write to",
          await live.write_field("account_name_text_field", "x") == "missing")
    check("with no browser there is nothing to press",
          await live.press("sign-in") == "missing")
    check("with no browser there is nothing to show",
          (await live.snapshot()) == {"url": "", "title": "", "heading": "",
                                      "fields": {}, "messages": []})

    # ---- a value that had nowhere to go ----------------------------------
    # The sign-in widget is a frame Apple loads a second or two after Sign
    # In is clicked. A value typed in that window has no field to go into,
    # so it is kept and tried again - the person does not have to type it
    # twice.
    late = FakeFrame(ids=[], fields={})
    live.attach(FakeContext([FakePage([late])]), None)

    check("a value with no field yet is not written",
          await live.write_field("account_name_text_field", "someone@example.com") == "missing")
    check("...but it is kept",
          live.pending().get("id:account_name_text_field", {}).get("value")
          == "someone@example.com", live.pending())

    late.ids.add("account_name_text_field")
    await live.flush_pending()
    check("...and it is written when the field turns up",
          late.fields.get("account_name_text_field") == "someone@example.com",
          late.fields)
    check("...and then it is not kept any more",
          "id:account_name_text_field" not in live.pending(), live.pending())

    # A press that had no button is kept as well, but only for a little
    # while: a button pressed long after the person pressed it is not what
    # they asked for.
    check("a press with no button yet is not pressed",
          await live.press("sign-in") == "missing")
    check("...but it is kept",
          "id:sign-in" in live.pending(), live.pending())
    late.ids.add("sign-in")
    await live.flush_pending()
    check("...and it is pressed when the button turns up",
          "sign-in" in late.clicks, late.clicks)
    check("...and then it is not kept any more",
          "id:sign-in" not in live.pending(), live.pending())

    # A value that never finds its field is given up on.
    await live.write_field("never_there", "x")
    live._pending["id:never_there"]["at"] -= (live.PENDING_FIELD_TTL + 1)
    await live.flush_pending()
    check("a value that never finds its field is given up on",
          "id:never_there" not in live.pending(), live.pending())

    # ---- what the browser has -------------------------------------------
    # "not found" on its own is not worth much. What the browser does have
    # turns it into something that can be read.
    empty = FakeFrame(ids=[], fields={})
    live.attach(FakeContext([FakePage([empty])]), None)
    check("a browser with no controls says so",
          "no controls yet" in await live.describe_missing("account_name_text_field"),
          await live.describe_missing("account_name_text_field"))

    live.attach(FakeContext([FakePage([late])]), None)
    described = await live.describe_missing("account_name_text_field")
    check("a browser with controls lists them",
          "account_name_text_field" in described and "sign-in" in described, described)

    live.detach()

    # ---- through the hub --------------------------------------------------
    # The same two things, as the socket carries them.
    #
    # The captures are pointed at an empty folder: with the app's own flow
    # file in place, a press on a capture it mentions moves the app instead
    # of reaching the browser, and that is the flow's business (and
    # test_sync.py's).
    server.SRC = Path(tempfile.mkdtemp()).resolve()
    session = server.session_for('the-tests')
    session.hub = server.Hub()
    form = FakeFrame(
        ids=["account_name_text_field", "sign-in"],
        fields={"account_name_text_field": ""},
        url="https://idmsa.apple.com/signin",
    )
    # The bridge the capture talks to is the one belonging to the visitor
    # whose capture this is.
    session.bridge.attach(FakeContext([FakePage([form])]), None)

    class FakeWebSocket:
        def __init__(self):
            self.cookies = {server.SESSION_COOKIE: "the-tests"}
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
          "sign-in" in form.clicks, form.clicks)

    websocket.feed({"t": "field", "id": "not an id", "value": "x"})
    websocket.feed({"t": "press", "id": "not an id"})
    await settle(20)
    check("an id that is not an id is refused",
          form.typed == [("account_name_text_field", "someone@example.com")] and
          form.clicks == ["account_name_text_field", "sign-in"],
          (form.typed, form.clicks))

    # ---- the browser state reaches the viewers ----------------------------
    session.hub.browser = {"url": "https://idmsa.apple.com/signin",
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
