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
_WRITE = """([id, value]) => {
  const el = document.getElementById(id);
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

# A press is a real click, not a dispatch of a synthetic event: the page's own
# handlers have to run, and the browser has to treat it as a user.
_PRESS = """(id) => {
  const el = document.getElementById(id);
  if (!el) return false;
  el.click();
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
  return { url: location.href, title: document.title, fields };
}"""

# The browser the server opened. ``context`` is a Playwright BrowserContext
# and ``page`` the page it started on; either can be None.
_context = None
_page = None

# One write at a time. Two writes in flight at once reach the browser in
# whatever order the browser gets to them, and the text comes out scrambled -
# the user types an address and then a password, and the password lands in
# the address field.
_lock = asyncio.Lock()


def attach(context, page) -> None:
    """The browser the app drives."""
    global _context, _page
    _context = context
    _page = page


def detach() -> None:
    """The browser has gone."""
    global _context, _page
    _context = None
    _page = None


def is_open() -> bool:
    return _context is not None or _page is not None


def frames() -> list:
    """Every frame of every page the browser has open.

    The newest page first: the sign-in flow opens a tab of its own, and that
    tab is the page the user is looking at, so it is the page the capture is
    talking about. A frame the older page holds is still searched - the
    field may be there and not in the new one - but the new one is asked
    first.
    """
    pages = []
    if _context is not None:
        try:
            pages = list(_context.pages)
        except Exception:
            pages = []
    if not pages and _page is not None:
        pages = [_page]

    out = []
    for browser_page in reversed(pages):
        try:
            out.extend(browser_page.frames)
        except Exception:
            continue
    return out


def element_id(value) -> str:
    """The id of a control, if it is one we are willing to act on."""
    if not isinstance(value, str) or not ELEMENT_ID.match(value):
        return ""
    return value


async def in_frames(script: str, argument) -> bool:
    """Run a script in each frame until one of them answers True.

    A frame that is navigating, detached or crashed is not an error worth
    passing on: the control is somewhere, and the next frame may be where it
    is.
    """
    for frame in frames():
        try:
            if await frame.evaluate(script, argument):
                return True
        except Exception:
            continue
    return False


async def write_field(name: str, value: str) -> bool:
    """Write a value into the browser, where the capture's field maps to it.

    A value that has nowhere to go yet is kept, and tried again: the
    sign-in widget is a frame Apple loads a second or two after Sign In is
    clicked, so a value typed straight away would otherwise be lost.
    """
    name = element_id(name)
    if not name:
        return False
    if not isinstance(value, str):
        value = ""
    value = value[:MAX_VALUE]

    written = await _write_now(name, value)
    if written:
        forget(name)
    else:
        remember(name, "field", value)
    return written


async def _write_now(name: str, value: str) -> bool:
    async with _lock:
        return await in_frames(_WRITE, [name, value])


async def press(name: str) -> bool:
    """Press a control in the browser, where the capture's button maps to it.

    A press that has nowhere to go yet is kept, and tried again - but only
    for a little while: a button pressed long after the person pressed it
    is not what they asked for.
    """
    name = element_id(name)
    if not name:
        return False

    pressed = await _press_now(name)
    if pressed:
        forget(name)
    else:
        remember(name, "press", "")
    return pressed


async def _press_now(name: str) -> bool:
    async with _lock:
        return await in_frames(_PRESS, name)


# --------------------------------------------------------------------------
# The ones that had nowhere to go yet
#
# The browser is not ready the moment the app is: the sign-in widget is a
# frame on another origin, and Apple loads it a second or two after Sign In
# is clicked. A value typed in that window has no field to go into, and a
# button pressed in it has no button. Both are kept here and tried again,
# so the person does not have to press it twice.
# --------------------------------------------------------------------------

# How long a value waits for its field, and a press waits for its button.
PENDING_FIELD_TTL = 120.0
PENDING_PRESS_TTL = 20.0

_pending: dict[str, dict] = {}


def remember(name: str, kind: str, value: str = "") -> None:
    """Keep something that had nowhere to go, so it can be tried again."""
    _pending[name] = {"kind": kind, "value": value, "at": time.monotonic()}


def forget(name: str) -> None:
    _pending.pop(name, None)


def pending() -> dict:
    """What is waiting, for the console and the tests."""
    return {name: dict(entry) for name, entry in _pending.items()}


async def flush_pending() -> None:
    """Try again the values and presses that had nowhere to go yet."""
    if not _pending:
        return

    now = time.monotonic()
    for name, entry in list(_pending.items()):
        ttl = PENDING_PRESS_TTL if entry["kind"] == "press" else PENDING_FIELD_TTL
        if now - entry["at"] > ttl:
            _pending.pop(name, None)
            continue

        if entry["kind"] == "press":
            if await _press_now(name):
                _pending.pop(name, None)
            continue

        if await _write_now(name, entry["value"]):
            _pending.pop(name, None)


# The ids the browser has for the things we can act on. It is what turns
# "not found" into something that can be read: a control that is there
# under another name, or a browser that has not loaded the widget yet.
_CONTROLS = """() => {
  const nodes = document.querySelectorAll(
    'input, textarea, select, button, [role=button], a[href]');
  const ids = [];
  for (const el of nodes) {
    if (el.id) ids.push(el.id);
  }
  return ids.slice(0, 60);
}"""


async def controls() -> list:
    """The ids the browser has for the things we can act on."""
    found: list = []
    for frame in frames():
        try:
            ids = await frame.evaluate(_CONTROLS)
        except Exception:
            continue
        for name in ids or []:
            if name not in found:
                found.append(name)
    return found


async def describe_missing(name: str) -> str:
    """What the browser has, so a mismatch can be read rather than guessed."""
    ids = await controls()
    if not ids:
        return "the browser has no controls yet - the sign-in widget may still be loading"
    near = [i for i in ids if name.lower() in i.lower() or i.lower() in name.lower()]
    hint = ("; closest: " + ", ".join(near[:5])) if near else ""
    return "the browser has: " + ", ".join(ids[:12]) + hint


async def snapshot() -> dict:
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
    url = title = ""
    fallback_url = fallback_title = ""

    for frame in frames():
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

    if not url:
        url, title = fallback_url, fallback_title

    return {"url": url, "title": title, "fields": fields}
