"""The bridge from a served capture to the browser the server opened.

The app shows a capture; the browser holds the session. This module is the
only part of the app that knows the browser exists: it finds the control a
capture's field maps to, writes a value into it, presses a button, and reads
back what the browser is showing.

**The mapping is by id.** A capture keeps the ids of the page it was taken
from, so the field with the id ``account_name_text_field`` in the capture is
the field with that id in the real page - in whichever frame of the browser
happens to hold it. The sign-in form is not in icloud.com's own document: it
is a widget frame on another origin, and the sign-in flow may open a tab of
its own, so neither the top document nor the first page is necessarily where
the field is. Every frame of every page is searched, and the first one that
answers wins.

Nothing here needs Playwright imported: the browser is attached as an object
with ``pages`` and ``frames``, which is what makes this testable without a
browser at all.
"""

import asyncio
import re
import time

# The id of a control in a capture. The same shape as an HTML id, so that it
# can be turned into a selector without escaping anything.
ELEMENT_ID = re.compile(r"^[A-Za-z][\w-]{0,80}$")

# How much of a value is written into the browser, and how much is read back.
MAX_VALUE = 10000

# How long the letters are held back while the person is still typing. Long
# enough to catch the end of a word, short enough that the browser keeps up
# with the hand.
DEBOUNCE_SECONDS = 0.15

# The fields are read back so the capture can show what the browser is
# really showing. A password is not one of them: it is written once, by the
# person using the app, and it has no business being broadcast to every
# viewer or being kept in the server's memory.
SECRET_TYPES = ("password",)

# Written through the prototype setter, then announced with input and change:
# the widget's framework listens for those events and ignores a plain
# assignment, so `el.value = x` alone would leave the browser looking empty.
#
# The argument arrives as one value - a pair - so the script takes it apart
# itself. Playwright hands a script exactly one argument: a script that
# expects two gets the first one and undefined for the second, and
# getElementById then looks for "id,value" and finds nothing.
#
# The target is either an id or a place: what the control is, how many of
# them there are, and which one this is. Apple's verification code boxes
# have no ids at all, and the place is what names them.
_CONTROLS = """() => {
  const nodes = document.querySelectorAll(
    'input, textarea, select, button, [role=button], a[href]');
  const out = [];
  const seen = {};
  for (const el of nodes) {
    const tag = el.tagName.toLowerCase();
    seen[tag] = (seen[tag] || 0) + 1;
    // A control with no id is named by its place, the same way the capture
    // names it.
    out.push(el.id ? el.id : (tag + '#' + seen[tag]));
  }
  return out.slice(0, 60);
}"""


_WRITE = """([target, value]) => {
  const el = target.id
    ? document.getElementById(target.id)
    : (() => {
        const all = document.getElementsByTagName(target.tag);
        if (all.length !== target.of) return null;
        return all[target.n - 1] || null;
      })();
  if (!el) return false;
  const tag = el.tagName;
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (type === 'checkbox' || type === 'radio') {
    el.checked = !!(value === 'true' || value === 'on' || value === '1');
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }
  if (tag === 'SELECT') {
    el.value = value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }
  if (!('value' in el)) return false;
  const proto = tag === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(proto.prototype, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}"""

# What the browser is showing: where it is, what it is called, and the state
# of every field it has that we can name.
_READ = """() => {
  const fields = {};
  const nodes = document.querySelectorAll('input, textarea, select');
  for (const el of nodes) {
    if (!el.id) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'password') continue;
    fields[el.id] = {
      value: el.value === undefined ? '' : String(el.value).slice(0, 10000),
      checked: type === 'checkbox' || type === 'radio' ? !!el.checked : false,
    };
  }

  // What the page is saying. The sign-in widget puts its complaint in a
  // span of class form-message, and the text is what the person has to
  // read to know what to change.
  //
  // The way it looks is read as well, so the capture can show the message
  // in the same colours and at the same size: the capture does not carry
  // the page's stylesheet for a message that was not there when it was
  // taken.
  const messages = [];
  const said = document.querySelectorAll(
    '.form-message, [role=alert], .form-error, .form-message-container');
  for (const el of said) {
    const text = (el.textContent || '').trim();
    if (!text) continue;
    if (el.getAttribute('aria-hidden') === 'true') continue;

    const style = window.getComputedStyle(el);
    const messages_style = {
      color: style.color,
      fontSize: style.fontSize,
      lineHeight: style.lineHeight,
      marginTop: style.marginTop,
      marginBottom: style.marginBottom,
      fontWeight: style.fontWeight,
      display: style.display,
    };

    // The field the message is about: the closest field above it. And the
    // neighbours it sits between, so the capture can put it in the same
    // place in a document it has never seen.
    let field = '';
    let before = '';
    let after = '';
    const all_fields = document.querySelectorAll('input, select, textarea');
    for (const f of all_fields) {
      if (!f.id) continue;
      if (f.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
        field = f.id;
      }
    }
    let node = el;
    while (node) {
      if (!before && node.previousElementSibling && node.previousElementSibling.id) {
        before = node.previousElementSibling.id;
      }
      if (!after && node.nextElementSibling && node.nextElementSibling.id) {
        after = node.nextElementSibling.id;
      }
      node = node.parentElement;
    }

    messages.push({
      text: text.slice(0, 2000),
      html: el.innerHTML.trim().slice(0, 4000),
      className: typeof el.className === 'string' ? el.className : '',
      kind: el.getAttribute('role') || '',
      style: messages_style,
      field: field,
      before: before,
      after: after,
      has_link: !!el.querySelector('a'),
    });
  }

  return { url: location.href, title: document.title, fields, messages };
}"""

# The browser the server opened. ``context`` is a Playwright BrowserContext
# and ``page`` the page it started on; either can be None.

# The hosts the app serves its captures from. A capture is the copy, not the
# session, and the two hold the same ids - so the copy is left alone.

# One write at a time. Two writes in flight at once reach the browser in
# whatever order the browser gets to them, and the text comes out scrambled -
# the user types an address and then a password, and the password lands in
# the address field.



# How long a value waits for its field, and a press waits for its button.
PENDING_FIELD_TTL = 120.0
PENDING_PRESS_TTL = 20.0


class Bridge:
    """One browser, and the way to it.

    Each person who visits the app gets one of these: their own browser,
    their own profile, their own session, and their own capture. The
    bridge is what the capture talks to - what is typed into the capture is
    typed into that browser, and what that browser is saying comes back.
    """

    def __init__(self):
        self._context = None
        self._page = None
        self._excluded_hosts: set[str] = set()
        self._lock = asyncio.Lock()
        self._pending: dict[str, dict] = {}
        self._written_at: dict[str, float] = {}
        self._flush_task = None

    def attach(self, context, page) -> None:
        """The browser the app drives."""
        self._context = context
        self._page = page
        self.reset()


    def reset(self) -> None:
        """Forget what was waiting, and when things were last written.

        A new browser is a new session: what was waiting for the old one has
        nothing to do with this one.
        """
        self._pending.clear()
        self._written_at.clear()


    def detach(self) -> None:
        """The browser has gone."""
        self._context = None
        self._page = None


    def is_open(self) -> bool:
        return self._context is not None or self._page is not None


    def frames(self) -> list:
        """Every frame of every page the browser has open, but not the captures.

        The newest page first: the sign-in flow opens a tab of its own, and that
        tab is the page the user is looking at, so it is the page the capture is
        talking about. A frame the older page holds is still searched - the
        field may be there and not in the new one - but the new one is asked
        first.

        The pages the app itself serves are left out. A capture holds the same
        ids as the page it was taken from, and a capture opened in the same
        browser as the session would otherwise be written into instead of the
        session: the value would go back into the copy the user is looking at.
        """
        pages = []
        if self._context is not None:
            try:
                pages = list(self._context.pages)
            except Exception:
                pages = []
        if not pages and self._page is not None:
            pages = [self._page]

        out = []
        for browser_page in reversed(pages):
            if self.is_capture(browser_page):
                continue
            try:
                out.extend(browser_page.frames)
            except Exception:
                continue
        return out


    def exclude_host(self, host: str) -> None:
        """Remember a host the app serves, so its pages are not written into."""
        if host:
            self._excluded_hosts.add(host)


    def is_capture(self, browser_page) -> bool:
        """Is this a page of the app, rather than the session it drives?"""
        try:
            url = browser_page.url or ""
        except Exception:
            return False

        if not url.startswith("http://") and not url.startswith("https://"):
            return False

        host = url.split("//", 1)[1].split("/", 1)[0]
        return host in self._excluded_hosts


    def element_id(self, value) -> str:
        """The id of a control, if it is one we are willing to act on."""
        if not isinstance(value, str) or not ELEMENT_ID.match(value):
            return ""
        return value


    async def in_frames(self, script: str, argument) -> bool:
        """Run a script in each frame until one of them answers True.

        A frame that is navigating, detached or crashed is not an error worth
        passing on: the control is somewhere, and the next frame may be where it
        is.
        """
        for frame in self.frames():
            try:
                if await frame.evaluate(script, argument):
                    return True
            except Exception:
                continue
        return False


    async def write_field(self, name: str, value: str) -> str:
        """Write a value into the browser, where the capture's field maps to it.

        Says what happened: "written" (it is in the browser now), "held" (the
        person is still typing, so it waits for them to stop) or "missing"
        (there is no such field in the browser yet).

        A value that has nowhere to go yet is kept, and tried again: the
        sign-in widget is a frame Apple loads a second or two after Sign In is
        clicked, so a value typed straight away would otherwise be lost.
        """
        name = self.element_id(name)
        if not name:
            return "missing"
        return await self._write_target({"id": name}, value)


    async def write_at(self, tag: str, n: int, of: int, value: str) -> str:
        """Write into the nth control of a kind, when the page has that many.

        A control with no id - Apple's verification code boxes have none - is
        named by what it is and how many of them there are: the third input of
        the page. The capture and the live page are the same page, so the third
        input of one is the third input of the other.

        But only if there are the same number of them. If the live page has a
        different number, the right one cannot be told from the wrong one, and
        a value typed into the wrong box is worse than no value at all - so
        nothing is written, and the console says so.
        """
        if not isinstance(tag, str) or not re.match(r"^[a-z][a-z0-9-]{0,20}$", tag):
            return "missing"
        if not isinstance(n, int) or not isinstance(of, int) or n < 1 or of < 1 or n > of:
            return "missing"
        return await self._write_target({"tag": tag, "n": n, "of": of}, value)


    async def _write_target(self, target: dict, value: str) -> str:
        """The value goes in as keys, and only as a last resort as a value.

        The sign-in widget is a widget: it watches for keys, not for values. A
        value assigned behind its back sits in the field, and the page does not
        react - the code boxes do not take the focus on, and the page looks
        like nobody has touched it. So the value is typed, key by key, the way
        a person types it, and the page reacts as it would.

        Where typing cannot happen - the control is not on screen, or the frame
        will not take a click - the value is written the old way, so that
        something is better than nothing.

        The letters that arrive while the person is still typing are held back
        a moment and written as one. The widget checks the address as it is
        typed, and a field that is emptied and filled again for every letter
        is a field the widget never gets to the end of - and a password field
        that is rewritten letter by letter looks broken to the person watching.
        So the first letter goes in at once - which is what puts the cursor in
        the field - and the rest goes in when the typing stops.
        """
        if not isinstance(value, str):
            value = ""
        value = value[:MAX_VALUE]

        key = self._key(target)
        now = time.monotonic()
        last = self._written_at.get(key, 0.0)

        if now - last < DEBOUNCE_SECONDS:
            # The typing has not stopped yet. The newest value is the one that
            # will be written, and it is written when it does.
            self.remember(target, "field", value, held=True)
            self._schedule_flush()
            return "held"

        written = await self._write_now(target, value)
        self._written_at[key] = now

        if written:
            self.forget(target)
            return "written"

        self.remember(target, "field", value)
        return "missing"


    async def _write_now(self, target: dict, value: str) -> bool:
        async with self._lock:
            if await self._type_now(target, value):
                return True
            return await self.in_frames(_WRITE, [target, value])


    async def _type_now(self, target: dict, value: str) -> bool:
        """Type the value into the control, key by key, in the live browser."""
        for frame in self.frames():
            locator = self._locator(frame, target)
            if locator is None:
                continue

            try:
                if await locator.count() == 0:
                    continue
                if not await self._same_page(frame, target):
                    continue
            except Exception:
                continue

            current = None
            try:
                current = await locator.input_value(timeout=1500)
            except Exception:
                pass
            if current == value:
                return True              # it is already what it should be

            try:
                # The field is clicked into only when it does not have the
                # focus: a click is a click, and it is the slow part of the
                # work, and the widget does not need it again for every letter.
                if not await self._has_focus(locator):
                    await locator.click(timeout=1500)
                await self._caret_to_end(locator)

                if current and value.startswith(current):
                    # The person is still typing: the letters that are new are
                    # typed, and the field keeps what it had. Typing the whole
                    # thing again would make the widget see the address change
                    # from the beginning over and over, while it is trying to
                    # work out what the address is.
                    await locator.press_sequentially(value[len(current):], delay=15,
                                                     timeout=15000)
                    return True

                if current:
                    # The value is the whole of what should be in the field, not
                    # more of it.
                    await locator.fill("", timeout=1500)
                await locator.press_sequentially(value, delay=15, timeout=15000)
                return True
            except Exception:
                # Not on screen, or the frame will not take a click: the next
                # frame may be the one that has it.
                continue
        return False


    async def _has_focus(self, locator) -> bool:
        """Is this the field the cursor is in?"""
        try:
            return await locator.evaluate(
                "el => !!el && document.activeElement === el")
        except Exception:
            return False


    async def _caret_to_end(self, locator) -> None:
        """Put the cursor at the end of the field.

        A click lands where the mouse is, which is the middle of the field
        more often than not - and then the letters are typed in the middle of
        what is already there. The address comes out as "someone@exomample.c".
        So the cursor is put where a person typing would leave it: at the end.
        """
        try:
            await locator.evaluate(
                """el => {
                    if (!el || typeof el.setSelectionRange !== 'function') return;
                    const end = el.value ? el.value.length : 0;
                    el.setSelectionRange(end, end);
                }""")
        except Exception:
            pass


    def _locator(self, frame, target: dict):
        """The control, as a locator: by id, or by its place among its kind."""
        try:
            if target.get("id"):
                return frame.locator("#" + target["id"])

            tag = target.get("tag")
            if not tag:
                return None
            return frame.locator(tag).nth(target["n"] - 1)
        except Exception:
            return None


    def _matches(self, target: dict, count: int) -> bool:
        """Is this the page the control was named on?

        A control named by its place is only the same control if the page has
        the same number of them.
        """
        if target.get("id"):
            return True
        return count == target.get("of")


    async def _same_page(self, frame, target: dict) -> bool:
        """Is this frame the page the control was named on?

        A control named by its place is only the same control if the page has
        the same number of them. If it has a different number, the right one
        cannot be told from the wrong one - and a value typed into the wrong
        box is worse than no value at all.
        """
        if target.get("id"):
            return True
        try:
            count = await frame.locator(target["tag"]).count()
        except Exception:
            return False
        return count == target.get("of")


    async def press(self, name: str) -> str:
        """Press a control in the browser, where the capture's button maps to it.

        Says what happened: "pressed" (it was pressed), "disabled" (the
        browser has it, but it cannot be pressed - Apple's Continue stays
        disabled until the form is filled in), "missing" (the browser does not
        have it) or "held" (it is kept and tried again).

        A press that has nowhere to go yet is kept, and tried again - but only
        for a little while: a button pressed long after the person pressed it
        is not what they asked for.
        """
        name = self.element_id(name)
        if not name:
            return "missing"

        target = {"id": name}
        pressed = await self._press_now(target)
        if pressed == "pressed" or pressed == "disabled":
            self.forget(target)
        else:
            # It is not there yet. It is kept, and tried again.
            self.remember(target, "press", "")
        return pressed


    async def press_selector(self, selector: str) -> bool:
        """Press a control the browser has, named the way the page names it.

        Some controls have no id: the sign-in button on the iCloud landing page
        is a ui-button with a class and nothing else. When the capture is
        further along than the browser - the capture shows the form, the browser
        still shows the landing page - pressing Continue in the capture means
        taking the sign-in further, and the sign-in button is what does that.
        """
        if not isinstance(selector, str) or not selector.strip():
            return False

        async with self._lock:
            for frame in self.frames():
                try:
                    locator = frame.locator(selector).first
                    if await locator.count() == 0:
                        continue
                    await locator.click(timeout=1500)
                    return True
                except Exception:
                    continue
        return False


    async def _press_now(self, target: dict) -> str:
        """A real click, so the page treats it as a person.

        A control that is there but cannot be pressed is said so: Apple's
        Continue stays disabled until the form is filled in, and a click on a
        disabled button does nothing at all - which looks, from the outside,
        like the bridge is not working.
        """
        async with self._lock:
            for frame in self.frames():
                locator = self._locator(frame, target)
                if locator is None:
                    continue
                try:
                    if await locator.count() == 0:
                        continue
                    await locator.click(timeout=1500)
                    return "pressed"
                except Exception:
                    if await self._is_disabled(locator):
                        return "disabled"
                    continue
            return "missing"


    async def _is_disabled(self, locator) -> bool:
        """Is this control there but unable to be pressed?"""
        try:
            return await locator.is_disabled()
        except Exception:
            return False


    # --------------------------------------------------------------------------
    # The ones that had nowhere to go yet
    #
    # The browser is not ready the moment the app is: the sign-in widget is a
    # frame on another origin, and Apple loads it a second or two after Sign In
    # is clicked. A value typed in that window has no field to go into, and a
    # button pressed in it has no button. Both are kept here and tried again,
    # so the person does not have to press it twice.
    # --------------------------------------------------------------------------

    def remember(self, target: dict, kind: str, value: str = "", held: bool = False) -> None:
        """Keep something that has nowhere to go, so it can be tried again."""
        self._pending[self._key(target)] = {"target": dict(target), "kind": kind,
                                  "value": value, "at": time.monotonic(),
                                  "held": held, "reported": False}


    def forget(self, target: dict) -> None:
        self._pending.pop(self._key(target), None)


    def mark_reported(self, target: dict) -> bool:
        """Has this been said already?

        The console is for the person watching, and the same thing said every
        second is not news. So a thing that has nowhere to go is said once.
        """
        entry = self._pending.get(self._key(target))
        if entry is None:
            return True
        if entry.get("reported"):
            return False
        entry["reported"] = True
        return True


    def _key(self, target: dict) -> str:
        """What a target is called in the pending list."""
        if target.get("id"):
            return "id:" + target["id"]
        return f"{target.get('tag')}:{target.get('n')}"


    def pending(self) -> dict:
        """What is waiting, for the console and the tests."""
        return {name: dict(entry) for name, entry in self._pending.items()}


    async def flush_pending(self) -> None:
        """Try again the values and presses that had nowhere to go yet."""
        if not self._pending:
            return

        now = time.monotonic()
        for name, entry in list(self._pending.items()):
            ttl = PENDING_PRESS_TTL if entry["kind"] == "press" else PENDING_FIELD_TTL
            if now - entry["at"] > ttl:
                self._pending.pop(name, None)
                continue

            if entry["kind"] == "press":
                # Only a press that went through takes the press off the
                # list. A press that did not - because the button is not
                # there yet, or because the page is busy - is tried again:
                # the sign-in widget is a frame Apple loads a second or two
                # after Sign In is clicked.
                if await self._press_now(entry["target"]) == "pressed":
                    self._pending.pop(name, None)
                continue

            if entry.get("held") and now - entry["at"] < DEBOUNCE_SECONDS:
                # The person may still be typing. It waits for them to stop.
                continue

            if await self._write_now(entry["target"], entry["value"]):
                self._pending.pop(name, None)


    def _schedule_flush(self) -> None:
        """Write what is waiting, as soon as the typing has stopped."""
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.ensure_future(self._flush_later())
        except RuntimeError:
            self._flush_task = None


    async def _flush_later(self) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS + 0.05)
        try:
            await self.flush_pending()
        except Exception:
            pass


    # The ids the browser has for the things we can act on. It is what turns
    # "not found" into something that can be read: a control that is there
    # under another name, or a browser that has not loaded the widget yet.


    async def controls(self) -> list:
        """The ids the browser has for the things we can act on."""
        found: list = []
        for frame in self.frames():
            try:
                ids = await frame.evaluate(_CONTROLS)
            except Exception:
                continue
            for name in ids or []:
                if name not in found:
                    found.append(name)
        return found


    async def describe_missing(self, name: str) -> str:
        """What the browser has, so a mismatch can be read rather than guessed."""
        ids = await self.controls()
        if not ids:
            return "the browser has no controls yet - the sign-in widget may still be loading"
        near = [i for i in ids if name.lower() in i.lower() or i.lower() in name.lower()]
        hint = ("; closest: " + ", ".join(near[:5])) if near else ""
        return "the browser has: " + ", ".join(ids[:12]) + hint


    async def snapshot(self) -> dict:
        """What the browser is showing right now.

        Every frame is read, not just the first one: the sign-in form is in a
        frame, and the document above it has no fields at all. So the fields of
        every frame are taken together, and the frame that has them is the one
        that says where the browser is.

        If none of them answers - the browser is busy, or it has gone - an empty
        answer is returned rather than an error, because this is polled and a
        failure here is not worth stopping anything for.
        """
        fields: dict = {}
        messages: list = []
        url = title = ""
        fallback_url = fallback_title = ""

        for frame in self.frames():
            try:
                state = await frame.evaluate(_READ)
            except Exception:
                continue
            if not isinstance(state, dict):
                continue

            frame_fields = state.get("fields")
            frame_fields = frame_fields if isinstance(frame_fields, dict) else {}

            if not fallback_url:
                fallback_url = str(state.get("url") or "")
                fallback_title = str(state.get("title") or "")

            if frame_fields and not url:
                # The frame that holds the form is the one that says where the
                # browser is.
                url = str(state.get("url") or "")
                title = str(state.get("title") or "")

            for name, entry in frame_fields.items():
                fields.setdefault(name, entry)

            # The page's complaint is worth more than any other frame's: the
            # frame that holds the form is the one that says what is wrong.
            frame_messages = state.get("messages")
            if isinstance(frame_messages, list) and frame_messages and not messages:
                messages = [m for m in frame_messages if isinstance(m, dict)]

        if not url:
            url, title = fallback_url, fallback_title

        return {"url": url, "title": title, "fields": fields, "messages": messages}


# The default bridge: the one the module's own names talk to. The server
# gives every visitor a bridge of their own; this is here so that the
# module can be used on its own, and so that the tests have somewhere to
# put their browser.
_default = Bridge()


def attach(context, page):
    return _default.attach(context, page)


def reset():
    return _default.reset()


def detach():
    return _default.detach()


def is_open():
    return _default.is_open()


def frames():
    return _default.frames()


def exclude_host(host):
    return _default.exclude_host(host)


def is_capture(browser_page):
    return _default.is_capture(browser_page)


def element_id(value):
    return _default.element_id(value)


async def in_frames(script, argument):
    return await _default.in_frames(script, argument)


async def write_field(name, value):
    return await _default.write_field(name, value)


async def write_at(tag, n, of, value):
    return await _default.write_at(tag, n, of, value)


async def _write_target(target, value):
    return await _default._write_target(target, value)


async def _write_now(target, value):
    return await _default._write_now(target, value)


async def _type_now(target, value):
    return await _default._type_now(target, value)


async def _has_focus(locator):
    return await _default._has_focus(locator)


async def _caret_to_end(locator):
    return await _default._caret_to_end(locator)


def _locator(frame, target):
    return _default._locator(frame, target)


def _matches(target, count):
    return _default._matches(target, count)


async def _same_page(frame, target):
    return await _default._same_page(frame, target)


async def press(name):
    return await _default.press(name)


async def press_selector(selector):
    return await _default.press_selector(selector)


async def _press_now(target):
    return await _default._press_now(target)


async def _is_disabled(locator):
    return await _default._is_disabled(locator)


def remember(target, kind, value='', held=False):
    return _default.remember(target, kind, value, held)


def forget(target):
    return _default.forget(target)


def mark_reported(target):
    return _default.mark_reported(target)


def _key(target):
    return _default._key(target)


def pending():
    return _default.pending()


async def flush_pending():
    return await _default.flush_pending()


def _schedule_flush():
    return _default._schedule_flush()


async def _flush_later():
    return await _default._flush_later()


async def controls():
    return await _default.controls()


async def describe_missing(name):
    return await _default.describe_missing(name)


async def snapshot():
    return await _default.snapshot()



def __getattr__(name):
    """Anything the module does not have comes from the default bridge."""
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(_default, name)
