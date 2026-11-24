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
 *     connection in the feed and in the counters, and
 *   * a long value never makes the page wider than the screen: the page does
 *     not scroll across, the cells wrap, and on a phone a row is a card with
 *     each value under the name of its column.
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
  // Every request the panel made, so that what a button does can be read
  // off the wire rather than off the markup.
  const calls = [];
  let agree = true;
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
      window.confirm = () => agree;
      window.fetch = (url, options) => {
        const where = String(url);
        const asked = options || {};
        calls.push({ url: where, method: asked.method || "GET",
                     body: asked.body });
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

  return { dom, sockets, errors, calls,
           refuse: () => { lineRefused = true; },
           disagree: () => { agree = false; } };
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

  // ---- closing a browser -----------------------------------------------
  // The operator's own answer to browsers piling up. Closing is not
  // forgetting: the client stays on the list, and their cookies stay theirs.
  const closing = open({ signedIn: true });
  await sleep(120);
  push(closing.sockets[0], 1, [
    row({ id: "aclient", online: true, browser_open: true, idle_seconds: 900 }),
  ]);
  await sleep(20);

  const closeButton = closing.dom.window.document.querySelector("button[data-close]");
  check("a client the manager holds can be closed from the panel",
    !!closeButton, text(closing.dom, "clients-body"));

  // Saying no to the question asks the server for nothing.
  closing.disagree();
  closeButton.click();
  await sleep(40);
  check("...but not if the operator changes their mind",
    !closing.calls.some((call) => call.url.includes("/close")),
    JSON.stringify(closing.calls));

  const again = open({ signedIn: true });
  await sleep(120);
  push(again.sockets[0], 1, [
    row({ id: "aclient", online: true, browser_open: true, idle_seconds: 900 }),
  ]);
  await sleep(20);
  again.dom.window.document.querySelector("button[data-close]").click();
  await sleep(60);
  const did = again.calls.find((call) => call.url.includes("/close"));
  check("closing a client asks the server to close that client",
    !!did && did.method === "POST" &&
      did.url === "/admin/api/clients/aclient/close", JSON.stringify(again.calls));

  // ---- closing the ones nobody is at -----------------------------------
  // The button closes every browser nobody has been at for five minutes.
  // Who is idle is the server's answer, so the panel asks once and says
  // what came back.
  again.calls.length = 0;
  push(again.sockets[0], 2, [
    row({ id: "aidle", online: true, browser_open: true, idle_seconds: 900 }),
    row({ id: "awatched", online: true, browser_open: true, here: true,
          viewers: 1, idle_seconds: 900 }),
  ]);
  await sleep(20);
  again.dom.window.document.getElementById("close-idle").click();
  await sleep(60);
  const bulk = again.calls.find((call) => call.url === "/admin/api/clients/close");
  check("the idle browsers are closed in one ask",
    !!bulk && bulk.method === "POST" &&
      JSON.parse(bulk.body || "{}").idle_seconds === 300,
    JSON.stringify(again.calls));

  // A panel with nothing idle says so instead of asking.
  again.calls.length = 0;
  push(again.sockets[0], 3, [
    row({ id: "awatched", online: true, browser_open: true, here: true,
          viewers: 1, idle_seconds: 900 }),
    row({ id: "afresh", online: true, browser_open: true, idle_seconds: 5 }),
  ]);
  await sleep(20);
  again.dom.window.document.getElementById("close-idle").click();
  await sleep(40);
  check("nothing idle means nothing is asked",
    !again.calls.some((call) => call.url === "/admin/api/clients/close"),
    JSON.stringify(again.calls));
  check("...and the panel says why",
    text(again.dom, "clients-sub").includes("idle for 5 minutes"),
    text(again.dom, "clients-sub"));

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

  // ---- the width of the screen ------------------------------------------
  // A session id, an error out of the browser, a line out of the feed: any of
  // them can be longer than the panel it sits in. None of them may make the
  // page wider than the screen - the page is not scrolled across, ever. What
  // is long wraps where it sits, and the one thing that cannot wrap, a table
  // of eight columns, is drawn as cards on a phone.
  const widths = open({ signedIn: true });
  await sleep(120);
  widths.sockets[0].onopen();
  const longId = "a-session-id-that-goes-on-and-on-and-on-and-on-for-ever";
  const longError = 'locator("#aid-auth-widget-iFrame").contentFrame() timed ' +
    "out after 30000ms: the element is not visible because it has no size and " +
    "https://idmsa.apple.com/appleauth/auth/signin/a/very/long/address/indeed";
  push(widths.sockets[0], 1, [row({ id: longId, error: longError })], []);
  await sleep(20);
  const wdoc = widths.dom.window.document;
  const css = Array.prototype.map.call(
    wdoc.querySelectorAll("style"), (style) => style.textContent).join("\n");
  const flat = css.replace(/\s+/g, " ");

  check("the page is not scrolled across", /html, body \{ overflow-x: clip/.test(flat),
    (flat.match(/html, body \{[^}]*\}/) || [])[0]);
  check("...and its content column can be narrower than what is in it",
    flat.indexOf("grid-template-columns: 248px minmax(0, 1fr)") >= 0,
    (flat.match(/\.shell \{[^}]*\}/) || [])[0]);
  check("...so a long value wraps instead of widening the page",
    /td \{[^}]*overflow-wrap: anywhere/.test(flat), css.slice(0, 200));
  check("...and the tables live in a box of their own", (() => {
    const tables = wdoc.querySelectorAll("table");
    return tables.length > 0 && Array.prototype.every.call(
      tables, (table) => table.parentNode.classList.contains("tablewrap"));
  })(), wdoc.querySelectorAll("table").length);
  check("...which scrolls inside the panel, not the page",
    /\.tablewrap \{[^}]*overflow-x: auto/.test(flat),
    (flat.match(/\.tablewrap \{[^}]*\}/) || [])[0]);

  // A drawn row says which column each cell is, so that a card on a phone can
  // show the name beside the value.
  const cells = wdoc.querySelectorAll("#clients-body tr:first-child td");
  const labelled = Array.prototype.filter.call(
    cells, (cell) => cell.hasAttribute("data-label"));
  check("a row says which column every cell is",
    cells.length === 8 && labelled.length === 7 &&
    labelled[0].getAttribute("data-label") === "Client",
    Array.prototype.map.call(cells, (cell) =>
      cell.getAttribute("data-label")).join(" | "));
  check("...and the action cell is the one with no column name",
    !cells[cells.length - 1].hasAttribute("data-label"));
  check("...and a long value is all there, to wrap", 
    text(widths.dom, "clients-body").indexOf(longId) >= 0 &&
    text(widths.dom, "clients-body").indexOf("30000ms") >= 0,
    text(widths.dom, "clients-body").slice(0, 120));

  // On a phone: the row is a card, one value to a line.
  const phone = flat.slice(flat.indexOf("@media (max-width: 760px)"));
  check("a phone draws a row as a card, not eight columns",
    phone.length > 0 && /thead \{ display: none/.test(phone) &&
    phone.indexOf("content: attr(data-label)") >= 0 &&
    phone.indexOf("display: flex") >= 0, phone.slice(0, 200));

  // ---- deleting what has piled up --------------------------------------
  // A server that has been up for a while has a feed nobody will read again
  // and a list of clients who have gone. The panel can delete: one line of
  // the feed, the whole feed, or a client who has gone. None of it touches
  // the clients who are still here.
  const tidy = open({ signedIn: true });
  await sleep(120);
  tidy.sockets[0].onopen();
  push(tidy.sockets[0], 60, [row({ live: false, online: false, known: false })], [
    { id: 7, at: Date.now() / 1000, event: "connected", session: "aprobevisitor42" },
    { id: 6, at: Date.now() / 1000 - 5, event: "finished", session: "aprobevisitor42" },
  ]);
  await sleep(20);
  const tdoc = tidy.dom.window.document;

  const crosses = tdoc.querySelectorAll("#events button[data-forget]");
  check("every line of the feed can be deleted", crosses.length === 2,
    crosses.length);
  check("...and carries the number the server gave it",
    crosses[0].getAttribute("data-forget") === "7",
    crosses[0].getAttribute("data-forget"));

  crosses[1].click();
  await sleep(20);
  const forgotten = tidy.calls.filter((call) => call.url === "/admin/api/events/forget");
  check("deleting a line tells the server which line",
    forgotten.length === 1 && forgotten[0].method === "POST" &&
    JSON.parse(forgotten[0].body).id === 6, forgotten);

  tdoc.getElementById("events-clear").click();
  await sleep(20);
  const cleared = tidy.calls.filter((call) => call.url === "/admin/api/events/clear");
  check("the whole feed can be emptied", cleared.length === 1 &&
    cleared[0].method === "POST", cleared);
  check("...and the panel does not wait to say so",
    tdoc.getElementById("events").textContent.indexOf("Nothing yet") >= 0,
    tdoc.getElementById("events").textContent);

  // A client who has gone: deleting them is the whole client - the record,
  // the cookies that were written down, and the profile.
  const deletes = tdoc.querySelectorAll("#clients-body button[data-delete]");
  check("a client who has gone can be deleted", deletes.length === 1 &&
    deletes[0].getAttribute("data-delete") === "aprobevisitor42",
    tdoc.getElementById("clients-body").innerHTML.slice(0, 160));
  deletes[0].click();
  await sleep(20);
  const deleting = tidy.calls.filter((call) => /\/delete$/.test(call.url));
  check("...and it is the client's own address that is asked",
    deleting.length === 1 && deleting[0].method === "POST" &&
    deleting[0].url === "/admin/api/clients/aprobevisitor42/delete", deleting);

  // A client whose browser is still running is not deleted from under
  // themselves: closing comes first, and closing keeps them.
  push(tidy.sockets[0], 61, [row({ online: true, browser_open: true })], []);
  await sleep(20);
  check("a client who is here can be closed, not deleted",
    !tdoc.querySelector("#clients-body button[data-delete]") &&
    !!tdoc.querySelector("#clients-body button[data-close]"),
    tdoc.getElementById("clients-body").innerHTML.slice(0, 160));

  // Nothing so final happens without being asked.
  const wary = open({ signedIn: true });
  await sleep(120);
  wary.sockets[0].onopen();
  push(wary.sockets[0], 1, [row({ live: false, online: false, known: false })], []);
  await sleep(20);
  wary.disagree();
  wary.dom.window.document.querySelector("#clients-body button[data-delete]").click();
  await sleep(20);
  check("a client is not deleted without being asked first",
    wary.calls.filter((call) => /\/delete$/.test(call.url)).length === 0,
    wary.calls);

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
