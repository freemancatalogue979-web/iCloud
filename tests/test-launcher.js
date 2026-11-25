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

function launch(statuses, url, seed, answers) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (event) => errors.push(String((event && event.message) || event)));

  let polls = 0;
  const asked = [];
  const sent = [];
  const dom = new JSDOM(CLIENT, {
    url: url || "http://localhost:9002/",
    runScripts: "dangerously",
    virtualConsole,
    beforeParse(window) {
      // Some of the tests need to say where the copy is, as a browser that
      // has been here before would have it remembered.
      if (seed) {
        try {
          window.localStorage.setItem("icloud-root", seed);
        } catch (err) {
          /* no storage in this one */
        }
      }
      window.fetch = (url, options) => {
        asked.push(String(url));
        sent.push({ url: String(url), options: options || {} });
        // The page says who the visitor is with the request, so the path is
        // what matters, not the whole address.
        if (String(url).split("?")[0] === "/start") {
          return Promise.resolve({ json: () => Promise.resolve({ status: "idle" }) });
        }
        // The question asked at the door: the page asks the server what the
        // question is, when it loads.
        if (String(url).split("?")[0] === "/turnstile/sitekey") {
          const answer = (answers && answers.site_key) || {};
          return Promise.resolve({ json: () => Promise.resolve(answer) });
        }
        // Stay on the last state: a splash that keeps polling after it has
        // gone, or after a failure, is a splash that never settles.
        const body = statuses[Math.min(polls, statuses.length - 1)];
        polls++;
        return Promise.resolve({ json: () => Promise.resolve(body) });
      };
    },
  });

  return { dom, errors, polls: () => polls, asked: () => asked,
           sent: () => sent };
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

  // ---- and it says who the visitor is, every time ----------------------
  // A copy of this page that lives on another host has no cookie of the
  // server's to say who it is, so the name goes in the address of every
  // request - not only the first one.
  const asked = ok.asked();
  check("the browser is asked to open, with the visitor's name",
    asked.some((url) => url.indexOf("/start?visitor=") === 0), asked);
  check("and every request after that says who it is too",
    asked.filter((url) => url.indexOf("/status") === 0)
      .every((url) => /[?&]visitor=/.test(url)), asked);
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

  // ---- and the question asked before the browser opens ------------------
  // The page asks the server what the question is when it loads - the
  // site's key is public and the server is the one that has it - then asks
  // Cloudflare, and sends the answer with the request. A browser is not
  // asked to open until the answer is there - and an answer that is not
  // good stops it.
  function launchAsking(statuses, turnstile) {
    const errors = [];
    const virtualConsole = new VirtualConsole();
    virtualConsole.on("jsdomError", (event) =>
      errors.push(String((event && event.message) || event)));
    let polls = 0;
    const sent = [];
    const dom = new JSDOM(CLIENT, {
      url: "http://localhost:9002/",
      runScripts: "dangerously",
      virtualConsole,
      beforeParse(window) {
        window.turnstile = turnstile;
        window.fetch = (where, options) => {
          sent.push({ url: String(where), options: options || {} });
          if (String(where).split("?")[0] === "/start") {
            return Promise.resolve({
              json: () => Promise.resolve({ status: "idle" }) });
          }
          if (String(where).split("?")[0] === "/turnstile/sitekey") {
            return Promise.resolve({
              json: () => Promise.resolve({
                site_key: "0x4AAAA", asked: true }) });
          }
          const body = statuses[Math.min(polls, statuses.length - 1)];
          polls++;
          return Promise.resolve({
            json: () => Promise.resolve(body) });
        };
      },
    });
    return { dom, errors, sent: () => sent };
  }

  // Cloudflare that answers.
  let rendered = null;
  const good = launchAsking([running], {
    render: (where, options) => {
      rendered = { where, options };
      setTimeout(() => options.callback("a-good-token"), 10);
      return "widget-id";
    },
  });
  await sleep(300);

  check("the page asks the server what the question is",
    good.sent().some((one) => one.url.indexOf("/turnstile/sitekey") === 0),
    good.sent().map((one) => one.url));
  check("the widget is asked for, with the site's key",
    rendered && rendered.options.sitekey === "0x4AAAA", rendered);
  check("...and with the name of the question, which comes back with the answer",
    rendered && rendered.options.action === "start", rendered);
  check("...and there is a way to say it could not answer",
    rendered && rendered.options["error-callback"], rendered);

  const starts = good.sent().filter((one) => one.url.indexOf("/start") === 0);
  check("the browser is asked to open only once the answer is here",
    starts.length === 1, good.sent());
  check("...and the answer travels with the request",
    starts.length === 1 &&
      starts[0].options.headers &&
      starts[0].options.headers["X-Icloud-Challenge"] === "a-good-token",
    starts.length ? starts[0].options.headers : "no request");

  // Cloudflare that cannot answer: the server is told, and it opens
  // nothing - an answer that is not good stops the door.
  const unanswered = launchAsking([running], {
    render: (where, options) => {
      setTimeout(() => options["error-callback"](), 10);
      return "widget-id";
    },
  });
  await sleep(300);
  const brokenStarts = unanswered.sent()
    .filter((one) => one.url.indexOf("/start") === 0);
  check("an answer that could not be had is told to the server as well",
    brokenStarts.length === 1 &&
      !(brokenStarts[0].options.headers || {})["X-Icloud-Challenge"],
    brokenStarts.length ? brokenStarts[0].options.headers : "no request");

  // ---- and the copy's own folder ---------------------------------------
  // The server knows its pages by their own names - "/src/1.html" - and a
  // copy may live in a folder of its own. The folder is put back, so a
  // copy in "/app/" asks for "/app/src/1.html": a copy that asked for
  // "/src/1.html" would be asking for a page that is not there, and the
  // host would answer with the launcher again - for ever.
  const inFolder = launch([running], "http://localhost:9002/app/client.html");
  await sleep(200);
  const folder = inFolder.dom.window.__icloudLauncher;
  check("the capture is asked for in the copy's own folder",
    folder && new URL(folder.localPath("/src/1.html"),
      "http://localhost:9002/app/client.html").pathname === "/app/src/1.html",
    folder && folder.localPath("/src/1.html"));
  check("...and the folder is remembered",
    folder && folder.copyRoot() === "/app/",
    folder && folder.copyRoot());

  // ---- and a page that is on the page it is asked for does not go ------
  // A page that sends itself to itself goes for ever: the browser runs it
  // again, it asks again, and the visitor watches a page reload without
  // end. So the page this one is on is never asked for.
  const onIt = launch([done], "http://localhost:9002/src/1.html", "/");
  await sleep(400);
  check("a page that is on the page it is asked for does not move",
    navigations(onIt.errors).length === 0, onIt.errors);

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
