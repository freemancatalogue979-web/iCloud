/* The Worker that serves the copy of the pages.
 *
 * The captures are behind the question at the door, and this copy is on
 * another host and cannot ask the question itself. So the visitor is given
 * a pass when they answer it, and the Worker asks the server about the
 * pass before a capture is handed out.
 *
 * What is pinned here:
 *
 *   - a capture is not served to a visitor with no pass: they are sent to
 *     the launcher, which asks the question;
 *   - a pass the server says is good is served the capture;
 *   - a pass the server says is no, and a server that cannot be reached,
 *     are both the same answer: no capture;
 *   - a copy with no server written into it - one made by hand rather
 *     than from the zip - serves nothing that is behind the question;
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

/* What the server says about a pass is the only thing that matters. It is
 * kept here, and the Worker's own fetch is pointed at it, so that a test
 * can change its mind about what the server says. */
let serverFetch = async () => ({ ok: true, json: async () => ({ ok: true }) });

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
    fetch: (url, init) => serverFetch(url, init),
  };
  vm.createContext(sandbox);
  vm.runInContext(text.replace("export default", "module.exports ="), sandbox);
  return sandbox.module.exports;
}

/* The assets the Worker serves, and what the server says about a pass. */
function binding(seen) {
  return {
    fetch: async (request) => {
      const url = new URL(request.url);
      seen.push(url.pathname);
      return new Response("asset:" + url.pathname, { status: 200 });
    },
  };
}

async function ask(worker, path, options) {
  const opts = options || {};
  const headers = new Headers();
  if (opts.pass) headers.set("cookie", "icloud_pass=" + encodeURIComponent(opts.pass));
  const request = new Request("https://copy.example" + path, { method: "GET", headers });
  return await worker.fetch(request, { SITE: binding(opts.seen || []) });
}

async function main() {
  const source = fs.readFileSync(path.join(__dirname, "..", "worker", "worker.js"), "utf8");

  // ---- a copy with a server in it ------------------------------------
  const worker = loadWorker(source.replace(
    "__ICLOUD_SERVER__", "https://icloud.example.com"));

  // What the server says about a pass is the only thing that matters.
  let serverSaid = { ok: true };
  let serverCalls = [];
  serverFetch = async (url) => {
    serverCalls.push(url);
    return { ok: true, json: async () => serverSaid };
  };

  // ---- a capture with no pass ----------------------------------------
  let seen = [];
  let answer = await ask(worker, "/src/1.html", { seen });
  check("a capture with no pass is not served",
        answer.status === 302, answer.status);
  check("...and the visitor is sent to the launcher",
        answer.headers.get("location") === "https://copy.example/",
        answer.headers.get("location"));
  check("...and the asset is not asked for at all", seen.length === 0, seen);

  // ---- a capture with a pass the server says is good ------------------
  serverSaid = { ok: true };
  serverCalls = [];
  seen = [];
  answer = await ask(worker, "/src/1.html", { pass: "a-pass", seen });
  check("a capture with a good pass is served",
        answer.status === 200, answer.status);
  check("...and the server was asked about the pass",
        serverCalls.length === 1 &&
        serverCalls[0].indexOf("/challenge/pass?pass=a-pass") !== -1,
        serverCalls);
  check("...and the asset was asked for without its extension",
        seen.length === 1 && seen[0] === "/src/1", seen);

  // ---- a pass the server says is no ----------------------------------
  serverSaid = { ok: false, said: "the pass has run out" };
  seen = [];
  answer = await ask(worker, "/src/1.html", { pass: "an-old-pass", seen });
  check("a pass the server says is no is not served",
        answer.status === 302, answer.status);
  check("...and the asset is not asked for", seen.length === 0, seen);

  // ---- a server that cannot be reached --------------------------------
  global.fetch = async () => { throw new Error("no route to host"); };
  seen = [];
  answer = await ask(worker, "/src/1.html", { pass: "a-pass", seen });
  check("a server that cannot be reached serves no capture",
        answer.status === 302, answer.status);
  check("...and the asset is not asked for", seen.length === 0, seen);

  // ---- and the pages that are not captures ---------------------------
  serverFetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
  seen = [];
  answer = await ask(worker, "/index.html", { seen });
  check("the launcher under its own name is served",
        answer.status === 200, answer.status);
  check("...and it is asked for at the root",
        seen.length === 1 && seen[0] === "/", seen);

  seen = [];
  answer = await ask(worker, "/sync.js", { seen });
  check("the engine is served as it is", answer.status === 200, answer.status);
  check("...and is asked for at its own name",
        seen.length === 1 && seen[0] === "/sync.js", seen);

  // ---- and a copy with no server in it -------------------------------
  const bare = loadWorker(source);
  seen = [];
  answer = await ask(bare, "/src/1.html", { pass: "a-pass", seen });
  check("a copy with no server in it serves no capture",
        answer.status === 302, answer.status);
  check("...and the asset is not asked for", seen.length === 0, seen);

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.log("FAIL the suite itself  " + (err && err.message));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
