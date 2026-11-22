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

  // ---- the client's own state -------------------------------------------
  // How far this person has got is kept in their own browser, and the
  // server is told about it when the engine says hello.
  const stored = JSON.parse(win.localStorage.getItem("icloud_state") || "null");
  check("the step is kept in the client's own storage",
    stored && stored.stage === "Address" && stored.page === "/src/1.html" &&
    stored.finished === false, JSON.stringify(stored));
  check("...and it is said when the engine says hello",
    hello.state && hello.state.stage === "Address", JSON.stringify(hello.state));

  // Somebody who has got in stays got in: the flag is not taken away by
  // opening a capture again.
  const theSecond = new JSDOM(PAGE, {
    url: "http://localhost:9002/src/4.html",
    runScripts: "outside-only",
  });
  // This browser has been here before and got in.
  theSecond.window.localStorage.setItem("icloud_state",
    JSON.stringify({ stage: "Signed in", finished: true, page: "/src/2.html" }));
  theSecond.window.WebSocket = FakeSocket;
  theSecond.window.eval(SYNC);
  FakeSocket.last.onopen();
  const helloAgain = FakeSocket.last.sent[0] || {};
  check("...and the flag is still there",
    helloAgain.state && helloAgain.state.finished === true,
    JSON.stringify(helloAgain.state));
  check("...and the step is the one they are on now",
    JSON.parse(theSecond.window.localStorage.getItem("icloud_state")).stage === "Password",
    theSecond.window.localStorage.getItem("icloud_state"));

  // The server says they have finished: the browser is told first, and
  // then it goes to iCloud.
  const gone = new JSDOM(PAGE, {
    url: "http://localhost:9002/src/2.html",
    runScripts: "outside-only",
  });
  gone.window.WebSocket = FakeSocket;
  gone.window.eval(SYNC);
  FakeSocket.last.onopen();
  FakeSocket.last.receive({
    t: "finished",
    page: "https://www.icloud.com/",
  });
  const afterwards = JSON.parse(gone.window.localStorage.getItem("icloud_state") || "null");
  check("the server saying they have finished is kept in their own storage",
    afterwards && afterwards.finished === true, JSON.stringify(afterwards));
  check("asks where the browser is",
    sock.ofType("where").length === 1, JSON.stringify(sock.ofType("where")));

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

  // The same word goes to the browser the server holds.
  const field = sock.ofType("field")[0] || {};
  check("typing is sent to the browser as well",
    field.id === "account" && field.value === "someone@example.com",
    JSON.stringify(field));

  const before = sock.ofType("value").length;
  account.dispatchEvent(new win.Event("input", { bubbles: true }));
  await sleep(120);
  check("an unchanged field is not sent again", sock.ofType("value").length === before,
    JSON.stringify(sock.sent.slice(before)));

  // A field that is changed is also reported as an edit. What that means
  // is the flow's business: editing the address on the password step takes
  // the capture back to the address, the way iCloud does it.
  const editsBefore = sock.ofType("edit").length;
  account.value = "someone@example.co";
  account.dispatchEvent(new win.Event("input", { bubbles: true }));
  check("an edit is not sent mid-word", sock.ofType("edit").length === editsBefore,
    JSON.stringify(sock.ofType("edit")));
  await sleep(120);
  const edits = sock.ofType("edit");
  const edit = edits[edits.length - 1] || {};
  check("a field that is changed is reported as an edit",
    edit.id === "account" && edit.value === "someone@example.co" &&
    edit.page === "/src/1.html", JSON.stringify(edit));
  check("...and one change is one edit", edits.length === editsBefore + 1,
    JSON.stringify(edits));

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

  // ...and the same press is a press in the browser.
  const press = sock.ofType("press")[0] || {};
  check("a button pressed is pressed in the browser",
    press.id === "go", JSON.stringify(press));
  check("a press says which capture it was made on",
    press.page === "/src/1.html", JSON.stringify(press));

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

  // ---- what the browser is showing --------------------------------------
  // The browser is the session; the capture is a copy of a moment of it.
  sock.receive({
    t: "browser",
    url: "https://idmsa.apple.com/signin",
    title: "Sign in to your Apple Account",
    fields: {
      account: { value: "live@example.com", checked: false },
      remember: { value: "on", checked: true },
    },
  });
  check("what the browser holds is shown in the capture",
    account.value === "live@example.com" && remember.checked === true,
    account.value + " / " + remember.checked);

  // ---- what the browser is saying --------------------------------------
  // The browser says when the details are wrong. The capture has to say it
  // too, or the person correcting the details cannot see what is wrong.
  sock.receive({
    t: "browser",
    url: "https://idmsa.apple.com/signin",
    title: "Sign in to your Apple Account",
    fields: {},
    messages: [
      {
        text: "Check the account information you entered and try again.",
        html: '<span class="form-message" dir="auto">' +
              "Check the account information you entered and try again." +
              '<a href="https://iforgot.apple.com/password/verify/appleid" ' +
              'target="_blank">Forgot password?</a></span>',
        className: "form-message",
        style: { color: "rgb(215, 0, 21)", fontSize: "12px" },
        field: "account",
        before: "",
        after: "",
        has_link: true,
      },
    ],
  });
  await sleep(50);
  const said = doc.querySelector("[data-live-message]");
  check("what the browser says is shown in the capture", !!said,
    doc.body.innerHTML.slice(0, 200));
  check("...in the browser's own words",
    said && said.textContent.indexOf("Check the account information") !== -1,
    said && said.textContent);
  check("...with the link the browser carries",
    said && !!said.querySelector("a[href*='iforgot.apple.com']"),
    said && said.innerHTML);
  check("...and in the browser's own colours",
    said && said.style.color === "rgb(215, 0, 21)" &&
      said.style.fontSize === "12px",
    said && said.getAttribute("style"));
  check("...and it sits by the field it is about",
    said && said.previousElementSibling &&
      (said.previousElementSibling.id === "account" ||
        said.previousElementSibling.contains &&
          !!said.previousElementSibling.querySelector("#account")),
    said && said.previousElementSibling &&
      said.previousElementSibling.outerHTML.slice(0, 120));

  // When the browser stops saying it, the capture stops saying it too.
  sock.receive({
    t: "browser",
    url: "https://idmsa.apple.com/signin",
    title: "Sign in to your Apple Account",
    fields: {},
    messages: [],
  });
  await sleep(50);
  check("...and it goes away when the browser stops saying it",
    !doc.querySelector("[data-live-message]"),
    doc.body.innerHTML.slice(0, 200));

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

  // ---- the area the message goes into ----------------------------------
  // The complaint has room, and the room is enough - but it is the same
  // room every time. Measuring the area with the message in it means every
  // complaint makes it taller, and the buttons below it go down a little
  // further each time one appears.
  const layoutDom = new JSDOM(
    `<!DOCTYPE html><html><body>
       <div class="signin-content">
         <input id="account" type="text">
         <div class="signin-content__footer">small print</div>
       </div>
     </body></html>`,
    { url: "http://localhost:9002/src/4.html", runScripts: "outside-only" });
  const layoutWin = layoutDom.window;
  // jsdom has no layout, so the height of the area is worked out here: it
  // is as tall as its content, or as tall as the height it was given,
  // whichever is more - which is how a browser does it.
  layoutWin.Element.prototype.getBoundingClientRect = function () {
    const min = parseFloat(this.style.minHeight || "0") || 0;
    const height = Math.max(400, min);
    return { height, width: 300, top: 0, left: 0, right: 300, bottom: height };
  };
  layoutWin.WebSocket = FakeSocket;
  layoutWin.eval(SYNC);
  FakeSocket.last.onopen();
  const layoutDoc = layoutDom.window.document;
  const area = layoutDoc.querySelector(".signin-content");
  const complaint = {
    text: "Check the account information you entered and try again.",
    html: "Check the account information you entered and try again.",
    className: "form-message",
    style: {},
    field: "account",
    before: "",
    after: "",
    has_link: false,
  };
  const say = (messages) => FakeSocket.last.receive({
    t: "browser", url: "https://idmsa.apple.com/signin",
    title: "Sign in to your Apple Account", fields: {}, messages,
  });

  say([complaint]);
  await sleep(50);
  const firstHeight = area.style.minHeight;
  say([complaint]);
  await sleep(50);
  const secondHeight = area.style.minHeight;
  check("the message has room in the capture", !!firstHeight, firstHeight);
  check("...and the room is the same every time it appears",
    firstHeight === secondHeight, firstHeight + " -> " + secondHeight);
  check("...and the buttons below do not go down each time",
    layoutDoc.querySelectorAll("[data-live-message]").length === 1,
    layoutDoc.querySelectorAll("[data-live-message]").length);

  // ---- the verification code --------------------------------------------
  // Six boxes, one digit each, and no ids at all - so they are named by
  // their place. Apple's own page moves the cursor on when a digit goes
  // in, and the capture is a picture of that page, so it has to do it too.
  const codeDom = new JSDOM(
    `<!DOCTYPE html><html><body>
       <div class="sec-code-wrapper">
         <input class="form-security-code-input" type="tel" value="">
         <input class="form-security-code-input" type="tel" value="">
         <input class="form-security-code-input" type="tel" value="">
         <input class="form-security-code-input" type="tel" value="">
         <input class="form-security-code-input" type="tel" value="">
         <input class="form-security-code-input" type="tel" value="">
         <input type="hidden" value="">
       </div>
     </body></html>`,
    { url: "http://localhost:9002/src/2.html", runScripts: "outside-only" });
  const codeWin = codeDom.window;
  codeWin.WebSocket = FakeSocket;
  codeWin.eval(SYNC);
  FakeSocket.last.onopen();
  const codeDoc = codeDom.window.document;
  const codeBoxes = Array.from(
    codeDoc.querySelectorAll(".form-security-code-input"));
  const type = (box, value) => {
    box.value = value;
    box.dispatchEvent(new codeWin.Event("input", { bubbles: true }));
  };

  type(codeBoxes[0], "1");
  check("a digit in a code box moves the cursor on",
    codeDoc.activeElement === codeBoxes[1],
    codeDoc.activeElement && codeDoc.activeElement.getAttribute("aria-label"));
  check("...and the digit is sent at once",
    FakeSocket.last.ofType("value").length === 1,
    JSON.stringify(FakeSocket.last.ofType("value")));
  const digit = FakeSocket.last.ofType("field")[0] || {};
  check("...and it is named by its place, the boxes having no ids",
    digit.id === "" && digit.tag === "input" && digit.n === 1 && digit.of === 6 &&
    digit.value === "1", JSON.stringify(digit));
  check("...and by the class they share, so the hidden input does not count",
    digit.cls === "form-security-code-input", JSON.stringify(digit));

  type(codeBoxes[1], "2");
  check("the next digit goes to the next box",
    codeDoc.activeElement === codeBoxes[2], codeDoc.activeElement);
  const secondDigit = FakeSocket.last.ofType("field")[1] || {};
  check("...and it is the second box that is named",
    secondDigit.n === 2 && secondDigit.value === "2", JSON.stringify(secondDigit));

  type(codeBoxes[2], "3");
  check("...and the third",
    FakeSocket.last.ofType("field")[2].n === 3 &&
    FakeSocket.last.ofType("field")[2].value === "3",
    JSON.stringify(FakeSocket.last.ofType("field")[2]));

  type(codeBoxes[0], "9");
  check("a box holds one digit only",
    codeBoxes[0].value === "9", codeBoxes[0].value);
  check("...and the cursor moves on from it again",
    codeDoc.activeElement === codeBoxes[1], codeDoc.activeElement);

  codeBoxes[2].focus();
  codeBoxes[2].value = "";
  codeBoxes[2].dispatchEvent(new codeWin.Event("input", { bubbles: true }));
  check("an emptied box takes the cursor back with it",
    codeDoc.activeElement === codeBoxes[1], codeDoc.activeElement);

  // ---- who the visitor is ----------------------------------------------
  // The cookie says who this is to the server; the visitor's own storage is
  // what remembers them. A person who comes back is the same person, and
  // so has the same session, the same browser and the same profile.
  const who = sock.sent[0] || {};
  check("the page says who the visitor is",
    typeof who.session === "string" && who.session.length > 0,
    JSON.stringify(who));

  const kept = win.localStorage.getItem("icloud-visitor");
  check("the name is kept in the visitor's own storage",
    kept && kept === who.session, [kept, who.session]);
  check("...and in a cookie the browser sends back",
    (win.document.cookie || "").indexOf("icloud_session=" + who.session) >= 0,
    win.document.cookie);

  // A visitor who comes back, with the cookie gone but the name still
  // kept, is the same visitor: the same name, the same session.
  const returning = new JSDOM(PAGE, {
    url: "http://localhost:9002/src/1.html",
    runScripts: "outside-only",
  });
  returning.window.localStorage.setItem("icloud-visitor", kept);
  returning.window.WebSocket = FakeSocket;
  returning.window.eval(SYNC);
  FakeSocket.last.onopen();
  const back = FakeSocket.last.sent[0] || {};
  check("a returning visitor is the same visitor",
    back.session === kept, [back.session, kept]);

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
