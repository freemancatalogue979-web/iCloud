/* Exercises client.html - the launcher - in jsdom.
 *
 * The launcher is what decides when the app becomes the capture: it polls
 * /status, follows the step the server reports, and replaces itself with the
 * captured page once Sign In has been clicked. jsdom cannot navigate, so a
 * navigation attempt shows up as a jsdomError; that is the signal used here.
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
        if (String(url) === "/start") {
          return Promise.resolve({ json: () => Promise.resolve({ status: "idle" }) });
        }
        // Stay on the last state: a launcher that keeps polling after it has
        // navigated, or after a failure, is a launcher that never settles.
        const body = statuses[Math.min(polls, statuses.length - 1)];
        polls++;
        return Promise.resolve({ json: () => Promise.resolve(body) });
      };
    },
  });

  return {
    dom,
    errors,
    polls: () => polls,
    id: (name) => dom.window.document.getElementById(name),
  };
}

const navigations = (errors) => errors.filter((message) => /navigation/i.test(message));

(async () => {
  // ---- the happy path ---------------------------------------------------
  const running = launch([
    { status: "running", step: "launching bundled chromium", browser_open: false, page: "/src/1.html" },
    { status: "running", step: "clicking Sign In", browser_open: true, page: "/src/2.html" },
    { status: "done", step: "done", browser_open: true, page: "/src/2.html" },
  ]);

  await sleep(120);
  check("the launcher shows what the server is doing",
    running.id("caption").textContent === "launching bundled chromium",
    running.id("caption").textContent);
  check("a spinner shows while the browser is opening",
    running.id("spinner").hidden === false && running.id("icon").hidden === true);

  await sleep(420);
  check("the mark replaces the spinner once the browser is open",
    running.id("spinner").hidden === true && running.id("icon").hidden === false,
    [running.id("spinner").hidden, running.id("icon").hidden].join(" / "));
  check("the launcher knows which page the server is on",
    running.id("open").getAttribute("href") === "/src/2.html",
    running.id("open").getAttribute("href"));

  await sleep(460);
  check("once Sign In has been clicked the launcher navigates to the capture",
    navigations(running.errors).length === 1, running.errors);

  const pollsWhenDone = running.polls();
  await sleep(900);
  check("it does not keep polling once it has gone",
    running.polls() === pollsWhenDone,
    pollsWhenDone + " -> " + running.polls());

  // ---- the browser will not open ---------------------------------------
  const broken = launch([
    { status: "error", step: "FAILED", error: "no display", browser_open: false, page: "/src/1.html" },
  ]);

  await sleep(200);
  check("a failure is reported instead of spinning forever",
    /no display/.test(broken.id("caption").textContent),
    broken.id("caption").textContent);
  check("the failed mark is shown",
    broken.id("icon").hidden === false &&
      broken.id("icon").classList.contains("failed"));

  await sleep(700);
  check("a failed launch still offers the capture",
    broken.id("fallback").hidden === false &&
      broken.id("open").getAttribute("href") === "/src/1.html");
  check("a failed launch does not navigate", navigations(broken.errors).length === 0,
    broken.errors);
  check("a failed launch stops polling", broken.polls() === 1, broken.polls());

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
