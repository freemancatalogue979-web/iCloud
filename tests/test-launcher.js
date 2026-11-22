/* Exercises client.html - the app page - in jsdom.
 *
 * The page is deliberately silent: a black screen with the mark on it, and then
 * the capture in a frame. No text, no buttons, no "opening this / doing that".
 * The one thing it decides is when to hand over: not on a timer, but when the
 * server says the sign-in form is up in the browser.
 *
 * jsdom does not load frames, so the frame's document is the empty one jsdom
 * gives it; what is checked here is that the bridge is attached to it and that
 * what the bridge posts goes to /map.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const CLIENT = fs.readFileSync(path.join(__dirname, "..", "client.html"), "utf8");

let passed = 0;
let failed = 0;

function check(name, cond, detail) {
  if (cond) {
    passed++;
    console.log("ok   " + name);
  } else {
    failed++;
    console.log("FAIL " + name + (detail ? "\n     " + detail : ""));
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function launch(statuses) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (event) =>
    errors.push(String((event && event.message) || event))
  );

  const calls = [];      // every fetch the page makes
  const attached = [];   // (document, post) pairs handed to the bridge
  let polls = 0;

  const dom = new JSDOM(CLIENT, {
    url: "http://localhost:9002/",
    runScripts: "dangerously",
    virtualConsole,
    beforeParse(window) {
      // /map.js is fetched by the browser, not by jsdom: the bridge is stubbed
      // here, and exercised for real in test-mapping.js.
      window.attachMapping = (doc, post) => {
        attached.push({ doc, post });
        return () => {};
      };

      window.fetch = (url, options) => {
        calls.push({ url: String(url), options });

        if (String(url) === "/start") {
          return Promise.resolve({ json: () => Promise.resolve({ status: "running" }) });
        }
        if (String(url) === "/map") {
          return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
        }

        // Stay on the last state: a page that keeps polling after it has
        // handed over is a page that never settles.
        const body = statuses[Math.min(polls, statuses.length - 1)];
        polls++;
        return Promise.resolve({ json: () => Promise.resolve(body) });
      };
    },
  });

  return { dom, errors, calls, attached, polls: () => polls };
}

// The inline <script> is part of body.textContent; only what a person could
// see counts.
function visibleText(doc) {
  const copy = doc.body.cloneNode(true);
  copy.querySelectorAll("script, style, iframe").forEach((el) => el.remove());
  return copy.textContent.trim();
}

(async () => {
  const running = { status: "running", browser_open: false, page: "/src/1.html" };
  const done = { status: "done", browser_open: true, form_up: true, page: "/src/1.html" };

  // ---- the wait is only the mark ---------------------------------------
  const app = launch([running, running, done]);
  await sleep(120);

  const win = app.dom.window;
  const doc = win.document;
  const frame = doc.getElementById("capture");

  check("the screen is only the mark - no text, no buttons",
    visibleText(doc) === "" &&
      doc.querySelectorAll("a, button, input, p, span, h1, img").length === 0 &&
      !!doc.querySelector(".mark svg"),
    JSON.stringify(visibleText(doc)));
  check("the page carries the standard web-app meta",
    !!doc.querySelector('meta[name="mobile-web-app-capable"]'));
  check("the capture is in a frame of its own",
    !!frame && frame.tagName === "IFRAME" && frame.hidden, frame && frame.tagName);
  check("the frame is not loaded until the form is up",
    !frame.getAttribute("src"), frame.getAttribute("src"));
  check("nothing is shown while the browser is opening",
    !doc.body.classList.contains("show") && app.attached.length === 0);
  check("visiting the page opens the browser and presses Sign In",
    app.calls.some((call) => call.url === "/start"), JSON.stringify(app.calls.map((c) => c.url)));
  check("the page itself never navigates away",
    app.errors.filter((message) => /navigation/i.test(message)).length === 0, app.errors);

  // ---- the form is up: the capture takes over --------------------------
  await sleep(700);

  check("the capture is loaded once the sign-in form is up",
    frame.getAttribute("src") === "/src/1.html", frame.getAttribute("src"));

  // The browser's load event is what shows it; jsdom does not fire one.
  frame.dispatchEvent(new win.Event("load"));
  await sleep(80);

  check("the frame is shown as soon as it has the capture",
    doc.body.classList.contains("show") && !frame.hidden, doc.body.className);

  // The mark stays behind the fade of the frame, and is taken out of the flow
  // once that fade is over.
  await sleep(300);
  check("the mark steps aside once the capture is up",
    doc.body.classList.contains("done"), doc.body.className);
  check("the bridge is attached to the frame's document",
    app.attached.length === 1 && app.attached[0].doc === frame.contentDocument,
    app.attached.length);

  const pollsWhenShown = app.polls();
  await sleep(400);
  check("polling stops once the capture is up",
    app.polls() === pollsWhenShown, pollsWhenShown + " -> " + app.polls());

  // What the bridge posts is what the server receives.
  app.calls.length = 0;
  app.attached[0].post({ field: "account_name_text_field", value: "someone@example.com" });
  await sleep(50);
  const mapped = app.calls.find((call) => call.url === "/map");
  check("a change in the frame is posted to /map",
    !!mapped && mapped.options.method === "POST" &&
      JSON.parse(mapped.options.body).field === "account_name_text_field" &&
      JSON.parse(mapped.options.body).value === "someone@example.com",
    mapped && mapped.options.body);

  // ---- a browser that will not open ------------------------------------
  const broken = launch([
    { status: "error", error: "no display", browser_open: false, page: "/src/1.html" },
  ]);
  await sleep(150);
  broken.dom.window.document
    .getElementById("capture")
    .dispatchEvent(new broken.dom.window.Event("load"));
  await sleep(80);
  check("a browser that will not open still shows the capture",
    broken.dom.window.document.body.classList.contains("show"),
    broken.dom.window.document.body.className);
  check("and still shows nothing of its own on the way",
    visibleText(broken.dom.window.document) === "",
    JSON.stringify(visibleText(broken.dom.window.document)));
  check("a failed launch stops polling",
    broken.polls() === 1, broken.polls());

  // ---- the frame never reports back ------------------------------------
  const quiet = launch([done]);
  await sleep(1500);
  check("a frame that never reports back is shown anyway",
    quiet.dom.window.document.body.classList.contains("show"),
    quiet.dom.window.document.body.className);

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
