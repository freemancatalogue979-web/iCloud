/* Exercises the frame-tree protocol end to end: a parent document and a child
 * iframe, both running agent.js, talking over postMessage. This is the piece
 * that cannot be checked by serialising one document. */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const AGENT = fs.readFileSync("/home/user/iCloud/agent.js", "utf8");

const CHILD = `<!DOCTYPE html><html><head><base href="https://idmsa.apple.com/"></head>
<body><form><input id="account_name_text_field"><button>Continue</button></form></body></html>`;

// jsdom never parses srcdoc content, so the child document is written into the
// iframe directly once it has a window.
const PARENT = `<!DOCTYPE html><html><head><title>iCloud</title></head>
<body><div id="top">parent</div><iframe id="auth"></iframe></body></html>`;

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log("ok   " + name); }
  else { failed++; console.log("FAIL " + name + (detail ? "\n     " + detail : "")); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const dom = new JSDOM(PARENT, { runScripts: "outside-only", url: "https://www.icloud.com/" });
  const win = dom.window;
  const sent = [];
  win.__rbEmit = (m) => sent.push(m);

  // Wait for the iframe to have a window of its own, then give it a document.
  let child = null;
  for (let i = 0; i < 40 && !child; i++) {
    const el = win.document.getElementById("auth");
    if (el && el.contentWindow) child = el.contentWindow;
    else await sleep(50);
  }
  check("child frame has a window", !!child);
  if (!child) { console.log(`\n${passed} passed, ${failed} failed`); process.exit(1); }

  child.document.open();
  child.document.write(CHILD);
  child.document.close();
  check("child fixture has its own content",
    !!child.document.getElementById("account_name_text_field"),
    child.document.documentElement.outerHTML);

  // expose_binding is per-context in Playwright, so every frame shares it.
  child.__rbEmit = (m) => sent.push(m);
  win.eval(AGENT);
  child.eval(AGENT);

  // The parent's first capture may have run before the child was listening, so
  // give the request timeout time to expire, then ask for a fresh one.
  await sleep(2000);
  sent.length = 0;
  win.__rbResync();
  await sleep(2000);

  const docs = sent.filter((m) => m.kind === "doc");
  check("resync produced a document", docs.length > 0,
    "kinds: " + JSON.stringify(sent.map((m) => m.kind)));

  const frames = docs.length ? docs[0].frames : [];
  const ids = frames.map((f) => f.windowId);
  check("two frames captured", frames.length === 2, JSON.stringify(ids));
  check("parent is windowId 0", ids.includes("0"), JSON.stringify(ids));
  check("child is windowId 0.0", ids.includes("0.0"), JSON.stringify(ids));

  const parentHtml = (frames.find((f) => f.windowId === "0") || {}).html || "";
  const childHtml = (frames.find((f) => f.windowId === "0.0") || {}).html || "";

  check("child content came back over postMessage",
    /account_name_text_field/.test(childHtml), childHtml.slice(0, 200));
  check("parent carries the frame element tagged with the child id",
    /data-rb-frame="0\.0"/.test(parentHtml), parentHtml.slice(0, 300));
  check("parent kept its own content", /parent<\/div>/.test(parentHtml));
  check("child <base> was stripped", !/<base/i.test(childHtml));
  const childFrame = frames.find((x) => x.windowId === "0.0");
  check("child frame content carries node ids", /data-rb-nid="/.test(childFrame.html),
    childFrame.html.slice(0, 160));

  // The mirror resolves a child frame by walking the tag in the parent's HTML,
  // so the id written there must match the id the child's content came under.
  const tagMatch = parentHtml.match(/data-rb-frame="([^"]+)"/);
  check("tagged id matches the frame it delivered",
    tagMatch && ids.includes(tagMatch[1]),
    "tag=" + (tagMatch && tagMatch[1]) + " ids=" + JSON.stringify(ids));

  // ---- mutations inside the child frame --------------------------------
  sent.length = 0;
  const input = child.document.getElementById("account_name_text_field");
  input.setAttribute("value", "someone@icloud.com");
  await sleep(300);

  const muts = sent.filter((m) => m.kind === "mut");
  check("child frame mutation was emitted", muts.length > 0,
    "kinds: " + JSON.stringify(sent.map((m) => m.kind)));
  check("child patch carries the child windowId",
    muts.every((m) => m.windowId === "0.0"),
    JSON.stringify(muts.map((m) => m.windowId)));
  check("child patch carries the value",
    JSON.stringify(muts).includes("someone@icloud.com"), JSON.stringify(muts));

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
