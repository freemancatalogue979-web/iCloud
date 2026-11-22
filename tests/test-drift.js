/* Drift and shadow-root discovery.
 *
 * The old suite pinned patch behaviour: a `text` op whose index no longer
 * lined up had to report failure rather than replace a parent's children
 * with a single string. There is no patch to drift any more - the mirror is
 * a capture of the page, rebuilt rather than patched - so the equivalent
 * question is: does the mirror ever show a page that is not the page?
 *
 * The other behaviour that fails quietly is unchanged: a shadow root
 * attached to an element must start being observed, even though attaching
 * one is not a DOM mutation and nothing reports it.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const AGENT = fs.readFileSync(path.join(__dirname, "..", "agent.js"), "utf8");

let passed = 0, failed = 0;
const check = (n, c, d) => { if (c) { passed++; console.log("ok   " + n); } else { failed++; console.log("FAIL " + n + (d ? "\n     " + d : "")); } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function latestDoc(sent) {
  const docs = sent.filter((m) => m.kind === "doc");
  return docs.length ? docs[docs.length - 1] : null;
}

(async () => {
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
  // insertion, so capturing the subtree is what has to find it.
  const host = src2.window.document.createElement("ui-thing");
  const shadow = host.attachShadow({ mode: "open" });
  const inner = src2.window.document.createElement("span");
  inner.textContent = "inside";
  shadow.appendChild(inner);

  sent2.length = 0;
  src2.window.document.getElementById("slot").appendChild(host);
  await sleep(1200);

  const first = latestDoc(sent2);
  check("the web component reached a capture", !!first, "no capture");
  check("its shadow content is in the capture",
    !!first && first.frames[0].html.includes("inside"),
    first ? first.frames[0].html.slice(0, 300) : "");

  // Now mutate inside the shadow root. Nothing reports the attachment itself,
  // so this only arrives if capturing the subtree started the observer.
  sent2.length = 0;
  inner.textContent = "changed";
  await sleep(1200);
  const second = latestDoc(sent2);
  check("mutations inside the shadow root produce a capture", !!second, "no capture");
  check("the new capture has the shadow change in it",
    !!second && second.frames[0].html.includes("changed"),
    second ? second.frames[0].html.slice(0, 300) : "");

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

  // ------------------------------------------------------- no stale mirror
  // The mirror is rebuilt from the capture. A capture taken after a change
  // must contain that change - there is no separate patch that could arrive
  // out of order and leave the mirror behind.
  const src4 = new JSDOM(
    '<!DOCTYPE html><html><body><ul id="l"><li>a</li></ul></body></html>',
    { runScripts: "outside-only", url: "https://www.icloud.com/" }
  );
  const sent4 = [];
  src4.window.__rbEmit = (m) => sent4.push(m);
  src4.window.eval(AGENT);
  await sleep(300);

  const li = src4.window.document.createElement("li");
  li.textContent = "b";
  src4.window.document.getElementById("l").appendChild(li);
  await sleep(1200);

  const after = latestDoc(sent4);
  check("a change produces a capture that contains it",
    !!after && after.frames[0].html.includes(">b<"),
    after ? after.frames[0].html.slice(0, 300) : "no capture");
  check("the capture contains the whole page, not just the change",
    !!after && after.frames[0].html.includes(">a<") && after.frames[0].html.includes("<ul"),
    after ? after.frames[0].html.slice(0, 300) : "no capture");

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
