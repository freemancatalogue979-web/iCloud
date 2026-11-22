/* Exercises sync.js - the engine injected into every captured page - in jsdom.
 *
 * There is no real browser here, so the WebSocket is a stub and nothing is
 * laid out. What is being pinned is the protocol: what the page sends when it
 * is typed into, what it does with what comes back, and the two things that
 * must not happen - an echo of a value the server just applied, and a click
 * being replayed.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SYNC = fs.readFileSync(path.join(__dirname, "..", "sync.js"), "utf8");

const PAGE = `<!DOCTYPE html>
<html><head><title>Sign in to iCloud</title></head>
<body>
  <form>
    <label for="account">Apple ID</label>
    <input id="account" name="account" type="text">
    <input id="remember" name="remember" type="checkbox">
    <select id="country" name="country">
      <option value="us">United States</option>
      <option value="ng">Nigeria</option>
    </select>
    <button id="go" type="button">Continue</button>
    <input class="bare" type="text">
    <a id="next" href="/src/2.html">Next page</a>
  </form>
</body></html>`;

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

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1; // OPEN
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    FakeSocket.last = this;
  }

  send(data) {
    this.sent.push(JSON.parse(data));
  }

  close() {
    if (this.readyState === 3) return;
    this.readyState = 3;
    if (this.onclose) this.onclose({});
  }

  receive(message) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(message) });
  }

  ofType(type) {
    return this.sent.filter((message) => message.t === type);
  }
}

(async () => {
  const dom = new JSDOM(PAGE, {
    url: "http://localhost:9002/src/1.html",
    runScripts: "outside-only",
  });
  const win = dom.window;
  const doc = win.document;

  win.WebSocket = FakeSocket;
  win.eval(SYNC);

  const sock = FakeSocket.last;
  check("connects to the sync socket", !!sock && sock.url === "ws://localhost:9002/ws",
    sock && sock.url);
  check("says nothing before the socket opens", sock && sock.sent.length === 0,
    sock && JSON.stringify(sock.sent));

  sock.onopen();
  const hello = sock.sent[0] || {};
  check("says hello with the page it is showing",
    hello.t === "hello" && hello.page === "/src/1.html" && typeof hello.from === "string",
    JSON.stringify(hello));

  // ---- typing -----------------------------------------------------------
  const account = doc.getElementById("account");
  account.value = "someone@example.com";
  account.dispatchEvent(new win.Event("input", { bubbles: true }));
  check("typing is not sent mid-word", sock.ofType("value").length === 0,
    JSON.stringify(sock.sent));

  await sleep(120);
  const typed = sock.ofType("value")[0] || {};
  check("typing is sent once the word settles",
    typed.key === "#account" && typed.value === "someone@example.com" && typed.checked === false,
    JSON.stringify(typed));

  const before = sock.ofType("value").length;
  account.dispatchEvent(new win.Event("input", { bubbles: true }));
  await sleep(120);
  check("an unchanged field is not sent again", sock.ofType("value").length === before,
    JSON.stringify(sock.sent.slice(before)));

  // ---- one-shot controls ------------------------------------------------
  const remember = doc.getElementById("remember");
  remember.checked = true;
  remember.dispatchEvent(new win.Event("change", { bubbles: true }));
  const checked = sock.ofType("value").find((m) => m.key === "#remember") || {};
  check("a checkbox is sent at once, with its state",
    checked.value !== undefined && checked.checked === true, JSON.stringify(checked));

  const country = doc.getElementById("country");
  country.value = "ng";
  country.dispatchEvent(new win.Event("change", { bubbles: true }));
  const selected = sock.ofType("value").find((m) => m.key === "#country") || {};
  check("a select is sent with its value", selected.value === "ng", JSON.stringify(selected));

  // ---- clicks -----------------------------------------------------------
  doc.getElementById("go").dispatchEvent(
    new win.MouseEvent("click", { bubbles: true, clientX: 15, clientY: 25 })
  );
  const click = sock.ofType("click")[0] || {};
  check("a click is relayed with where it landed",
    click.key === "#go" && typeof click.x === "number" && typeof click.y === "number",
    JSON.stringify(click));

  // A click from another viewer draws a marker and presses nothing.
  const buttonsBefore = doc.querySelectorAll("button").length;
  sock.receive({ t: "click", key: "#go", x: 40, y: 50 });
  const markers = doc.querySelectorAll('[aria-hidden="true"]');
  check("a remote click is drawn, not replayed",
    markers.length === 1 && doc.querySelectorAll("button").length === buttonsBefore,
    markers.length + " marker(s)");

  await sleep(520);
  check("the marker clears itself", doc.querySelectorAll('[aria-hidden="true"]').length === 0);

  // ---- what the server sends back ---------------------------------------
  const sentBefore = sock.ofType("value").length;
  sock.receive({ t: "value", key: "#account", value: "shared@example.com", checked: false });
  check("a remote value lands on the field",
    account.value === "shared@example.com", account.value);

  await sleep(120);
  check("applying a value does not echo it back",
    sock.ofType("value").length === sentBefore,
    JSON.stringify(sock.ofType("value").slice(sentBefore)));

  sock.receive({
    t: "state",
    page: "/src/1.html",
    values: {
      "#account": { value: "stored@example.com", checked: false },
      "#remember": { value: "on", checked: true },
    },
  });
  check("a freshly loaded viewer gets the stored state",
    account.value === "stored@example.com" && remember.checked === true,
    account.value + " / " + remember.checked);

  // An element with no id still has a name the server can use.
  const bare = doc.querySelector(".bare");
  const key = win.__icloudSync.keyFor(bare);
  check("an element without an id gets a positional key",
    key.indexOf("body") === 0 && key.indexOf("input") !== -1, key);

  bare.value = "typed";
  bare.dispatchEvent(new win.Event("input", { bubbles: true }));
  await sleep(120);
  const bareMessage = sock.ofType("value").find((m) => m.value === "typed") || {};
  check("the positional key is what is sent", bareMessage.key === key,
    JSON.stringify(bareMessage));

  // ---- navigation -------------------------------------------------------
  const next = doc.getElementById("next");
  // jsdom cannot navigate; the point is the message, not the document swap.
  next.addEventListener("click", (event) => event.preventDefault());
  next.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true }));
  const nav = sock.ofType("nav")[0] || {};
  check("following a captured link tells the server",
    nav.page === "/src/2.html", JSON.stringify(nav));

  // ---- staying inside the captures --------------------------------------
  const outside = doc.createElement("a");
  outside.setAttribute("href", "https://www.icloud.com/");
  outside.textContent = "Open the real site";
  doc.body.appendChild(outside);

  const outsideClick = new win.MouseEvent("click", { bubbles: true, cancelable: true });
  outside.dispatchEvent(outsideClick);
  check("a link that leaves the captures is not followed",
    outsideClick.defaultPrevented &&
      sock.ofType("nav").length === 1 &&
      sock.ofType("click").some((m) => m.key === win.__icloudSync.keyFor(outside)),
    JSON.stringify(sock.ofType("nav")));

  const hash = doc.createElement("a");
  hash.setAttribute("href", "#top");
  hash.textContent = "Top";
  doc.body.appendChild(hash);
  const hashClick = new win.MouseEvent("click", { bubbles: true, cancelable: true });
  hash.dispatchEvent(hashClick);
  check("a same-page anchor is left alone", !hashClick.defaultPrevented);

  const submitted = new win.Event("submit", { bubbles: true, cancelable: true });
  doc.querySelector("form").dispatchEvent(submitted);
  check("a captured form does not navigate away", submitted.defaultPrevented);

  // ---- reconnecting -----------------------------------------------------
  const closed = sock;
  closed.close();
  await sleep(600);
  const second = FakeSocket.last;
  check("the page reconnects on its own", second && second !== closed,
    second && second.url);
  second.onopen();
  const again = second.sent[0] || {};
  check("a reconnected viewer asks for the state again",
    again.t === "hello" && again.page === "/src/1.html", JSON.stringify(again));

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
