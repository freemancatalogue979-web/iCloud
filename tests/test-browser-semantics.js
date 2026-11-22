/* Strict-receiver discipline for requestAnimationFrame.
 *
 * CORRECTION: this suite was written on the belief that Chromium throws
 * "Illegal invocation" when requestAnimationFrame is called with no receiver.
 * Measured against real Chromium 153.0.8010.0, it does not - the unbound call
 * is accepted. So the strict stub below encodes a rule stricter than any
 * browser actually enforces, not a real divergence.
 *
 * It is kept because calling a Window method as a method of Window is correct
 * regardless, and because the suite also verifies the scheduler re-arms for
 * later mutations, which is a real behaviour worth pinning. It is not evidence
 * of a bug that was fixed.
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

  // Chromium throws when a Window method is called without the Window as its
  // receiver. jsdom does not care, which is how this class of bug ships.
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
  await sleep(300);

  check("requestAnimationFrame is never called unbound", unboundCalls === 0,
    unboundCalls + " unbound call(s) - Chromium would throw Illegal invocation");

  const ops = sent.filter((m) => m.kind === "mut").flatMap((m) => m.ops || []);
  check("mutations still stream under strict rAF", ops.length > 0,
    "no mut messages - the update stream is dead");
  check("the edit actually arrived",
    JSON.stringify(ops).includes("two"), JSON.stringify(ops).slice(0, 200));

  // A second burst, to be sure the scheduler re-arms rather than working once.
  sent.length = 0;
  win.document.getElementById("s").textContent = "three";
  await sleep(300);
  check("scheduler re-arms for later mutations",
    sent.some((m) => m.kind === "mut"),
    "only the first mutation after load was delivered");

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
