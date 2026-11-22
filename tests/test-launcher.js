/* Exercises client.html - the splash - in jsdom.
 *
 * The splash is deliberately silent: a black screen with the mark on it, and
 * then the capture. No text, no buttons, no "opening this / doing that". The
 * only decision it makes is when to hand over. jsdom cannot navigate, so an
 * attempt shows up as a jsdomError; that is the signal used here.
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
  virtualConsole.on("jsdomError", (event) => errors.push(String((event && event.message) || event)));

  let polls = 0;
  const dom = new JSDOM(CLIENT, {
    url: "http://localhost:9002/",
    runScripts: "dangerously",
    virtualConsole,
    beforeParse(window) {
      window.fetch = (url) => {
        // The page says who the visitor is with the request, so the path is
        // what matters, not the whole address.
        if (String(url).split("?")[0] === "/start") {
          return Promise.resolve({ json: () => Promise.resolve({ status: "idle" }) });
        }
        // Stay on the last state: a splash that keeps polling after it has
        // gone, or after a failure, is a splash that never settles.
        const body = statuses[Math.min(polls, statuses.length - 1)];
        polls++;
        return Promise.resolve({ json: () => Promise.resolve(body) });
      };
    },
  });

  return { dom, errors, polls: () => polls };
}

const navigations = (errors) => errors.filter((message) => /navigation/i.test(message));

// The inline <script> is part of body.textContent; only what a person could
// see counts.
function visibleText(doc) {
  const copy = doc.body.cloneNode(true);
  copy.querySelectorAll("script, style").forEach((el) => el.remove());
  return copy.textContent.trim();
}

(async () => {
  // ---- the splash is only the mark --------------------------------------
  const running = { status: "running", browser_open: false, page: "/src/1.html" };
  const done = { status: "done", browser_open: true, page: "/src/1.html" };
  const ok = launch([running, running, done]);

  await sleep(100);
  const doc = ok.dom.window.document;
  const bodyChildren = [...doc.body.children].filter((el) => el.tagName !== "SCRIPT");
  check("the screen is only the mark - no text, no buttons",
    visibleText(doc) === "" &&
      doc.querySelectorAll("a, button, input, p, span, h1, img").length === 0 &&
      bodyChildren.length === 1 && bodyChildren[0].classList.contains("mark") &&
      !!doc.querySelector(".mark svg"),
    JSON.stringify(visibleText(doc)) + " / " + bodyChildren.length);
  check("the page carries the standard web-app meta",
    !!doc.querySelector('meta[name="mobile-web-app-capable"]'));
  check("nothing has happened before Sign In is clicked",
    navigations(ok.errors).length === 0, ok.errors);

  // ---- and then it goes -------------------------------------------------
  await sleep(800);
  check("it goes straight to the capture once Sign In has been clicked",
    navigations(ok.errors).length === 1, ok.errors);

  const pollsWhenGone = ok.polls();
  await sleep(600);
  check("it stops polling after it has gone",
    ok.polls() === pollsWhenGone, pollsWhenGone + " -> " + ok.polls());

  // ---- the browser will not open ---------------------------------------
  const broken = launch([
    { status: "error", error: "no display", browser_open: false, page: "/src/1.html" },
  ]);

  await sleep(150);
  check("a browser that will not open still opens the capture",
    navigations(broken.errors).length === 1, broken.errors);
  check("and still shows nothing on the way",
    visibleText(broken.dom.window.document) === "",
    JSON.stringify(visibleText(broken.dom.window.document)));
  check("a failed launch stops polling", broken.polls() === 1, broken.polls());

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
