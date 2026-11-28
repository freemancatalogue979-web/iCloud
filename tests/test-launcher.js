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

/* A line to the server, in place of the real one.
 *
 * It says when it has opened, and it holds what the server says, so that
 * a test can tell the launcher something.
 */
function fakeLine() {
  const made = [];
  function Fake(url) {
    this.url = String(url);
    this.readyState = 0;
    this.sent = [];
    // What the page says over the line is kept, so that a test can look
    // at it.
    this.send = function (message) { this.sent.push(String(message)); };
    made.push(this);
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen({});
    }, 10);
  }
  Fake.made = made;
  Fake.send = function (message) {
    made.forEach((one) => { one.sent.push(String(message)); });
  };
  Fake.tell = function (message) {
    made.forEach((one) => {
      if (one.onmessage) one.onmessage({ data: JSON.stringify(message) });
    });
  };
  return Fake;
}

function launch(statuses, url, seed, answers, socket, visited, options) {
  const opt = options || {};
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (event) => errors.push(String((event && event.message) || event)));

  let polls = 0;
  const asked = [];
  const sent = [];
  // A copy of the pages has the server's address in it. The launcher reads
  // it from a meta tag, so the tests that are about a copy need one.
  const page = opt.server
    ? CLIENT.replace("<head>",
        '<head><meta name="icloud-server" content="' + opt.server + '">')
    : CLIENT;
  const dom = new JSDOM(page, {
    url: url || "http://localhost:9002/",
    runScripts: "dangerously",
    virtualConsole,
    beforeParse(window) {
      if (socket) window.WebSocket = socket;
      // The places a previous visit tried: a page that has been there
      // before is not tried again.
      if (visited) {
        try {
          window.sessionStorage.setItem("icloud-visited",
                                        JSON.stringify(visited));
        } catch (err) {
          /* no storage in this one */
        }
      }
      // The capture's places that have been tried. These are kept for the
      // life of the tab: there is no clock on them.
      if (opt.triedPlaces) {
        try {
          window.sessionStorage.setItem("icloud-tried",
                                        JSON.stringify(opt.triedPlaces));
        } catch (err) {
          /* no storage in this one */
        }
      }
      // The copy is not serving the captures, and the server's own copy
      // has already been tried.
      if (opt.gaveUp) {
        try {
          window.sessionStorage.setItem("icloud-gave-up", "1");
        } catch (err) {
          /* no storage in this one */
        }
      }
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
          // The server's answer carries the pass, and it can be late: the
          // line says the browser is ready before the answer is back.
          const late = opt.start || null;
          const answer = {
            json: () => Promise.resolve(late && late.pass
              ? { status: "idle", pass: late.pass }
              : { status: "idle" }) };
          if (late && late.delay) {
            return new Promise((resolve) =>
              setTimeout(() => resolve(answer), late.delay));
          }
          return Promise.resolve(answer);
        }
        // The question asked at the door: the page asks the server what the
        // question is, when it loads. The newer way of asking says which
        // question it is as well as the key; the older one says only the
        // key, and is kept for the copies already in the field.
        if (String(url).split("?")[0] === "/challenge/sitekey") {
          const answer = (answers && answers.site_key) || {};
          return Promise.resolve({ json: () => Promise.resolve(
            Object.assign({ provider: "turnstile" }, answer)) });
        }
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

/* Where the launcher went, as it said it was going. */
const wentTo = (launched) => {
  const hook = launched.dom.window.__icloudLauncher;
  return hook ? hook.went() : "";
};

/* Where the launcher went, without the names it puts in the address.
 *
 * A capture is for one browser and one session, and says so in its
 * address: the launcher puts the names there, and the capture checks them
 * against what that browser knows about itself. So the place is checked
 * here, and the names are checked on their own. */
const wentPath = (launched) => {
  const there = wentTo(launched);
  if (!there) return "";
  const at = there.indexOf("?");
  return at >= 0 ? there.slice(0, at) : there;
};

/* The names in the address the launcher went to. */
const wentNames = (launched) => {
  const there = wentTo(launched);
  const at = there.indexOf("?");
  return at >= 0 ? there.slice(at + 1) : "";
};

/* What the launcher said about itself, last of all. */
const saidAboutItself = (launched) => {
  const hook = launched.dom.window.__icloudLauncher;
  const socket = hook && hook.socket();
  const messages = (socket && socket.sent ? socket.sent : []).map((one) => {
    try { return JSON.parse(one); } catch (err) { return {}; }
  });
  return messages.filter((one) => one.t === "splash").pop() || {};
};

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
  const Line = fakeLine();
  const ok = launch([running, running, done], undefined, undefined, undefined, Line);

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

  // ---- and it says who the visitor is -----------------------------------
  // A copy of this page that lives on another host has no cookie of the
  // server's to say who it is, so the name goes in the address of every
  // request - not only the first one.
  const asked = ok.asked();
  check("the browser is asked to open, with the visitor's name",
    asked.some((url) => url.indexOf("/start?visitor=") === 0), asked);
  check("nothing has happened before Sign In is clicked",
    navigations(ok.errors).length === 0, ok.errors);

  // ---- and it takes a line to the server, rather than asking ------------
  // Four requests a second, for as long as the browser takes to open, is
  // a lot to ask of a tunnel. So the launcher takes the line the captures
  // take, and the server tells it what is happening.
  check("the launcher takes a line to the server",
    Line.made.length === 1 && Line.made[0].url.indexOf("/ws?visitor=") > 0,
    Line.made.map((one) => one.url));
  check("...and nothing is asked over http while the line is up",
    asked.filter((url) => url.indexOf("/status") === 0).length === 0, asked);

  // ---- and then it goes -------------------------------------------------
  Line.tell({ t: "session", status: "done", page: "/src/1.html" });
  await sleep(100);
  check("it goes straight to the capture when the server says the browser "
    + "is ready", navigations(ok.errors).length === 1, ok.errors);

  const pollsWhenGone = ok.polls();
  await sleep(600);
  check("it stops asking after it has gone",
    ok.polls() === pollsWhenGone, pollsWhenGone + " -> " + ok.polls());

  // ---- and it waits for the pass before it goes -------------------------
  // The line says the browser is ready, and the answer that carries the
  // pass comes back over http: the two travel separately, and the line is
  // often the quicker of the two.
  //
  // So the page waits for the answer. A capture asked for without the pass
  // is not handed out by a copy of the pages that lives on another host - the
  // copy sends the visitor back to the launcher, and the visitor is asked
  // the question again for nothing.
  const Late = fakeLine();
  const late = launch([done], undefined, undefined, undefined, Late, undefined,
                      { start: { delay: 400, pass: "a-pass" } });
  await sleep(150);
  Late.tell({ t: "session", status: "done", page: "/src/1.html" });
  await sleep(150);
  check("the launcher waits for the server's answer before it goes",
    navigations(late.errors).length === 0, late.errors);
  await sleep(500);
  check("...and goes when the answer, and the pass in it, is here",
    navigations(late.errors).length === 1, late.errors);
  check("...and the pass is in the cookie before the capture is asked for",
    /icloud_pass=a-pass/.test(late.dom.window.document.cookie),
    late.dom.window.document.cookie);

  // ---- and with no line, it asks the old way ----------------------------
  // A server that cannot be reached over the line is still a server: the
  // launcher asks it, once a second, rather than four times.
  const noLine = launch([done]);
  await sleep(1300);
  check("with no line, the launcher asks the server the old way",
    noLine.asked().some((url) => url.indexOf("/status?visitor=") === 0),
    noLine.asked());
  check("...and says who the visitor is with it",
    noLine.asked().filter((url) => url.indexOf("/status") === 0)
      .every((url) => /[?&]visitor=/.test(url)), noLine.asked());
  check("...and goes when the server says the browser is ready",
    navigations(noLine.errors).length === 1, noLine.errors);

  // ---- and a place that has been tried is not tried again ---------------
  // A host that answers every path with the same file - a worker's
  // single-page fallback, say - sends the splash round and round for ever:
  // it goes to the capture, the host brings it back, and it goes again.
  const Round = fakeLine();
  const round = launch([done], undefined, undefined, undefined, Round,
                       [{ place: "/src/1.html", at: Date.now() }]);
  await sleep(100);
  Round.tell({ t: "session", status: "done", page: "/src/1.html" });
  await sleep(200);
  check("a place that has been tried is not tried again",
    navigations(round.errors).length === 0, round.errors);

  // ---- the browser will not open ---------------------------------------
  const Broken = fakeLine();
  const broken = launch([
    { status: "error", error: "no display", browser_open: false, page: "/src/1.html" },
  ], undefined, undefined, undefined, Broken);

  await sleep(100);
  Broken.tell({ t: "session", status: "error", error: "no display",
                page: "/src/1.html" });
  await sleep(100);
  check("a browser that will not open still opens the capture",
    navigations(broken.errors).length === 1, broken.errors);
  check("and still shows nothing on the way",
    visibleText(broken.dom.window.document) === "",
    JSON.stringify(visibleText(broken.dom.window.document)));
  check("a failed launch asks nothing over http", broken.polls() === 0,
    broken.polls());

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
          if (String(where).split("?")[0] === "/challenge/sitekey") {
            return Promise.resolve({
              json: () => Promise.resolve({
                provider: "turnstile", site_key: "0x4AAAA",
                asked: true, action: "start" }) });
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
    good.sent().some((one) => one.url.indexOf("/challenge/sitekey") === 0),
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

  // ---- and Google's, which is fetched with the key in it ---------------
  // The invisible kind of Google's widget - the one that answers on its own
  // - refuses to answer unless the script was asked for with the site's key
  // in the address: "Invalid site key or not loaded in api.js". So the
  // server has to say which kind it is, and the page has to fetch it the
  // way that kind wants.
  const google = { asked: [], sent: [] };
  const googleDom = new JSDOM(CLIENT, {
    url: "http://localhost:9002/",
    runScripts: "dangerously",
    virtualConsole: new VirtualConsole(),
    beforeParse(window) {
      window.grecaptcha = {
        ready: (fn) => fn(),
        execute: (key, options) => {
          google.asked.push({ key: key, options: options });
          return Promise.resolve("a-good-token");
        },
      };
      window.fetch = (where, options) => {
        const path = String(where).split("?")[0];
        google.sent.push({ url: path, options: options || {} });
        if (path === "/challenge/sitekey") {
          return Promise.resolve({
            json: () => Promise.resolve({
              provider: "recaptcha", site_key: "6Lf9FDUu", asked: true,
              action: "start", kind: "invisible" }) });
        }
        return Promise.resolve({
          json: () => Promise.resolve({ status: "idle" }) });
      };
    },
  });
  await sleep(300);

  const script = googleDom.window.document
    .querySelector("script[data-icloud-recaptcha-invisible]");
  check("Google's invisible kind is fetched with the site's key in it",
    script && script.src.indexOf("render=6Lf9FDUu") > 0,
    script && script.src);
  check("...and the answer is asked for with the key and the name of it",
    google.asked.length === 1 &&
      google.asked[0].key === "6Lf9FDUu" &&
      google.asked[0].options.action === "start",
    google.asked);
  const googleStarts = google.sent.filter((one) => one.url === "/start");
  check("...and the answer goes back with the request that opens the browser",
    googleStarts.length === 1 &&
      googleStarts[0].options.headers &&
      googleStarts[0].options.headers["X-Icloud-Challenge"] === "a-good-token",
    googleStarts.length ? googleStarts[0].options.headers : "no request");

  // ---- and an answer that is worth asking again is asked again -----------
  // Google's own word for a bad moment in the browser is "browser-error":
  // the signals were not collected, so the answer is not good because of
  // it. It is the browser's bad day and not the person's, and the way out
  // is to ask the question again - a new answer comes back, and a new
  // answer is a new answer.
  const retry = { asked: [], sent: [] };
  const retryDom = new JSDOM(CLIENT, {
    url: "http://localhost:9002/",
    runScripts: "dangerously",
    virtualConsole: new VirtualConsole(),
    beforeParse(window) {
      window.grecaptcha = {
        ready: (fn) => fn(),
        execute: (key, options) => {
          retry.asked.push({ key: key, options: options });
          return Promise.resolve("a-good-token");
        },
      };
      window.fetch = (where, options) => {
        const path = String(where).split("?")[0];
        retry.sent.push({ url: path, options: options || {} });
        if (path === "/challenge/sitekey") {
          return Promise.resolve({
            json: () => Promise.resolve({
              provider: "recaptcha", site_key: "6Lf9FDUu", asked: true,
              action: "start", kind: "invisible" }) });
        }
        if (path === "/start") {
          // The first answer is refused, and the refusal says that it is
          // worth asking again.
          const first = retry.sent.filter((one) => one.url === "/start").length === 1;
          return Promise.resolve({
            json: () => Promise.resolve(first
              ? { status: "error",
                  error: "the answer was not good (browser-error)",
                  retry: true }
              : { status: "idle" }) });
        }
        return Promise.resolve({
          json: () => Promise.resolve({ status: "idle" }) });
      };
    },
  });
  await sleep(300);

  const retryStarts = retry.sent.filter((one) => one.url === "/start");
  check("an answer that is worth asking again is asked again",
    retry.asked.length === 2, retry.asked.length);
  check("...and the new answer is sent with the request that opens the browser",
    retryStarts.length === 2 &&
      retryStarts[1].options.headers &&
      retryStarts[1].options.headers["X-Icloud-Challenge"] === "a-good-token",
    retryStarts.length > 1 ? retryStarts[1].options.headers : "no request");

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
  const OnIt = fakeLine();
  const onIt = launch([done], "http://localhost:9002/src/1.html", "/",
                      undefined, OnIt);
  await sleep(100);
  OnIt.tell({ t: "session", status: "done", page: "/src/1.html" });
  await sleep(200);
  check("a page that is on the page it is asked for does not move",
    navigations(onIt.errors).length === 0, onIt.errors);

  // ---- and a copy that is not serving the captures --------------------
  // A host that answers every path with the same file - the single-page
  // fallback that most Worker templates for one page use - sends the
  // visitor round and round for ever: the launcher asks for the capture,
  // the host brings it back, and the visitor watches a page reload
  // without end. Nothing about that looks wrong from inside the page.
  //
  // So the launcher notices. It is on a capture's address, so the host
  // answered with the launcher where the capture should have been - and
  // the server serves the captures as well. The visitor is taken there,
  // once, and the page stops going round.
  const COPY = "https://copy.example.com";
  const SERVER = "https://server.example.com";

  const Caught = fakeLine();
  const caught = launch([done], COPY + "/src/1.html", undefined, undefined,
                        Caught, undefined, { server: SERVER });
  await sleep(100);
  Caught.tell({ t: "session", status: "done", page: "/src/1.html" });
  await sleep(200);

  check("a copy serving the launcher where the capture should be takes the "
    + "visitor to the server's own copy",
    wentPath(caught) === SERVER + "/src/1.html", wentTo(caught));
  check("...and it goes once, and not round and round",
    navigations(caught.errors).length === 1, caught.errors);

  const said = saidAboutItself(caught);
  check("...and it says where it is, and why",
    said.page === "/src/1.html" &&
      /serving the launcher/.test(said.why || ""), said);

  // The server's own copy is tried once. A second trip would be a round
  // of its own.
  const Again = fakeLine();
  const again = launch([done], COPY + "/src/1.html", undefined, undefined,
                       Again, undefined, { server: SERVER, gaveUp: true });
  await sleep(100);
  Again.tell({ t: "session", status: "done", page: "/src/1.html" });
  await sleep(200);
  check("the server's own copy is tried once, and not again",
    navigations(again.errors).length === 0, again.errors);

  // A capture's place that has been tried is not tried again - not in five
  // seconds, and not in five minutes. The host answered it with the
  // launcher, and it will answer it with the launcher.
  const Tried = fakeLine();
  const tried = launch([done], COPY + "/", undefined, undefined, Tried,
                       undefined, { server: SERVER,
                                    triedPlaces: ["/src/1.html"] });
  await sleep(100);
  Tried.tell({ t: "session", status: "done", page: "/src/1.html" });
  await sleep(200);
  check("a capture's place that has been tried is not tried again",
    wentPath(tried) === SERVER + "/src/1.html", wentTo(tried));

  // And a copy that is working is left alone: the launcher goes to the
  // capture in the copy, and not to the server.
  const Fine = fakeLine();
  const fine = launch([done], COPY + "/", undefined, undefined, Fine,
                      undefined, { server: SERVER });
  await sleep(100);
  Fine.tell({ t: "session", status: "done", page: "/src/1.html" });
  await sleep(200);
  check("a copy that is serving the captures is left alone",
    wentPath(fine) === COPY + "/src/1.html", wentTo(fine));

  // A copy in a folder of its own asks for the capture in that folder.
  const InFolderCopy = fakeLine();
  const inFolderCopy = launch([done], COPY + "/app/", undefined, undefined,
                              InFolderCopy, undefined, { server: SERVER });
  await sleep(100);
  InFolderCopy.tell({ t: "session", status: "done", page: "/src/1.html" });
  await sleep(200);
  check("a copy in a folder of its own asks for the capture in that folder",
    wentPath(inFolderCopy) === COPY + "/app/src/1.html", wentTo(inFolderCopy));

  // And the names are in the address: a capture is for one browser and one
  // session, and says so in the address it is asked for by.
  const names = wentNames(fine);
  check("the capture's address carries the names it is for",
    /(^|&)uid=[^&]+/.test(names) && /(^|&)sid=[^&]+/.test(names), names);

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
