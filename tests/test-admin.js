/* Exercises admin.html - the control room - in jsdom.
 *
 * The panel does not ask the server for the clients and it does not keep a
 * list of its own: it subscribes to the session manager's line, and what it
 * draws is what was pushed to it. Two things are checked here:
 *
 *   * a browser that is already signed in opens the panel and subscribes on
 *     the way in, instead of asking for the secret again, and
 *   * a client that connects is drawn as connected - "Connecting…" while
 *     the browser is still opening, "Online" once it is up - with the
 *     connection in the feed and in the counters.
 *
 * The socket is a stand-in: the test pushes to the page the way the server
 * pushes, and looks at what the page did with it.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ADMIN = fs.readFileSync(path.join(__dirname, "..", "admin.html"), "utf8");

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

function answer(body, status) {
  return Promise.resolve({
    ok: status < 400,
    status: status,
    json: () => Promise.resolve(body),
  });
}

/* A panel, loaded the way a browser loads it. `signedIn` is whether the
 * admin's cookie is already good; `missingSecret` is a server with no
 * secret at all. */
function open({ signedIn = true, missingSecret = false } = {}) {
  const sockets = [];
  const errors = [];
  // The secret can be taken away while the panel is open: the line was let
  // in, and the answer to the API is refused. That is what a panel whose
  // secret has been changed sees.
  let lineRefused = false;
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (event) =>
    errors.push(String((event && event.message) || event)));

  const dom = new JSDOM(ADMIN, {
    url: "http://localhost:9002/admin",
    runScripts: "dangerously",
    virtualConsole,
    beforeParse(window) {
      window.fetch = (url) => {
        const where = String(url);
        if (where === "/admin/api/session") {
          if (missingSecret) {
            return answer({ error: "no admin secret has been set." }, 503);
          }
          return signedIn
            ? answer({ ok: true, token_set: true }, 200)
            : answer({ error: "the admin's secret is needed" }, 403);
        }
        if (where === "/admin/api/clients") {
          return signedIn && !lineRefused
            ? answer({ revision: 0, clients: [], events: [], counts: {} }, 200)
            : answer({ error: "the admin's secret is needed" }, 403);
        }
        return answer({}, 200);
      };
      window.WebSocket = function (url) {
        this.url = url;
        this.sent = [];
        sockets.push(this);
      };
    },
  });

  return { dom, sockets, errors, refuse: () => { lineRefused = true; } };
}

function row(fields) {
  return Object.assign({
    id: "aprobevisitor42",
    live: true,
    online: false,
    status: "idle",
    stage: "Address",
    page: "/src/1.html",
    step: "",
    error: "",
    browser_open: false,
    browser_url: "",
    browser_title: "",
    viewers: 0,
    here: false,
    waiting: false,
    finished: false,
    idle_seconds: 0.4,
    export_allowed: false,
    profile: "aprobevisitor42",
    known: true,
  }, fields || {});
}

function push(socket, revision, clients, events) {
  socket.onmessage({
    data: JSON.stringify({
      t: "state",
      revision: revision,
      clients: clients,
      events: events || [],
      counts: {},
    }),
  });
}

const text = (dom, id) => dom.window.document.getElementById(id).textContent;

(async () => {
  // ---- a panel that is already signed in --------------------------------
  const panel = open({ signedIn: true });
  const doc = panel.dom.window.document;
  await sleep(120);

  check("the gate comes down when the browser is already signed in",
    doc.getElementById("gate").classList.contains("hidden") &&
      !doc.getElementById("shell").classList.contains("hidden"),
    doc.getElementById("gate").className);
  check("...and the panel lays itself out", !!doc.querySelector("#clients-body"));
  check("...and it subscribed on the way in, without being asked",
    panel.sockets.length === 1, panel.sockets.length);
  check("...to the session manager's line",
    panel.sockets[0].url === "ws://localhost:9002/admin/ws",
    panel.sockets[0].url);

  // ---- a client connects ------------------------------------------------
  const socket = panel.sockets[0];
  socket.onopen();
  push(socket, 5, [row()], [
    { at: Date.now() / 1000, event: "connected", session: "aprobevisitor42" },
  ]);
  await sleep(20);

  const clients = text(panel.dom, "clients-body");
  check("a client who has connected is on the panel", clients.includes("aprobevisitor42"),
    clients);
  check("...and is said to be connecting, not idle",
    clients.includes("Connecting") && !clients.includes("Idle"), clients);
  check("...and the feed says a client connected",
    text(panel.dom, "events").includes("a new client connected"),
    text(panel.dom, "events"));
  check("...and it is counted as a client",
    text(panel.dom, "s-total") === "1", text(panel.dom, "s-total"));
  check("...and no browser is counted online yet",
    text(panel.dom, "s-online") === "0", text(panel.dom, "s-online"));
  check("...and the line is said to be live",
    text(panel.dom, "last-seen").startsWith("live"),
    text(panel.dom, "last-seen"));

  // ---- the browser comes up --------------------------------------------
  push(socket, 6, [
    row({ online: true, browser_open: true, status: "active", here: true,
          viewers: 1, browser_url: "https://idmsa.apple.com/signin",
          browser_title: "Sign in to your Apple Account" }),
  ]);
  await sleep(20);
  const online = text(panel.dom, "clients-body");
  check("a client whose browser is up is online", online.includes("Online"), online);
  check("...and where the browser is", online.includes("idmsa.apple.com"), online);
  check("...and counted as online", text(panel.dom, "s-online") === "1",
    text(panel.dom, "s-online"));
  check("...and counted as here", text(panel.dom, "s-here") === "1",
    text(panel.dom, "s-here"));

  // ---- a picture from before is not drawn over a newer one -------------
  push(socket, 4, [row({ id: "an-older-client" }), row({ id: "and-another" })]);
  await sleep(20);
  check("a picture older than the one on screen is not drawn",
    !text(panel.dom, "clients-body").includes("an-older-client"),
    text(panel.dom, "clients-body"));

  // ---- a line that is refused ------------------------------------------
  panel.refuse();
  socket.onclose({ code: 1008 });
  await sleep(120);
  check("a refused line says the secret is needed",
    doc.getElementById("banner").classList.contains("show") &&
      text(panel.dom, "banner-text").includes("secret is needed"),
    text(panel.dom, "banner-text"));
  check("...and offers the secret again",
    doc.getElementById("banner-button").style.display !== "none",
    doc.getElementById("banner-button").style.display);

  // It keeps trying: a secret that is put back does not need the panel to
  // be reloaded.
  await sleep(1100);
  check("...and the panel keeps trying to listen",
    panel.sockets.length === 2, panel.sockets.length);

  // ---- a browser that is not signed in ---------------------------------
  const gate = open({ signedIn: false });
  await sleep(120);
  check("a browser that is not signed in is asked for the secret",
    !gate.dom.window.document.getElementById("gate").classList.contains("hidden"));
  check("...and nothing is subscribed until it is",
    gate.sockets.length === 0, gate.sockets.length);

  // ---- a server with no secret at all ----------------------------------
  const bare = open({ signedIn: false, missingSecret: true });
  await sleep(120);
  check("a server with no secret says so on the gate",
    text(bare.dom, "gate-err").toLowerCase().includes("no admin secret"),
    text(bare.dom, "gate-err"));
  check("...and still does not subscribe", bare.sockets.length === 0,
    bare.sockets.length);

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
