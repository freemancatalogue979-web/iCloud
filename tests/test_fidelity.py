"""Capture fidelity: does the mirror look like the page it is mirroring?

Every other suite proves the protocol works - the ids line up, the ops apply,
the resources load. None of them asked the only question that matters to
somebody looking at the mirror: does it look like the real page?

So this suite captures a realistic page with the real agent, rebuilds it in a
second real browser, and compares the two: which elements survived, what the
computed styles are, where the boxes ended up, what the form controls are
holding, and whether a canvas still has its picture.

The last check is the one the whole zero-specificity stylesheet exists for:
when the live page changes a class, the mirror has to follow the page's own
CSS. A snapshot pinned onto the element as an inline style wins over every
stylesheet rule, so a pinned mirror never changes - it just sits there,
looking correct, and wrong.

Skips if no Chromium executable is available.
"""

import asyncio
import http.server
import os
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AGENT_JS = (ROOT / "agent.js").read_text(encoding="utf-8")
MIRROR_JS = (ROOT / "mirror.js").read_text(encoding="utf-8")

# The mirror is the captured page itself, with the engine that keeps it in
# step with the real browser put into its head - which is what the server
# does when it writes the capture out as a page of its own.
MIRROR_PAGE = (
    "<!DOCTYPE html><html><head><script>" + MIRROR_JS + "</script>"
    "</head><body></body></html>"
)

passed = 0
failed = 0

# A page with the things that have actually broken: the user's menu markup
# with its inline SVG, a custom element, a shadow root with a slot, CSS that
# lives in a custom property, pseudo-elements, a fixed bar, a grid, a canvas
# with pixels in it, and form controls whose live values differ from their
# attributes.
PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>iCloud</title>
<style>
  :root { --apple-blue: #0071e3; --gutter: 12px; }
  body { font-family: -apple-system, "SF Pro Text", sans-serif; margin: 0; }
  .cloudos-menu-item { display: flex; align-items: center; gap: 8px;
                       padding: var(--gutter); color: var(--apple-blue); }
  .has-icon::before { content: ""; display: inline-block; width: 18px; height: 18px;
                       background-color: rgb(255, 69, 58); border-radius: 50%; }
  .counted::after { content: " (" attr(data-n) ")"; color: gray; }
  .fixed-bar { position: fixed; top: 0; left: 0; height: 44px; width: 100%;
               background: rgba(0,0,0,0.8); z-index: 9999; }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; }
  .hidden { display: none; }
  .hot { color: rgb(255, 0, 0); }
  .late { color: rgb(0, 128, 0); padding: 7px; }
  .cool { color: rgb(0, 0, 255); }
  .styled { color: rgb(0, 153, 0); }
</style>
</head>
<body>
  <div class="fixed-bar" id="bar">header</div>

  <ui-menu-scroll-container role="menu" id="menu">
    <a role="menuitem" href="https://support.apple.com/guide/icloud/welcome/icloud" class="blue">
      <div class="cloudos-menu-item cloudos-menu-item-link has-icon counted" data-n="3" id="item1">
        iCloud Help
        <svg viewBox="0 0 268.02 158.12" id="icon">
          <path transform="matrix(1,0,0,-1,0,158.12)" d="M0 0 L10 10 Z" fill="currentColor"/>
        </svg>
      </div>
    </a>
  </ui-menu-scroll-container>

  <div class="grid"><span>a</span><span>b</span><span>c</span></div>
  <div class="hidden" id="hid">invisible</div>
  <div id="host">light-dom-fallback</div>
  <div id="styled" class="styled">styled by css</div>

  <canvas id="cv" width="30" height="30"></canvas>

  <form id="form">
    <input id="text" type="text" value="typed value">
    <input id="chk" type="checkbox" checked>
    <select id="sel"><option value="one">one</option><option value="two">two</option></select>
    <textarea id="ta">area text</textarea>
  </form>

<script>
  const host = document.getElementById('host');
  const sr = host.attachShadow({ mode: 'open' });
  sr.innerHTML = '<style>:host{display:block} .in{color:rgb(0,0,255)}</style>'
               + '<div class="in">shadow text</div><slot></slot>';
  // A constructable stylesheet is not markup and does not serialise, so
  // anything it styles can only reach the mirror through the snapshot.
  const sheet = new CSSStyleSheet();
  sheet.replaceSync('.late { color: rgb(0, 128, 0); }');
  sr.adoptedStyleSheets = [sheet];
  const cv = document.getElementById('cv');
  const c2 = cv.getContext('2d');
  c2.fillStyle = 'rgb(0, 200, 0)';
  c2.fillRect(0, 0, 30, 30);
  document.getElementById('text').value = 'changed by script';
  document.getElementById('sel').value = 'two';
</script>
</body></html>"""


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + detail) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.split("?")[0] == "/1.html":
            body, ctype = MIRROR_PAGE.encode(), "text/html; charset=utf-8"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def find_chromium():
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env, os.environ.get("CHROME_LD_PATH", "")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and Path(path).exists():
                return path, ""
    except Exception:
        pass
    return None, ""


# The snapshot of an element, as the browser sees it. Used to compare the
# page against the mirror that was rebuilt from its capture.
PROBE = """() => {
  const out = {};
  const all = document.querySelectorAll('*');
  const seen = {};
  for (const el of all) {
    if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
    // Identity by tag and id, with a counter for the ones that repeat.
    let base = el.tagName + (el.id ? '#' + el.id : '');
    seen[base] = (seen[base] || 0) + 1;
    const key = base + (seen[base] > 1 ? '#' + seen[base] : '');
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const before = getComputedStyle(el, '::before');
    out[key] = {
      tag: el.tagName,
      rect: [Math.round(r.x * 100) / 100, Math.round(r.y * 100) / 100,
             Math.round(r.width * 100) / 100, Math.round(r.height * 100) / 100],
      display: cs.display,
      color: cs.color,
      background: cs.backgroundColor,
      position: cs.position,
      fontSize: cs.fontSize,
      padding: cs.paddingTop + ' ' + cs.paddingRight + ' ' + cs.paddingBottom + ' ' + cs.paddingLeft,
      beforeContent: before.content,
      beforeWidth: before.width,
      value: 'value' in el ? String(el.value) : null,
      checked: 'checked' in el ? !!el.checked : null,
      selected: el.tagName === 'OPTION' ? el.selected : null,
    };
  }
  const cv = document.querySelector('canvas');
  out['__canvas__'] = cv ? cv.toDataURL() : null;
  const host = document.getElementById('host');
  out['__shadow__'] = host && host.shadowRoot
    ? Array.from(host.shadowRoot.childNodes)
        .filter(n => !(n.nodeType === 1 && n.tagName === 'STYLE'))
        .map(n => n.textContent).join('')
    : null;
  out['__lightdom__'] = host ? host.textContent : null;
  return out;
}"""


async def run():
    from playwright.async_api import async_playwright

    exe, ld_path = find_chromium()
    if not exe:
        return None

    env = dict(os.environ)
    if ld_path:
        env["LD_LIBRARY_PATH"] = ld_path + os.pathsep + env.get("LD_LIBRARY_PATH", "")

    port = free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=exe,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            env=env,
        )
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        emitted = []

        def collect(source, message):
            emitted.append(message)

        await ctx.expose_binding("__rbEmit", collect)
        await ctx.add_init_script(AGENT_JS)
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:%d/" % port, wait_until="load")
        await page.wait_for_timeout(400)

        original = await page.evaluate(PROBE)
        frame = await page.evaluate(
            "() => window.__rbInternals.serialiseDocumentOf(document)"
        )
        html = frame["html"]
        print("     captured %d bytes" % len(html))

        # ---- rebuild the capture in a second, clean browser -----------------
        mirror_ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        mirror_page = await mirror_ctx.new_page()
        await mirror_page.goto("http://127.0.0.1:%d/1.html" % port, wait_until="load")
        # The real mirror, the real seeding path.
        await mirror_page.evaluate(
            "(a) => window.seed(a.frames, a.viewport)",
            {"frames": [{"windowId": "0", **a} for a in [frame]],
             "viewport": {"w": 1280, "h": 800}},
        )
        await mirror_page.wait_for_timeout(400)
        rebuilt = await mirror_page.evaluate(PROBE)

        # ---- elements -----------------------------------------------------
        missing = [k for k in original if k not in rebuilt and not k.startswith("__")]
        extra = [k for k in rebuilt if k not in original and not k.startswith("__")]
        check(
            "every element of the page is in the mirror",
            not missing,
            "missing: %s" % missing[:4],
        )
        check("the mirror invents no elements", not extra, "extra: %s" % extra[:4])

        # ---- computed styles and geometry ---------------------------------
        style_diffs = []
        geom_diffs = []
        for key, a in original.items():
            if key.startswith("__") or key not in rebuilt:
                continue
            b = rebuilt[key]
            for field in ("display", "color", "background", "position", "fontSize",
                          "padding", "beforeContent", "beforeWidth",
                          "value", "checked", "selected"):
                if a[field] != b[field]:
                    style_diffs.append("%s %s: %r vs %r" % (key, field, a[field], b[field]))
            # A canvas in a scriptless sandbox is not a replaced element, so
            # the mirror makes it an inline-block to give it a box. That one
            # difference is allowed; the size it ends up with is not.
            if a["tag"] == "CANVAS" and a["display"] == "inline" and b["display"] == "inline-block":
                style_diffs = [d for d in style_diffs if not d.startswith(key + " display")]
            if a["rect"] != b["rect"]:
                geom_diffs.append("%s %s vs %s" % (key, a["rect"], b["rect"]))
        check(
            "computed styles match on every element",
            not style_diffs,
            "; ".join(style_diffs[:5]),
        )
        check("every box is in the same place", not geom_diffs, "; ".join(geom_diffs[:5]))

        # ---- the capture must not be mostly noise -------------------------
        # Every element used to carry the browser's own defaults - flex-grow,
        # flex-shrink, order, outline-width, four border colours - because
        # the filter list was written against jsdom, which does not report
        # them at all. That was most of the capture, saying nothing.
        # A declaration that shows up on most elements is a default leaking,
        # not a style worth carrying.
        nodes = html.count("data-rb-nid")
        noise = [
            d for d in ("flex-grow:0", "flex-shrink:1", "order:0", "outline-width:3px",
                        "border-top-color", "transform-origin:")
            if html.count(d) > max(2, nodes // 3)
        ]
        check("the snapshot carries no default-valued declarations",
              not noise, "found: %s" % noise)
        check("the capture is not mostly style",
              len(html) / max(nodes, 1) < 400,
              "%d bytes for %d nodes" % (len(html), nodes))

        # ---- the picture on a canvas --------------------------------------
        check(
            "a canvas keeps its picture",
            original["__canvas__"] == rebuilt["__canvas__"],
            "original %d chars, mirror %d chars"
            % (len(original["__canvas__"] or ""), len(rebuilt["__canvas__"] or "")),
        )

        # ---- shadow content and the light DOM it shows ---------------------
        check(
            "the shadow root keeps its content",
            original["__shadow__"] == rebuilt["__shadow__"],
            "%r vs %r" % (original["__shadow__"], rebuilt["__shadow__"]),
        )
        check(
            "light DOM shown through a slot is kept",
            original["__lightdom__"] == rebuilt["__lightdom__"],
            "%r vs %r" % (original["__lightdom__"], rebuilt["__lightdom__"]),
        )

        # ---- the author's own inline style survives ------------------------
        styled = await page.evaluate(
            "() => { const e = document.getElementById('styled');"
            " e.setAttribute('style', 'color: rgb(1, 2, 3)'); return e.getAttribute('style'); }"
        )
        html2 = await page.evaluate(
            "() => window.__rbInternals.serialiseDocumentOf(document).html"
        )
        check(
            "an element's own inline style is not overwritten by the snapshot",
            'style="color: rgb(1, 2, 3)"' in html2,
            "inline style was %r" % styled,
        )
        check(
            "the snapshot does not land on the element as an inline style",
            'style="color: rgb(0, 153, 0)"' not in html2,
            "the snapshot was written onto the element",
        )

        # ---- a live change has to reach the mirror ------------------------
        # This is what the snapshot must not be allowed to block: the page
        # changes a class, the page's own CSS decides the new colour, and
        # the mirror has to show the new colour too.
        page2 = await ctx.new_page()
        await page2.goto("http://127.0.0.1:%d/" % port, wait_until="load")
        await page2.wait_for_timeout(300)
        await page2.evaluate(
            "() => { document.getElementById('styled').className = 'hot'; }"
        )
        await page2.wait_for_timeout(200)
        hot = await page2.evaluate(
            "() => getComputedStyle(document.getElementById('styled')).color"
        )

        live_page = await mirror_ctx.new_page()
        await live_page.goto("http://127.0.0.1:%d/1.html" % port, wait_until="load")
        await live_page.evaluate(
            "(a) => window.seed(a.frames, a.viewport)",
            {"frames": [{"windowId": "0", **a} for a in [frame]],
             "viewport": {"w": 1280, "h": 800}},
        )
        await live_page.wait_for_timeout(400)
        cold = await live_page.evaluate(
            "() => getComputedStyle(document.getElementById('styled')).color"
        )
        await live_page.evaluate(
            "() => { document.getElementById('styled').className = 'hot'; }"
        )
        await live_page.wait_for_timeout(100)
        warmed = await live_page.evaluate(
            "() => getComputedStyle(document.getElementById('styled')).color"
        )
        check(
            "a class the page toggles changes the mirror",
            cold != warmed and warmed == hot,
            "before %r, after %r, page %r" % (cold, warmed, hot),
        )

        # ---- an element added later gets its snapshot too -----------------
        # The page changes, so a fresh capture is taken, and the mirror shows
        # that capture. The new node's resolved styles travel with it.
        emitted.clear()
        await page2.evaluate(
            """() => {
                 const d = document.createElement('div');
                 d.id = 'late';
                 d.className = 'late';
                 d.textContent = 'late arrival';
                 document.body.appendChild(d);
               }"""
        )
        await page2.wait_for_timeout(1500)
        docs = [m for m in emitted if m.get("kind") == "doc"]
        check("the page's addition produces a fresh capture", bool(docs),
              "no doc message: %r" % emitted[-2:])
        if docs:
            capture = docs[-1]
            await live_page.evaluate(
                "(a) => window.seed(a.frames, a.viewport)",
                {"frames": capture["frames"], "viewport": capture.get("viewport")},
            )
        await live_page.wait_for_timeout(200)
        late_page = await page2.evaluate(
            """() => { const e = document.getElementById('late');
                 const cs = getComputedStyle(e);
                 return { color: cs.color, display: cs.display,
                          rect: [e.getBoundingClientRect().width] }; }"""
        )
        late_mirror = await live_page.evaluate(
            """() => { const e = document.getElementById('late');
                 if (!e) return null;
                 const cs = getComputedStyle(e);
                 return { color: cs.color, display: cs.display,
                          rect: [e.getBoundingClientRect().width] }; }"""
        )
        check("the added element reaches the mirror", late_mirror is not None,
              "not in the mirror")
        check(
            "the added element looks the same in the mirror",
            late_mirror == late_page,
            "page %r, mirror %r" % (late_page, late_mirror),
        )

        # ---- an element added inside a shadow root -----------------------
        # Its style comes from a constructable stylesheet, which is not
        # markup, so the only way it can reach the mirror is the snapshot
        # the capture carries.
        emitted.clear()
        await page2.evaluate(
            """() => {
                 const host = document.getElementById('host');
                 const d = document.createElement('div');
                 d.className = 'late';
                 d.textContent = 'in the shadow';
                 host.shadowRoot.appendChild(d);
               }"""
        )
        await page2.wait_for_timeout(1500)
        docs = [m for m in emitted if m.get("kind") == "doc"]
        check("a change inside a shadow root produces a capture", bool(docs),
              "no doc message: %r" % emitted[-2:])
        if docs:
            capture = docs[-1]
            await live_page.evaluate(
                "(a) => window.seed(a.frames, a.viewport)",
                {"frames": capture["frames"], "viewport": capture.get("viewport")},
            )
        await live_page.wait_for_timeout(200)
        shadow_page = await page2.evaluate(
            """() => { const e = document.getElementById('host').shadowRoot.querySelector('.late');
                 return e ? getComputedStyle(e).color : null; }"""
        )
        shadow_mirror = await live_page.evaluate(
            """() => { const h = document.getElementById('host');
                 const e = h && h.shadowRoot && h.shadowRoot.querySelector('.late');
                 if (!e) return null;
                 return { color: getComputedStyle(e).color,
                          nid: e.getAttribute('data-rb-nid'),
                          width: getComputedStyle(e).width,
                          inShadow: e.getRootNode() === h.shadowRoot }; }"""
        )
        check(
            "an element added in a shadow root reaches the mirror",
            shadow_mirror is not None,
            "not in the mirror",
        )
        check(
            "an element added in a shadow root is styled in the mirror",
            shadow_mirror is not None
            and shadow_mirror.get("color") == shadow_page
            and shadow_mirror.get("color") == "rgb(0, 128, 0)",
            "page %r, mirror %r" % (shadow_page, shadow_mirror),
        )

        await browser.close()
    httpd.shutdown()
    return 0


def main():
    result = asyncio.run(run())
    if result is None:
        print("SKIPPED: no Chromium executable available")
        return 0
    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
