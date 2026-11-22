/* Runs agent.js inside a real DOM (jsdom) and checks the parts that a stub
 * cannot: shadow-root serialisation, frame windowId minting, and the
 * lockstep clone walk. */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const AGENT = fs.readFileSync("/home/user/iCloud/agent.js", "utf8");

const PAGE = `<!DOCTYPE html>
<html>
<head>
  <base href="https://www.icloud.com/">
  <style>.hero{background:url(img/bg.png)}</style>
  <script>window.leaked = true</script>
</head>
<body>
  <img src="logo.png">
  <div onclick="evil()"><a href="/signin">go</a></div>
  <div id="host"></div>
  <iframe src="https://idmsa.apple.com/appleauth/auth/authorize/signin"></iframe>
  <iframe src="https://example.com/other"></iframe>
</body>
</html>`;

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log("ok   " + name); }
  else { failed++; console.log("FAIL " + name + (detail ? "\n     " + detail : "")); }
}

const dom = new JSDOM(PAGE, { runScripts: "outside-only", url: "https://www.icloud.com/" });
const win = dom.window;

// The real page's own web component: its visible markup lives in a shadow root,
// which is exactly what outerHTML drops.
const host = win.document.getElementById("host");
const sr = host.attachShadow({ mode: "open" });
const btn = win.document.createElement("button");
btn.className = "sign-in-button";
btn.textContent = "Sign In";
sr.appendChild(btn);

// Give the agent the binding it expects.
win.__rbEmit = () => {};

win.eval(AGENT);
const I = win.__rbInternals;

// ---- collectFrames: positional windowIds ---------------------------------
const ids = I.collectFrames();
const entries = [...ids.entries()].map(([el, id]) => [id, el.getAttribute("src")]);
check("both frames found", entries.length === 2, JSON.stringify(entries));

// The id written onto an element is the same id used to key its content, which
// is the invariant the mirror depends on. Both come from this one map.
const byId = new Map(entries);
check("windowIds are positional and unique",
  byId.has("0.0") && byId.has("0.1"),
  JSON.stringify(entries));
check("the auth iframe got an id",
  [...byId.values()].some((s) => /idmsa\.apple\.com/.test(s || "")),
  JSON.stringify(entries));

// ---- path: shadow roots are addressable ----------------------------------
check("elements get stable ids", !!I.ensureId(win.document.querySelector("img")));
check("the same element keeps its id",
  I.ensureId(btn) === I.ensureId(btn),
  "identity must be stable across calls");

// ---- serialiseDocument ----------------------------------------------------
const captured = I.serialiseDocumentOf(win.document, I.collectFrames());
const html = captured.html;

check("shadow content survives serialisation", /Sign In/.test(html),
  "the sign-in control lives only in a shadow root");
check("shadow root becomes declarative", /<template shadowrootmode="open">/.test(html));
check("<base> removed (SingleFile cleanupPage does this)", !/<base/i.test(html));
check("<script> removed", !/<script/i.test(html));
check("inline event handlers removed", !/onclick/i.test(html));
check("relative img src proxied", /src="\/res\?url=https%3A%2F%2Fwww\.icloud\.com%2Flogo\.png"/.test(html), html.slice(0, 400));
// href means two things: a resource on <link>, navigation on <a>. Only the
// first goes through the proxy.
check("<a href> absolutised, NOT proxied", /href="https:\/\/www\.icloud\.com\/signin"/.test(html),
  (html.match(/<a [^>]*>/) || ["(no a)"])[0]);
check("no anchor href was turned into a /res fetch",
  !/<a [^>]*href="\/res\?url=/.test(html));
check("<link href> IS proxied", /<link[^>]*href="\/res\?url=/.test(html) || !/<link/.test(html));
// proxyCss preserves whatever quoting the author used; this one had none.
check("url() inside <style> proxied", /url\(\/res\?url=https%3A%2F%2Fwww\.icloud\.com%2Fimg%2Fbg\.png\)/.test(html),
  (html.match(/<style[^>]*>[\s\S]*?<\/style>/) || ["(no style)"])[0]);
check("frame elements are tagged with their windowId",
  (html.match(/data-rb-frame="0\.\d"/g) || []).length === 2,
  (html.match(/data-rb-frame="[^"]*"/g) || []).join(" "));
check("frame src removed so the mirror controls it",
  !/src="https:\/\/idmsa\.apple\.com/.test(html));
check("frames sandboxed to same-origin only",
  (html.match(/sandbox="allow-same-origin"/g) || []).length === 2);
check("light DOM text kept", /go<\/a>/.test(html));
check("doctype preserved", html.startsWith("<!DOCTYPE html>"));

// ---- sidecar: must line up with the serialised element order -------------
const parsed = new (require("jsdom").JSDOM)(html).window.document;
// template[shadowrootmode] is the declarative-shadow-DOM wrapper, not a page
// node: Chromium's parser consumes it, jsdom leaves it. Either way it is not
// something the mirror needs to address.
const real = [...parsed.querySelectorAll("*")].filter((e) => !e.hasAttribute("shadowrootmode"));
check("every surviving page element carries an id",
  parsed.querySelectorAll("[data-rb-nid]").length === real.length,
  parsed.querySelectorAll("[data-rb-nid]").length + " tagged vs " + real.length + " page elements");
check("ids are unique in the output",
  new Set([...parsed.querySelectorAll("[data-rb-nid]")].map((e) => e.getAttribute("data-rb-nid"))).size
    === parsed.querySelectorAll("[data-rb-nid]").length);
check("shadow content is tagged too",
  /shadowrootmode="open"[\s\S]*data-rb-nid/.test(html));

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
