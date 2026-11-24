#!/usr/bin/env python3
"""The font, carried inside the capture.

The engine can hand a page its font at run time, and it does - but a font that
arrives *after* the page has been drawn is a page that flashes: the fallback is
laid out first, then Montserrat arrives and every line is measured again. One
way to be sure nothing flashes is to wait for the font before drawing anything;
the other is to have nothing to wait for. This script does the second: the font
travels inside the page, as an `@font-face` whose source is the font file
itself, base64, in a `data:` URL.

What that buys:

  * nothing to fetch, so nothing to arrive late - the bytes are in the document
    before the first line of text is laid out, and the first line is already in
    Montserrat;
  * a page that still works with no server at all: `src/2.html` opened straight
    from disk reads in Montserrat, because it carries its own font;
  * the frame a capture carries (`<iframe srcdoc="...">`, the sign-in form) is
    a document of its own and does not inherit the page's faces, so it is given
    the same block in its own head.

The capture in `src/` is what a refresh of it replaces, so run this again after
a capture is refreshed:

    python3 inline_font.py              # every src/*.html
    python3 inline_font.py src/2.html   # just one
    python3 inline_font.py --check      # exit 1 if a page does not carry it

It is safe to run twice: a block this script wrote is replaced, never doubled.
"""

import base64
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONT_DIR = HERE / "fonts"
FONT_FILE = FONT_DIR / "montserrat-latin.woff2"

# The face and the rule that uses it, as one <style>. Single quotes throughout
# and no `&`, so the same text can sit inside a `srcdoc="..."` attribute as-is.
MARK = "<style data-icloud=font>"
STACK = "'Montserrat', 'Helvetica Neue', Helvetica, Arial, sans-serif"

font_cache = {}


def font_bytes(path=FONT_FILE):
    """The font file's bytes, read once per run."""
    path = Path(path)
    if path not in font_cache:
        font_cache[path] = path.read_bytes()
    return font_cache[path]


def block(font=None):
    """The <style> a page carries: the face, and the rule that asks for it.

    `font-display: block` rather than `swap`: there is no fetch to hide, and a
    short invisible start is what keeps a fallback from being drawn at all.
    """
    data = base64.b64encode(font if font is not None else font_bytes()).decode("ascii")
    rules = [
        "@font-face{",
        "font-family:'Montserrat';",
        "font-style:normal;",
        "font-weight:100 900;",
        "font-display:block;",
        "src:url('data:font/woff2;base64," + data + "') format('woff2')",
        "}",
        # !important, and on every element: Apple's stylesheet names a font on
        # almost every element, and a font named on the element itself is not
        # inherited over.
        "html,body,body *{font-family:" + STACK + "!important}",
    ]
    return MARK + "".join(rules) + "</style>"


def without_block(text):
    """The page with no font block of ours in it (so it can be put back)."""
    out = []
    at = 0
    while True:
        start = text.find(MARK, at)
        if start < 0:
            out.append(text[at:])
            break
        end = text.find("</style>", start)
        if end < 0:
            out.append(text[at:])
            break
        out.append(text[at:start])
        at = end + len("</style>")
    return "".join(out)


def srcdocs(text):
    """Where the values of the page's `srcdoc` attributes begin and end."""
    found = []
    at = 0
    while True:
        name = text.find("srcdoc=", at)
        if name < 0:
            return found
        i = name + len("srcdoc=")
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] not in "\"'":
            at = i
            continue
        quote = text[i]
        start = i + 1
        end = text.find(quote, start)
        if end < 0:
            return found
        found.append((start, end))
        at = end + 1


def head_of(text, before=None):
    """Where a <style> can go: just after the charset, the head, the <html>.

    `before` limits the search to the part of the text that is this document -
    a frame's markup also has a `<meta charset>`, and that one is the frame's.
    """
    limit = len(text) if before is None else before

    for opener in ("<meta charset", "<head", "<html"):
        at = text.find(opener, 0, limit)
        if at < 0:
            continue
        end = text.find(">", at)
        if end < 0 or end >= limit:
            continue
        return end + 1
    return 0


def render(text, font=None):
    """The page, with the font block in it and in the frame it carries."""
    html = block(font) if font is not None else block()

    text = without_block(text)

    # This document's own head first: the frame's <meta charset> must not be
    # mistaken for it, and inserting early moves nothing that comes later.
    frame_at = text.find("srcdoc=")
    cut = len(text) if frame_at < 0 else frame_at
    at = head_of(text, cut)
    text = text[:at] + html + text[at:]

    # Then every frame it carries, from the last to the first, so the earlier
    # positions are still the ones we measured.
    for start, end in reversed(srcdocs(text)):
        frame = text[start:end]
        spot = head_of(frame)
        text = text[:start] + frame[:spot] + html + frame[spot:] + text[end:]

    return text


def pages(args):
    if not args:
        return sorted((HERE / "src").glob("*.html"))
    return [Path(a) for a in args]


def main(argv):
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]

    if not FONT_FILE.exists():
        print("no font at " + str(FONT_FILE), file=sys.stderr)
        return 1

    stale = []
    for path in pages(args):
        if not path.exists():
            print("no such page: " + str(path), file=sys.stderr)
            return 1
        before = path.read_text(encoding="utf-8")
        after = render(before)
        if after == before:
            if not check:
                print("  " + str(path) + " - already carries the font")
            continue
        if check:
            stale.append(str(path))
            print("  " + str(path) + " - does not carry the font")
            continue
        path.write_text(after, encoding="utf-8")
        print("  " + str(path) + " - font carried inline")

    if stale:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
