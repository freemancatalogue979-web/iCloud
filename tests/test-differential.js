/* Differential test: capture in one jsdom, show it in a second one, and
 * check the mirror converges on the source as the page changes.
 *
 * The mirror is not re-implemented here - the shipped mirror.js is eval'd,
 * so what runs is what ships.
 */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const AGENT = fs.readFileSync("/home/user/iCloud/agent.js", "utf8");
const MIRROR = fs.readFileSync("/home/user/iCloud/mirror.js", "utf8");

const PAGE = `<!DOCTYPE html><html><head><style>.a{color:red}</style></head><body>
<div id="wrap"><span id="s">one</span><em>tail</em></div>
<ul id="list"><li>a</li><li>b</li></ul>
<input id="field" type="text">
</body></html>`;

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log("ok   " + name); }
  else { failed++; console.log("FAIL " + name + (detail ? "\n     " + detail : "")); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* The real mirror engine, lifted into a jsdom so it can be driven. */
function makeMirror() {
  const dom = new JSDOM(
    "<!DOCTYPE html><html><head></head><body></body></html>",
    { runScripts: "outside-only", url: "http://localhost:9002/1.html" }
  );
  const win = dom.window;
  win.fetch = () => Promise.reject(new Error("no server in a unit test"));
  win.WebSocket = function () { this.close = () => {}; };
  win.eval(MIRROR);
  return { dom, win, doc: win.document };
}

(async () => {
  const src = new JSDOM(PAGE, { runScripts: "outside-only", url: "https://www.icloud.com/" });
  const swin = src.window;
  const sent = [];
  swin.__rbEmit = (m) => sent.push(m);
  swin.eval(AGENT);
  await sleep(300);

  const doc = sent.find((m) => m.kind === "doc");
  check("capture produced a document", !!doc);
  if (!doc) { console.log(`\n${passed} passed, ${failed} failed`); process.exit(1); }

  const frame = doc.frames[0];
  check("serialised HTML carries node ids", /data-rb-nid="/.test(frame.html));

  // ---- the mirror shows the capture -------------------------------------
  const mir = makeMirror();
  const shown = mir.win.seed(doc.frames, doc.viewport);
  check("the mirror accepts the capture", shown === true, String(shown));
  check("the mirror shows the page",
    mir.doc.getElementById("wrap") !== null, mir.doc.body.innerHTML.slice(0, 200));
  check("the ids survive the trip",
    mir.doc.querySelectorAll("[data-rb-nid]").length > 0,
    String(mir.doc.querySelectorAll("[data-rb-nid]").length));

  /* A change on the source produces a new capture, and the mirror shows
   * that capture. There is no patch to apply and nothing to address. */
  async function change(fn) {
    sent.length = 0;
    fn();
    await sleep(1200);
    const docs = sent.filter((m) => m.kind === "doc");
    if (!docs.length) return null;
    const capture = docs[docs.length - 1];
    mir.win.seed(capture.frames, capture.viewport);
    return capture;
  }

  // 1. text edit
  let r = await change(() => { swin.document.getElementById("s").textContent = "two"; });
  check("a text edit produces a new capture", !!r, "no new capture");
  check("the mirror shows the new text",
    mir.doc.getElementById("s").textContent === "two",
    mir.doc.getElementById("s").textContent);
  check("the text edit did not disturb its sibling",
    mir.doc.querySelector("#wrap em").textContent === "tail",
    mir.doc.querySelector("#wrap em").textContent);

  // 2. attribute
  r = await change(() => { swin.document.getElementById("wrap").setAttribute("class", "on"); });
  check("the mirror shows the new attribute",
    mir.doc.getElementById("wrap").getAttribute("class") === "on",
    mir.doc.getElementById("wrap").getAttribute("class"));

  // 3. inserted node keeps existing siblings in place
  r = await change(() => {
    const li = swin.document.createElement("li");
    li.textContent = "c";
    swin.document.getElementById("list").appendChild(li);
  });
  check("the mirror shows the inserted node",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(",") === "a,b,c",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(","));

  // 4. insert at the FRONT - the case index paths get wrong
  r = await change(() => {
    const list = swin.document.getElementById("list");
    const li = swin.document.createElement("li");
    li.textContent = "zero";
    list.insertBefore(li, list.firstChild);
  });
  check("the mirror shows the front insert in the right position",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(",") === "zero,a,b,c",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(","));

  // 5. removal
  r = await change(() => {
    const list = swin.document.getElementById("list");
    list.removeChild(list.querySelector("li"));
  });
  check("the mirror shows the removal",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(",") === "a,b,c",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(","));

  // 6. typing - not a DOM mutation, so it is not a capture. It is the one
  //    thing that stays a small message of its own.
  sent.length = 0;
  const field = swin.document.getElementById("field");
  field.value = "me@icloud.com";
  field.dispatchEvent(new swin.Event("input", { bubbles: true }));
  await sleep(200);
  const values = sent.filter((m) => m.kind === "value");
  check("typing emitted a value message", values.length > 0,
    JSON.stringify(sent.map((m) => m.kind)));
  mir.win.__rbMirror.applyValue(values[0]);
  check("typing reached the mirror",
    mir.doc.getElementById("field").value === "me@icloud.com",
    mir.doc.getElementById("field").value);

  // 7. a burst is one capture, not forty patches
  sent.length = 0;
  const list = swin.document.getElementById("list");
  for (let i = 0; i < 40; i++) {
    const li = swin.document.createElement("li");
    li.textContent = "b" + i;
    list.appendChild(li);
  }
  await sleep(1500);
  const burstDocs = sent.filter((m) => m.kind === "doc");
  check("a burst produces one capture, not one message per change",
    burstDocs.length <= 3, burstDocs.length + " captures for 40 changes");
  const burstHtml = burstDocs.length ? burstDocs[burstDocs.length - 1].frames[0].html : "";
  // The list held a, b, c when the burst started - three survivors - so the
  // capture must hold 43 items, not 41.
  check("the capture has all 40 of them",
    (burstHtml.match(/<li/g) || []).length === 43,
    (burstHtml.match(/<li/g) || []).length + " list items");
  mir.win.seed(burstDocs[burstDocs.length - 1].frames,
    burstDocs[burstDocs.length - 1].viewport);
  check("all 40 burst nodes arrived in order",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).slice(-1)[0] === "b39",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).slice(-3).join(","));

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
