/* Differential test: capture in one jsdom, rebuild in a second jsdom, and check
 * the mirror converges on the source as mutations stream in.
 *
 * The applier is not re-implemented here - it is lifted out of the shipped
 * view.html and eval'd, so what runs is what ships. */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const AGENT = fs.readFileSync("/home/user/iCloud/agent.js", "utf8");
const VIEW = fs.readFileSync("/home/user/iCloud/view.html", "utf8");
const VIEW_SCRIPT = VIEW.split("<script>")[1].split("</script>")[0];

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

/* The real view.html applier, lifted into a jsdom so it can be called. */
function makeMirror(html) {
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:9002/view" });
  const win = dom.window;
  win.WebSocket = function () { this.readyState = 1; this.send = () => {}; };
  win.eval(VIEW_SCRIPT);
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

  // ---- ids must survive the trip and resolve to the right nodes ----------
  const mir = makeMirror(frame.html);
  const ids = new Map();
  mir.win.adopt(mir.doc, ids);
  check("every serialised element got adopted", ids.size > 0, ids.size + " adopted");
  check("ids are unique", new Set([...ids.keys()]).size === ids.size);

  // Spot-check that identity lines up: the id the source gave #wrap must point
  // at the mirror's #wrap.
  const I = swin.__rbInternals;
  const wrapId = I.ensureId(swin.document.getElementById("wrap"));
  check("id maps to the same element across the wire",
    ids.get(wrapId) === mir.doc.getElementById("wrap"),
    "wrapId=" + wrapId + " -> " + (ids.get(wrapId) && ids.get(wrapId).id));
  const fieldId = I.ensureId(swin.document.getElementById("field"));
  check("input identity survives too", ids.get(fieldId) === mir.doc.getElementById("field"));

  const entry = { doc: mir.doc, ids };

  // ---- stream real mutations through the real applier -------------------
  async function mutate(fn, label) {
    sent.length = 0;
    fn();
    await sleep(150);
    const muts = sent.filter((m) => m.kind === "mut");
    const ops = muts.flatMap((m) => m.ops || []);
    const ok = muts.length ? mir.win.applyOps(entry, ops) : false;
    return { ops, ok, muts };
  }

  // 1. text edit
  let r = await mutate(() => { swin.document.getElementById("s").textContent = "two"; }, "text");
  check("text edit emitted ops", r.ops.length > 0, JSON.stringify(r.ops));
  check("text edit applied", mir.doc.getElementById("s").textContent === "two",
    mir.doc.getElementById("s").textContent);
  check("text edit did not disturb its sibling",
    mir.doc.querySelector("#wrap em").textContent === "tail",
    mir.doc.querySelector("#wrap em").textContent);

  // 2. attribute
  r = await mutate(() => { swin.document.getElementById("wrap").setAttribute("class", "on"); });
  check("attribute applied", mir.doc.getElementById("wrap").getAttribute("class") === "on");

  // 3. inserted node keeps existing siblings in place
  r = await mutate(() => {
    const li = swin.document.createElement("li");
    li.textContent = "c";
    swin.document.getElementById("list").appendChild(li);
  });
  check("insert emitted an add op", r.ops.some((o) => o.op === "add"), JSON.stringify(r.ops));
  check("insert applied",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(",") === "a,b,c",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(","));

  // 4. insert at the FRONT - the case index paths get wrong
  r = await mutate(() => {
    const list = swin.document.getElementById("list");
    const li = swin.document.createElement("li");
    li.textContent = "zero";
    list.insertBefore(li, list.firstChild);
  });
  check("front insert applied in the right position",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(",") === "zero,a,b,c",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(","));

  // 5. removal
  r = await mutate(() => {
    const list = swin.document.getElementById("list");
    list.removeChild(list.querySelector("li"));
  });
  check("removal applied",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(",") === "a,b,c",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).join(","));

  // 6. typing - not a DOM mutation, needs the value side channel
  r = await mutate(() => {
    const f = swin.document.getElementById("field");
    f.value = "me@icloud.com";
    f.dispatchEvent(new swin.Event("input", { bubbles: true }));
  });
  check("typing emitted a value op", r.ops.some((o) => o.op === "value"), JSON.stringify(r.ops));
  check("typing applied to the mirror",
    mir.doc.getElementById("field").value === "me@icloud.com",
    mir.doc.getElementById("field").value);

  // 7. a burst stays granular rather than becoming one huge subtree replace
  sent.length = 0;
  const list = swin.document.getElementById("list");
  for (let i = 0; i < 40; i++) {
    const li = swin.document.createElement("li");
    li.textContent = "b" + i;
    list.appendChild(li);
  }
  await sleep(250);
  const burstOps = sent.filter((m) => m.kind === "mut").flatMap((m) => m.ops || []);
  const totalHtml = burstOps.reduce((n, o) => n + (o.html ? o.html.length : 0), 0);
  check("burst produced add ops, not subtree replacements",
    burstOps.length > 0 && burstOps.every((o) => o.op === "add"),
    burstOps.map((o) => o.op).join(","));
  // Per node, not in total: granular ops scale with what changed, whereas
  // subtree replacement would scale with the whole list and grow without bound.
  const perNode = totalHtml / 40;
  check("payload scales per node, not per subtree",
    perNode < 600, perNode.toFixed(0) + " bytes/node, " + totalHtml + " total");
  mir.win.applyOps(entry, burstOps);
  check("all 40 burst nodes arrived in order",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).slice(-1)[0] === "b39",
    [...mir.doc.querySelectorAll("#list li")].map((n) => n.textContent).slice(-3).join(","));

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
