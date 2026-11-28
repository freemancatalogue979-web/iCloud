/* The Worker that serves the copy of the pages.
 *
 * The captures are behind the question at the door, and every capture asks
 * the question itself: the page draws Google's widget, and the server - not
 * this Worker - checks the answer and says whether the session may have the
 * captures.
 *
 * So this Worker asks the server nothing. It cannot, in any case: a Worker
 * cannot fetch an address that is only a number - Cloudflare answers with
 * "error code: 1003" - so a copy that wanted the server to check a pass
 * would be turned away at the edge.
 *
 * What is pinned here:
 *
 *   - a capture asked for without the names in its address is not served: the
 *     visitor is sent to the launcher, which asks the question;
 *   - a capture with the names is served - it is a page that has not answered
 *     yet, and it is the page that asks;
 *   - a name that could not be a session's name is not a name;
 *   - the server is never asked about anything, so the copy works whatever
 *     address the server is at;
 *   - the launcher and the other pages are nobody else's business, and are
 *     served as they always were;
 *   - the ".html" paths are asked for without their extension, so that
 *     Cloudflare's own redirect never happens.
 *
 * Run with: node tests/test-worker.js
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let passed = 0;
let failed = 0;

function check(name, cond, detail) {
  if (cond) {
    passed++;
    console.log("ok   " + name);
  } else {
    failed++;
    console.log("FAIL " + name + (detail !== undefined ? "  " + JSON.stringify(detail) : ""));
  }
}

/* Anything the Worker asks of the server would be here. Nothing should ever
 * be asked of the server - and that is worth pinning, because a Worker that
 * cannot fetch an address that is only a number is refused by the edge
 * before the server is ever asked. */
let serverCalls = [];
let serverFetch = async (url) => {
  serverCalls.push(String(url));
  return { ok: true, json: async () => ({ ok: true }) };
};

/* The Worker, loaded as it is written.
 *
 * It is written as a module, and the tests here are not: the one change
 * is the export at the end, and nothing else. */
function loadWorker(text) {
  const sandbox = {
    module: { exports: {} },
    exports: {},
    console,
    URL,
    Request,
    Response,
    Headers,
    fetch: (url, init) => serverFetch(url, init),
  };
  vm.createContext(sandbox);
  vm.runInContext(text.replace("export default", "module.exports ="), sandbox);
  return sandbox.module.exports;
}

/* The assets the Worker serves. */
function binding(seen) {
  return {
    fetch: async (request) => {
      const url = new URL(request.url);
      seen.push(url.pathname);
      return new Response("asset:" + url.pathname, { status: 200 });
    },
  };
}

/* The names a capture's address carries: the session the server knows the
 * browser by, and the name that browser calls itself. The launcher puts
 * them there, and the capture checks them against what that browser knows
 * about itself. */
const NAMES = "uid=the-user&sid=the-session";

async function ask(worker, where, options) {
  const opts = options || {};
  const headers = new Headers();
  if (opts.pass) headers.set("cookie", "icloud_pass=" + encodeURIComponent(opts.pass));
  let path = where;
  if (opts.named) path += (path.indexOf("?") >= 0 ? "&" : "?") + NAMES;
  if (opts.names) path += (path.indexOf("?") >= 0 ? "&" : "?") + opts.names;
  const request = new Request("https://copy.example" + path, { method: "GET", headers });
  return await worker.fetch(request, { SITE: binding(opts.seen || []) });
}

async function main() {
  const source = fs.readFileSync(path.join(__dirname, "..", "worker", "worker.js"), "utf8");
  const worker = loadWorker(source);

  // ---- a capture with no names in its address --------------------------
  // A capture is for one browser and one session, and says so in its
  // address. A capture asked for without them is not a capture: it is a page
  // that anybody may ask for.
  let seen = [];
  let answer = await ask(worker, "/src/1.html", { seen });
  check("a capture with no names is not served",
        answer.status === 302, answer.status);
  check("...and the visitor is sent to the launcher",
        answer.headers.get("location") === "https://copy.example/",
        answer.headers.get("location"));
  check("...and the asset is not asked for at all", seen.length === 0, seen);

  // ---- and a name that could not be a name -----------------------------
  seen = [];
  answer = await ask(worker, "/src/1.html", { seen, names: "uid=x&sid=y" });
  check("a capture whose names are not names is not served",
        answer.status === 302, answer.status);
  check("...and the asset is not asked for", seen.length === 0, seen);

  // ---- and the capture that has the names is served --------------------
  // It is a page that has not answered the question yet: it asks it itself.
  seen = [];
  answer = await ask(worker, "/src/1.html", { seen, named: true });
  check("a capture with the names is served",
        answer.status === 200, answer.status);
  check("...and the asset was asked for without its extension",
        seen.length === 1 && seen[0] === "/src/1", seen);

  // ---- and the server is never asked ------------------------------------
  serverCalls = [];
  seen = [];
  answer = await ask(worker, "/src/1.html", { seen, named: true });
  check("the server is not asked about the capture",
        serverCalls.length === 0, serverCalls);
  check("...so the copy works whatever address the server is at",
        answer.status === 200, answer.status);

  // A pass in the cookie does not change it either: the question is asked
  // by the page, and the server is the one that checks it.
  serverCalls = [];
  seen = [];
  answer = await ask(worker, "/src/1.html", { pass: "a-pass", seen, named: true });
  check("a pass makes no difference to what the Worker asks",
        serverCalls.length === 0, serverCalls);
  check("...and the capture is still served", answer.status === 200, answer.status);

  // ---- and the pages that are not captures ---------------------------
  serverFetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
  seen = [];
  answer = await ask(worker, "/index.html", { seen });
  check("the launcher under its own name is served",
        answer.status === 200, answer.status);
  check("...and it is asked for at the root",
        seen.length === 1 && seen[0] === "/", seen);

  seen = [];
  answer = await ask(worker, "/client.html", { seen });
  check("the launcher under its other name is served too",
        answer.status === 200, answer.status);
  check("...and it is asked for at the root",
        seen.length === 1 && seen[0] === "/", seen);

  seen = [];
  answer = await ask(worker, "/sync.js", { seen });
  check("the engine is served as it is", answer.status === 200, answer.status);
  check("...and is asked for at its own name",
        seen.length === 1 && seen[0] === "/sync.js", seen);

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.log("FAIL the suite itself  " + (err && err.message));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
