/* The capture scheduler.
 *
 * A change to the page is answered with a fresh capture of the whole page,
 * not with a patch. So the thing worth pinning is not "an op was produced"
 * but "a change produced a new capture, the new capture has the change in
 * it, and the scheduler re-arms for the next change".
 *
 * The strict requestAnimationFrame stub is kept here from the old suite:
 * Chromium does NOT throw "Illegal invocation" for an unbound call (measured
 * against real Chromium 153.0.8010.0), so it encodes a rule stricter than
 * any browser enforces and is not evidence of a bug that was fixed. It is
 * harmless, and it keeps the agent's scheduling honest about its receiver.
 */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const AGENT = fs.readFileSync(require("path").join(__dirname, "..", "agent.js"), "utf8");

let passed = 0, failed = 0;
const check = (n, c, d) => { if (c) { passed++; console.log("ok   " + n); } else { failed++; console.log("FAIL " + n + (d ? "\n     " + d : "")); } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const dom = new JSDOM(
    "<!DOCTYPE html><html><body><div id=d><span id=s>one</span></div></body></html>",
    { runScripts: "outside-only", url: "https://www.icloud.com/", pretendToBeVisual: true }
  );
  const win = dom.window;

  const realRaf = win.requestAnimationFrame.bind(win);
  let unboundCalls = 0;
  Object.defineProperty(win, "requestAnimationFrame", {
    configurable: true,
    value: function (cb) {
      if (this !== win) {
        unboundCalls++;
        throw new TypeError(
          "Failed to execute 'requestAnimationFrame' on 'Window': Illegal invocation"
        );
      }
      return realRaf(cb);
    },
  });

  const sent = [];
  win.__rbEmit = (m) => sent.push(m);
  win.eval(AGENT);
  await sleep(300);

  check("initial capture still runs under strict rAF",
    sent.some((m) => m.kind === "doc"));

  sent.length = 0;
  win.document.getElementById("s").textContent = "two";
  await sleep(1200);

  check("requestAnimationFrame is never called unbound", unboundCalls === 0,
    unboundCalls + " unbound call(s) - Chromium would throw Illegal invocation");

  const docs = sent.filter((m) => m.kind === "doc");
  check("a change produces a new capture", docs.length > 0,
    "no doc message - the update path is dead");
  const html = docs.length ? (docs[docs.length - 1].frames || [])[0].html : "";
  check("the new capture has the change in it",
    html.includes("two"), html.slice(0, 200));

  // A capture that comes out the same is not sent again: the observer can
  // fire for a change that does not show up in the markup, and sending the
  // page again for nothing would make the mirror flicker.
  sent.length = 0;
  win.document.getElementById("s").setAttribute("data-x", "1");
  await sleep(1200);
  const again = sent.filter((m) => m.kind === "doc");
  check("a change that shows up in the markup produces a capture",
    again.length > 0, "no doc message");

  // A change that does not show up in the markup. The capture of a script's
  // text is the same as the capture before it - scripts are not mirrored -
  // so the capture is not sent. That must not silence the agent: the next
  // change that does show up still has to be sent.
  const script = win.document.createElement("script");
  script.textContent = "var a = 1;";
  win.document.body.appendChild(script);
  sent.length = 0;
  script.textContent = "var a = 2;";
  await sleep(1200);
  const unchanged = sent.filter((m) => m.kind === "doc");
  check("a change that is not in the markup does not send the page again",
    unchanged.length === 0, unchanged.length + " captures sent");

  sent.length = 0;
  win.document.getElementById("s").textContent = "three";
  await sleep(1200);
  const third = sent.filter((m) => m.kind === "doc");
  check("the agent still speaks after an unchanged capture", third.length > 0,
    "the agent went quiet - only the first change after load was delivered");
  const thirdHtml = third.length ? (third[third.length - 1].frames || [])[0].html : "";
  check("the later capture has the later change in it",
    thirdHtml.includes("three"), thirdHtml.slice(0, 200));

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
