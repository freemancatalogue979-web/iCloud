/* Drift and shadow-root discovery.
 *
 * Two behaviours that fail quietly:
 *   - a `text` op whose index no longer lines up must ask for a resync rather
 *     than replace a parent's children with a single string
 *   - a shadow root attached to an element must start being observed, even
 *     though attaching one is not a DOM mutation and nothing reports it
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const AGENT = fs.readFileSync(path.join(__dirname, "..", "agent.js"), "utf8");
const VIEW = fs.readFileSync(path.join(__dirname, "..", "view.html"), "utf8").split("<script>")[1].split("</script>")[0];

let passed = 0, failed = 0;
const check = (n, c, d) => { if (c) { passed++; console.log("ok   " + n); } else { failed++; console.log("FAIL " + n + (d ? "\n     " + d : "")); } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  // ---------------------------------------------------------- text drift
  const src = new JSDOM(
    '<!DOCTYPE html><html><body><p id="p">alpha<br id="br">omega</p></body></html>',
    { runScripts: "outside-only", url: "https://www.icloud.com/" }
  );
  const sent = [];
  src.window.__rbEmit = (m) => sent.push(m);
  src.window.eval(AGENT);
  await sleep(300);

  const frame = sent.find((m) => m.kind === "doc").frames[0];
  const mir = new JSDOM(frame.html, { runScripts: "outside-only", url: "http://localhost:9002/view" });
  mir.window.WebSocket = function () { this.readyState = 1; this.send = () => {}; };
  mir.window.eval(VIEW);
  const ids = new Map();
  mir.window.adopt(mir.window.document, ids);
  const entry = { doc: mir.window.document, ids };

  const pid = src.window.__rbInternals.ensureId(src.window.document.getElementById("p"));

  // A correct text op still works.
  let ok = mir.window.applyOps(entry, [{ op: "text", parent: pid, index: 0, text: "beta" }]);
  check("a well-formed text op applies", ok);
  check("the text node was updated",
    mir.window.document.getElementById("p").childNodes[0].data === "beta",
    mir.window.document.getElementById("p").textContent);
  check("its siblings survived",
    mir.window.document.getElementById("p").childNodes.length === 3,
    String(mir.window.document.getElementById("p").childNodes.length));

  // The same op with an index that lands on an element instead of text.
  ok = mir.window.applyOps(entry, [{ op: "text", parent: pid, index: 1, text: "wiped" }]);
  check("a mis-indexed text op reports failure", ok === false,
    "returned " + ok + " - no resync would be requested");
  check("a mis-indexed text op does NOT wipe the parent",
    mir.window.document.getElementById("p").childNodes.length === 3,
    "children now: " + mir.window.document.getElementById("p").innerHTML);
  check("the parent's text is intact",
    mir.window.document.getElementById("p").textContent === "betaomega",
    mir.window.document.getElementById("p").textContent);

  // An index past the end, which is what a stale patch looks like.
  ok = mir.window.applyOps(entry, [{ op: "text", parent: pid, index: 99, text: "wiped" }]);
  check("an out-of-range text op reports failure", ok === false);
  check("an out-of-range text op does not wipe the parent",
    mir.window.document.getElementById("p").childNodes.length === 3);

  // ------------------------------------------------- shadow root discovery
  const src2 = new JSDOM(
    '<!DOCTYPE html><html><body><div id="slot"></div></body></html>',
    { runScripts: "outside-only", url: "https://www.icloud.com/" }
  );
  const sent2 = [];
  src2.window.__rbEmit = (m) => sent2.push(m);
  src2.window.eval(AGENT);
  await sleep(300);

  // Build a web component and drop it in. Its shadow root is attached before
  // insertion, so serialising the added subtree is what has to find it.
  const host = src2.window.document.createElement("ui-thing");
  const shadow = host.attachShadow({ mode: "open" });
  const inner = src2.window.document.createElement("span");
  inner.textContent = "inside";
  shadow.appendChild(inner);

  sent2.length = 0;
  src2.window.document.getElementById("slot").appendChild(host);
  await sleep(250);

  const addOps = sent2.filter((m) => m.kind === "mut").flatMap((m) => m.ops || []);
  check("the web component was emitted", addOps.some((o) => o.op === "add"),
    JSON.stringify(addOps).slice(0, 200));
  check("its shadow content is in the payload",
    JSON.stringify(addOps).includes("inside"), JSON.stringify(addOps).slice(0, 200));

  // Now mutate inside the shadow root. Nothing reports the attachment itself,
  // so this only arrives if serialising the subtree started the observer.
  sent2.length = 0;
  inner.textContent = "changed";
  await sleep(2550); // past the full-sweep backstop as well
  const shadowOps = sent2.filter((m) => m.kind === "mut").flatMap((m) => m.ops || []);
  check("mutations inside the shadow root are observed",
    shadowOps.length > 0 && JSON.stringify(shadowOps).includes("changed"),
    "ops: " + JSON.stringify(shadowOps).slice(0, 200));

  // ------------------------------------------- serialising before start()
  // A child frame can be asked to capture itself before its observer exists.
  const src3 = new JSDOM("<!DOCTYPE html><html><body><div id=h></div></body></html>",
    { runScripts: "outside-only", url: "https://www.icloud.com/" });
  Object.defineProperty(src3.window.document, "readyState", {
    configurable: true,
    get: () => "loading",
  });
  src3.window.__rbEmit = () => {};
  const h = src3.window.document.getElementById("h");
  const s3 = h.attachShadow({ mode: "open" });
  s3.appendChild(src3.window.document.createElement("b"));
  src3.window.eval(AGENT);

  let threw = null;
  let out = null;
  try {
    out = src3.window.__rbInternals.serialiseDocumentOf(src3.window.document, new Map());
  } catch (e) {
    threw = e;
  }
  check("serialising before start() does not throw", threw === null, threw && threw.message);
  check("it still produces the shadow template",
    out && /shadowrootmode="open"/.test(out.html), out && out.html.slice(0, 200));

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
