/* The order of input.
 *
 * A keystroke is one character, and each one is sent to the real browser as
 * its own request. The user types "hello", the browser is told "h", then
 * "e", then "l", then "l", then "o" - and the field has to end up holding
 * "hello".
 *
 * It does not, if the requests are in flight at the same time. Two
 * requests that overlap are in the browser in whatever order the network
 * happens to deliver them, so the field ends up holding "hlelo" or
 * "hleol", and the user is typing into a mirror that scrambles what they
 * type. The same goes for a click and a keystroke: a click that arrives
 * after the character it was supposed to precede puts the character into
 * the old field.
 *
 * So the mirror sends one input request at a time, in the order the user
 * made them. This suite pins that down: the requests must not overlap,
 * and they must carry the characters in order.
 */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const MIRROR = fs.readFileSync(
  require("path").join(__dirname, "..", "mirror.js"), "utf8");

let passed = 0, failed = 0;
const check = (n, c, d) => {
  if (c) { passed++; console.log("ok   " + n); }
  else { failed++; console.log("FAIL " + n + (d ? "\n     " + d : "")); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* A capture of a page with a field in it, the way the agent sends it. */
const CAPTURE = [{
  windowId: "0",
  html: '<!DOCTYPE html><html><body><input id="acct" type="text"></body></html>',
}];

(async () => {
  const mir = new JSDOM(
    "<!DOCTYPE html><html><head></head><body></body></html>",
    { runScripts: "outside-only", url: "http://localhost:9002/1.html" });
  const win = mir.window;

  /* Record every request the mirror makes, and how many of them were in
   * flight at the same time. A request takes a little while, as a real one
   * does, so overlapping requests have somewhere to overlap. */
  const calls = [];
  let inFlight = 0, maxInFlight = 0;
  win.fetch = (path, opts) => {
    inFlight++;
    if (inFlight > maxInFlight) maxInFlight = inFlight;
    let body = {};
    try { body = JSON.parse(opts && opts.body); } catch (e) { /* no body */ }
    calls.push({ path: String(path), body: body });
    return new Promise((resolve) => setTimeout(() => {
      inFlight--;
      resolve({ ok: true, status: 200 });
    }, 5));
  };
  win.WebSocket = function () { this.close = () => {}; };

  win.eval(MIRROR);
  win.seed(CAPTURE, { w: 800, h: 600 });
  await sleep(50);

  /* The user types, with no pause at all between the keys - the case that
   * used to scramble. */
  const typed = "hello";
  for (const ch of typed) {
    const ev = new win.KeyboardEvent("keydown", {
      key: ch, bubbles: true, cancelable: true,
    });
    win.document.dispatchEvent(ev);
  }

  /* Let the queue drain. */
  await sleep(400);

  const keys = calls.filter((c) => c.path.indexOf("/keys") !== -1);
  check("every character was sent", keys.length === typed.length,
    keys.length + " request(s) for " + typed.length + " character(s)");
  check("the characters were sent in the order they were typed",
    keys.map((c) => c.body.text).join("") === typed,
    JSON.stringify(keys.map((c) => c.body.text)));
  check("no two requests were in flight at the same time",
    maxInFlight === 1, "up to " + maxInFlight + " request(s) at once");

  /* A click is input too, and it has to take its place in the order with
   * the keys around it - a click that overtakes the character before it
   * puts that character into the field the user has just left. */
  calls.length = 0;
  const click = new win.MouseEvent("mousedown", {
    bubbles: true, cancelable: true, clientX: 10, clientY: 10,
  });
  win.document.dispatchEvent(click);
  await sleep(200);
  check("a click is sent as one request",
    calls.filter((c) => c.path.indexOf("/click") !== -1).length === 1,
    JSON.stringify(calls.map((c) => c.path)));

  /* And the queue keeps working after that: a stuck request must not
   * wedge the input for good. */
  calls.length = 0;
  for (const ch of "abc") {
    win.document.dispatchEvent(new win.KeyboardEvent("keydown", {
      key: ch, bubbles: true, cancelable: true,
    }));
  }
  await sleep(300);
  check("input still flows after a click",
    calls.filter((c) => c.path.indexOf("/keys") !== -1)
      .map((c) => c.body.text).join("") === "abc",
    JSON.stringify(calls.map((c) => c.path + ":" + c.body.text)));

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
