/* The markup from the bug report: a custom element wrapping inline SVG icons.
 * SVG lives in its own XML namespace, which innerHTML round-trips can lose. */
const fs = require("fs");
const { JSDOM } = require("jsdom");
const AGENT = fs.readFileSync("/home/user/iCloud/agent.js", "utf8");
const VIEW = fs.readFileSync("/home/user/iCloud/view.html", "utf8").split("<script>")[1].split("</script>")[0];

const PAGE = `<!DOCTYPE html><html><body>
<ui-menu-scroll-container role="menu" style="position: relative;">
  <a role="menuitem" href="https://support.apple.com/guide/icloud/welcome/icloud" target="_blank" rel="noreferrer" aria-label="iCloud User Guide (opens in a new tab)" tabindex="-1" class="blue">
    <div class="cloudos-menu-item cloudos-menu-item-link">
      <svg viewBox="0 0 268.0201416015625 158.116943359375" version="1.1" xmlns="http://www.w3.org/2000/svg" class="symbol-icon layout-box" aria-hidden="true" style="height: 22.4407px; width: 38.0386px;">
        <g transform="matrix(1 0 0 1 75.46503906249995 114.2884521484375)">
          <path d="M58.5449 14.5508C85.791 14.5508 108.35-8.05664 108.35-35.2539Z"></path>
        </g>
      </svg>
      <div class="label" aria-hidden="true">iCloud User Guide</div>
    </div>
  </a>
</ui-menu-scroll-container>
</body></html>`;

let passed = 0, failed = 0;
const check = (n, c, d) => { if (c) { passed++; console.log("ok   " + n); } else { failed++; console.log("FAIL " + n + (d ? "\n     " + d : "")); } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const src = new JSDOM(PAGE, { runScripts: "outside-only", url: "https://www.icloud.com/" });
  const sent = [];
  src.window.__rbEmit = (m) => sent.push(m);
  src.window.eval(AGENT);
  await sleep(300);

  const frame = sent.find((m) => m.kind === "doc").frames[0];
  const html = frame.html;

  check("custom element survives", /<ui-menu-scroll-container/.test(html));
  check("inline svg survives", /<svg/.test(html));
  check("svg path data survives", /M58\.5449 14\.5508/.test(html));
  check("transform matrix survives", /matrix\(1 0 0 1 75\.465/.test(html));
  check("viewBox survives", /viewBox="0 0 268\.02/.test(html));
  check("svg child elements get ids",
    (html.match(/<path[^>]*data-rb-nid=/) || []).length === 1,
    (html.match(/<path[^>]*>/) || ["(no path)"])[0]);
  check("anchor href absolutised, not proxied",
    /href="https:\/\/support\.apple\.com\/guide\/icloud\/welcome\/icloud"/.test(html));
  check("target and aria attributes kept", /target="_blank"/.test(html) && /aria-label="iCloud User Guide/.test(html));

  // Round-trip through the real applier: add a second icon and check the SVG
  // comes back in the SVG namespace rather than as inert HTML elements.
  const mir = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:9002/view" });
  mir.window.WebSocket = function () { this.readyState = 1; this.send = () => {}; };
  mir.window.eval(VIEW);
  const ids = new Map();
  mir.window.adopt(mir.window.document, ids);

  const parsedSvg = mir.window.document.querySelector("svg");
  check("svg is in the SVG namespace after the trip",
    parsedSvg && parsedSvg.namespaceURI === "http://www.w3.org/2000/svg",
    parsedSvg && parsedSvg.namespaceURI);
  check("svg path is in the SVG namespace",
    mir.window.document.querySelector("path").namespaceURI === "http://www.w3.org/2000/svg");
  check("path d attribute survived the trip",
    mir.window.document.querySelector("path").getAttribute("d").startsWith("M58.5449"));

  // Now stream an added SVG icon into the mirror.
  const label = src.window.document.querySelector(".label");
  const newSvg = src.window.document.createElementNS("http://www.w3.org/2000/svg", "svg");
  newSvg.setAttribute("viewBox", "0 0 10 10");
  const circle = src.window.document.createElementNS("http://www.w3.org/2000/svg", "circle");
  circle.setAttribute("r", "4");
  newSvg.appendChild(circle);
  label.appendChild(newSvg);
  await sleep(250);

  const ops = sent.filter((m) => m.kind === "mut").flatMap((m) => m.ops || []);
  check("added svg emitted an op", ops.length > 0, JSON.stringify(ops).slice(0, 200));
  const entry = { doc: mir.window.document, ids };
  const applied = mir.window.applyOps(entry, ops);
  check("op applied cleanly", applied);

  const mirrorCircle = mir.window.document.querySelector(".label circle");
  check("added svg reached the mirror", !!mirrorCircle);
  check("added svg kept its namespace",
    mirrorCircle && mirrorCircle.namespaceURI === "http://www.w3.org/2000/svg",
    mirrorCircle && mirrorCircle.namespaceURI);
  check("added svg kept its attributes",
    mirrorCircle && mirrorCircle.getAttribute("r") === "4");
  check("added svg is addressable by id",
    ops.some((o) => o.op === "add") && ids.size > 0);

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
