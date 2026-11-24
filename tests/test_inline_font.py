"""The font a capture carries inside itself.

Run with: python tests/test_inline_font.py   (or `npm test`)

What is pinned here is `inline_font.py`: that every page in src/ carries the
font (the page and the frame it carries, each a document of its own), that the
bytes it carries are the font file itself, that the page is told to read in
Montserrat, and that running the script again changes nothing - a refresh of
the capture should cost one command, not a diff of base64.

No browser and no server: the pages are read from disk, as the app reads them.
"""

import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import inline_font  # noqa: E402

passed = 0
failed = 0

FONT = ROOT / "fonts" / "montserrat-latin.woff2"
PAGES = sorted((ROOT / "src").glob("*.html"))


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + str(detail)) if detail else ""))


def blocks(text):
    """Every font block in a page, the page's and its frame's."""
    return [m.group(0) for m in re.finditer(
        re.escape(inline_font.MARK) + r".*?</style>", text, re.S)]


def main():
    font = FONT.read_bytes()

    check("the font file is there", FONT.exists() and len(font) > 10000,
          len(font) if FONT.exists() else "missing")
    check("...and it is a woff2", font[:4] == b"wOF2", font[:4])

    check("there are captures to carry it", len(PAGES) >= 4,
          [p.name for p in PAGES])

    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        found = blocks(text)

        # The page, and the frame inside it: a <iframe srcdoc> is a document of
        # its own and does not inherit the page's faces.
        check(page.name + " carries the font, and its frame too",
              len(found) == 2, len(found))

        if not found:
            continue

        carried = re.search(r"base64,([A-Za-z0-9+/=]+)", found[0])
        check(page.name + " carries the font file itself",
              bool(carried) and base64.b64decode(carried.group(1)) == font,
              len(carried.group(1)) if carried else "no data URI")

        check(page.name + " carries no address to fetch a font from",
              "http" not in found[0] and "/fonts/" not in found[0],
              found[0][:120])

        check(page.name + " asks for Montserrat, on every element",
              "font-family:'Montserrat'" in found[0] and
              "html,body,body *{font-family:" in found[0] and
              "!important" in found[0], found[0][-160:])

        # Nothing to fetch means nothing to arrive late: the first line drawn
        # is already in Montserrat.
        check(page.name + " does not swap a fallback in first",
              "font-display:block" in found[0] and
              "swap" not in found[0], found[0][:200])

    # A copy is never doubled, however many times the script is run.
    page = PAGES[0]
    text = page.read_text(encoding="utf-8")
    once = inline_font.render(text, font)
    twice = inline_font.render(once, font)
    check("running it again changes nothing", once == twice and once == text,
          (len(once), len(twice), len(text)))
    check("...and what it wants is what is on disk", once == text)

    # A page with no <head>, and a frame with no <head> of its own: the block
    # still lands where the browser will read it.
    bare = '<!DOCTYPE html><html><meta charset=utf-8><p id=x>hi</p></html>'
    out = inline_font.render(bare, font)
    check("a page with no <head> still gets the font",
          out.startswith("<!DOCTYPE html><html><meta charset=utf-8>" +
                         inline_font.MARK), out[:80])

    framed = '<html><body><iframe srcdoc="<meta charset=utf-8><p>x"></iframe>'
    out = inline_font.render(framed, font)
    check("a frame with no <head> gets it too", out.count(inline_font.MARK) == 2,
          out.count(inline_font.MARK))

    # What it writes can be taken back out, which is what makes it safe to run
    # over a page that already has it.
    check("the block can be taken back out",
          page.read_text(encoding="utf-8").count(inline_font.MARK) == 2 and
          inline_font.MARK not in inline_font.without_block(text))

    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
