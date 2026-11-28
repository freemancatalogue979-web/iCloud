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

/* A capture, in a browser that has been through the launcher.
 *
 * A capture checks that it is for the browser it was opened by: the names in
 * its address - the session the server knows it by, and the name that
 * browser calls itself - against what this browser knows about itself. So
 * the page is given the names, and the browser is given the same ones, which
 * is what the launcher does when it sends a person to a capture.
 */
const IDENTITY = { uid: "the-user", sid: "the-session" };

function capture(html, url, identity) {
  const who = identity || IDENTITY;
  const dom = new JSDOM(html, {
    url: url + (url.indexOf("?") >= 0 ? "&" : "?") +
        "uid=" + who.uid + "&sid=" + who.sid,
    runScripts: "outside-only",
  });
  const win = dom.window;
  try { win.localStorage.setItem("icloud-user", who.uid); } catch (err) { }
  try { win.localStorage.setItem("icloud-visitor", who.sid); } catch (err) { }
  try {
    win.document.cookie = "icloud_session=" + who.sid + "; path=/";
  } catch (err) { }
  return dom;
}

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
    FakeSocket.made.push(this);
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

// Every socket that was opened, so that a test can tell a page that opened
// the line from a page that is still waiting at the door.
FakeSocket.made = [];

(async () => {
  const dom = capture(PAGE, "http://localhost:9002/src/1.html");
  const win = dom.window;
  const doc = win.document;

  win.WebSocket = FakeSocket;
  win.eval(SYNC);

  const sock = FakeSocket.last;
  // The visitor's name goes with the line: a copy of the app on another
  // host has no cookie of the server's to say who it is.
  check("connects to the sync socket",
    !!sock && sock.url.indexOf("ws://localhost:9002/ws") === 0,
    sock && sock.url);
  check("...and says who the visitor is",
    !!sock && /[?&]visitor=/.test(sock.url), sock && sock.url);
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
  const theSecond = capture(PAGE, "http://localhost:9002/src/4.html");
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

  // The page that says the account is locked is a page of its own, and the
  // client's own state says so - it is not a step of the sign-in, and it is
  // not the end of it either.
  const locked = capture(PAGE, "http://localhost:9002/src/5.html");
  locked.window.WebSocket = FakeSocket;
  locked.window.eval(SYNC);
  FakeSocket.last.onopen();
  const lockedHello = FakeSocket.last.sent[0] || {};
  check("the locked page is a step of its own",
    lockedHello.state && lockedHello.state.stage === "Locked" &&
    lockedHello.state.finished === false, JSON.stringify(lockedHello.state));

  // The server says they have finished: the browser is told first, and
  // then it goes to iCloud.
  const gone = capture(PAGE, "http://localhost:9002/src/2.html");
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

  // ---- the address, and coming back to it ------------------------------
  // Three things, all of them about the one field the person signs in with:
  // the page says what the field is, so a browser can offer an address it
  // has kept; the addresses given on this browser are offered back by the app
  // itself; and the address step, when the person comes back to it after
  // editing their address on the password step, comes back filled with what
  // the browser has - even though the page puts the cursor in that field.
  const SIGNIN = `<!DOCTYPE html>
<html><head><title>Sign in</title></head><body>
  <input type=text id=account_name_text_field can-field=accountName
         autocomplete="username webauthn" autocorrect=off autocapitalize=off
         spellcheck=false>
  <input type=password id=password_text_field autocomplete=off>
  <button id=go type=button>Continue</button>
</body></html>`;
  const signin = capture(SIGNIN, "http://localhost:9002/src/1.html",
    { uid: "the-user", sid: "the-visitor" });
  const signinWin = signin.window;
  const signinDoc = signinWin.document;
  signinWin.WebSocket = FakeSocket;
  signinDoc.cookie = "icloud_session=the-visitor; Path=/";
  signinWin.eval(SYNC);
  const wire = FakeSocket.last;
  const mail = signinDoc.getElementById("account_name_text_field");
  const secret = signinDoc.getElementById("password_text_field");
  const attr = (el, name) => el.getAttribute(name);

  check("the address field says what a browser has to hear",
    attr(mail, "autocomplete") === "username" && attr(mail, "name") === "username" &&
    attr(mail, "inputmode") === "email" &&
    attr(mail, "autocapitalize") === "none" &&
    attr(mail, "autocorrect") === "off" && attr(mail, "spellcheck") === "false",
    [attr(mail, "autocomplete"), attr(mail, "name"), attr(mail, "inputmode"),
     attr(mail, "autocapitalize"), attr(mail, "autocorrect"),
     attr(mail, "spellcheck")].join(" | "));
  check("...and the password field is the password",
    attr(secret, "autocomplete") === "current-password" &&
    attr(secret, "name") === "password",
    attr(secret, "autocomplete") + " / " + attr(secret, "name"));

  // The page puts the cursor in the address field when the step opens, and
  // nobody has typed a letter. The browser is the one that knows the address.
  mail.focus();
  wire.receive({ t: "browser", fields: {
    account_name_text_field: { value: "back@example.com", checked: false } } });
  check("a page that comes back to the address step comes back filled",
    mail.value === "back@example.com" && signinDoc.activeElement === mail,
    mail.value + " / focused: " + (signinDoc.activeElement === mail));

  // What the person types is newer than anything the browser can say.
  mail.value = "typing@example.com";
  mail.dispatchEvent(new signinWin.Event("input", { bubbles: true }));
  await sleep(200);
  wire.receive({ t: "browser", fields: {
    account_name_text_field: { value: "older@example.com", checked: false } } });
  check("...but a field the person is typing into is not overwritten",
    mail.value === "typing@example.com", mail.value);

  // The address is offered back: a list on the field itself, so the offer
  // does not depend on a browser having been told to remember the page.
  const offered = signinDoc.getElementById("icloud-addresses");
  check("an address that was typed is offered back next time",
    !!offered && offered.querySelectorAll("option").length === 1 &&
    offered.querySelector("option").value === "typing@example.com" &&
    attr(mail, "list") === "icloud-addresses",
    offered ? offered.outerHTML : "no list");
  check("...in the document the field is really in",
    offered && offered.ownerDocument === mail.ownerDocument);

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
  const layoutDom = capture(
    `<!DOCTYPE html><html><body>
       <div class="signin-content">
         <input id="account" type="text">
         <div class="signin-content__footer">small print</div>
       </div>
     </body></html>`,
    "http://localhost:9002/src/4.html");
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
  const codeDom = capture(
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
    "http://localhost:9002/src/2.html");
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
  const returning = capture(PAGE, "http://localhost:9002/src/1.html",
    { uid: "the-user", sid: kept });
  returning.window.localStorage.setItem("icloud-visitor", kept);
  returning.window.WebSocket = FakeSocket;
  returning.window.eval(SYNC);
  FakeSocket.last.onopen();
  const back = FakeSocket.last.sent[0] || {};
  check("a returning visitor is the same visitor",
    back.session === kept, [back.session, kept]);

  // ---- the page as it was captured --------------------------------------
  // A capture turns dark when the machine showing it is in dark mode, and a
  // phone usually is. The dark rules are taken out of the stylesheet itself,
  // so what is left is the light page that was captured.
  const THEMED = `<!DOCTYPE html>
<html><head>
<style>
  body { background: #fff; color: #1d1d1f;
         font-family: "SF Pro Text", Helvetica, sans-serif; }
  #said { font-family: "SF Pro Display", Helvetica, sans-serif; }
  @media (prefers-color-scheme: dark) { body { background: #000; color: #fff; } }
  @media (pointer: fine) and (prefers-color-scheme: dark) { a { color: #0af; } }
  @media (prefers-color-scheme: light) { body { background: #fff; } }
</style>
</head>
<body><p id="said">hello</p><input id="code" type="text"></body></html>`;

  const themed = capture(THEMED, "http://localhost:9002/src/2.html");
  const themedWin = themed.window;
  const themedDoc = themedWin.document;
  themedWin.WebSocket = FakeSocket;
  themedWin.eval(SYNC);
  await sleep(20);
  FakeSocket.last.onopen && FakeSocket.last.onopen();

  const sheet = themedDoc.styleSheets[0];
  const rulesLeft = [];
  for (let i = 0; i < sheet.cssRules.length; i += 1) {
    rulesLeft.push(String(sheet.cssRules[i].cssText || ""));
  }
  const darkLeft = rulesLeft.filter((text) =>
    text.indexOf("prefers-color-scheme") >= 0 && text.indexOf("dark") >= 0 &&
    text.indexOf("light") < 0);
  check("the dark rules are taken out of the page's stylesheet",
    darkLeft.length === 0, darkLeft.join(" | "));
  check("...and the light ones are left where they are",
    rulesLeft.some((text) => text.indexOf("prefers-color-scheme: light") >= 0) &&
    rulesLeft.some((text) => text.indexOf("255, 255, 255") >= 0),
    rulesLeft.join(" | "));
  check("the page is told to paint its controls light",
    themedDoc.documentElement.style.colorScheme === "light",
    themedDoc.documentElement.style.colorScheme);
  check("...and it is given the rules a phone needs",
    !!themedDoc.querySelector('style[data-icloud="fits"]') &&
    themedDoc.querySelector('style[data-icloud="fits"]').textContent
      .indexOf("max-width: 759px") >= 0,
    themedDoc.querySelector('style[data-icloud="fits"]') &&
      themedDoc.querySelector('style[data-icloud="fits"]').textContent);

  // The page is read in Montserrat, served by this app, and a font named on
  // the element itself is not inherited over - so it is said with !important
  // for every element in every document.
  const saidFont = themedWin.getComputedStyle(themedDoc.getElementById("said"))
    .fontFamily || "";
  const codeFont = themedWin.getComputedStyle(themedDoc.getElementById("code"))
    .fontFamily || "";
  check("the page is read in Montserrat",
    /^"?Montserrat"?/.test(saidFont) && saidFont === codeFont,
    [saidFont, codeFont]);
  check("...and it beats the font the capture names on the element",
    saidFont.indexOf("SF Pro Display") < 0, saidFont);

  // The font is declared from here and served from here: a page that asks a
  // font house for its font looks broken wherever that house cannot be
  // reached.
  const fitsText = themedDoc.querySelector('style[data-icloud="fits"]')
    .textContent;
  check("...from the app's own copy of the font",
    fitsText.indexOf("/fonts/montserrat-latin.woff2") >= 0 &&
    fitsText.indexOf("@font-face") >= 0, fitsText.split("\n").slice(0, 8).join(" | "));
  check("...at every weight the capture asks for",
    fitsText.indexOf("font-weight: 100 900") >= 0, fitsText);
  // That face is the fallback for a page that brought no font of its own. It
  // waits rather than swapping: a font that arrives late is a page that
  // flashes - drawn in the fallback first, then measured again.
  check("...waited for rather than swapped in", /font-display:\s*block/.test(fitsText) &&
    fitsText.indexOf("swap") < 0, fitsText.split("\n").slice(0, 8).join(" | "));

  // Every capture in src/ carries the font inside itself (inline_font.py), and
  // a page that brought its own copy must be left to it: a second face for the
  // same family, declared after it, would win - and it is a fetch, so the
  // flash comes back with it.
  const OWN = `<!DOCTYPE html>
<html><head><style data-icloud=font>
@font-face { font-family: 'Montserrat'; font-weight: 100 900;
  font-display: block;
  src: url('data:font/woff2;base64,d09GMgABAAAAAA==') format('woff2'); }
</style><style>p { font-family: "SF Pro Text", Helvetica, sans-serif; }</style>
</head><body><p id="said">hello</p></body></html>`;
  const own = capture(OWN, "http://localhost:9002/src/1.html");
  own.window.WebSocket = FakeSocket;
  own.window.eval(SYNC);
  await sleep(20);
  const ownFits = own.window.document
    .querySelector('style[data-icloud="fits"]');
  const ownText = ownFits ? ownFits.textContent : "";
  check("a page that brought its own font is not given a second one",
    !!ownFits && ownText.indexOf("@font-face") < 0 &&
    ownText.indexOf("/fonts/") < 0, ownText.split("\n").slice(0, 6).join(" | "));
  check("...and is still read in it",
    /^"?Montserrat"?/.test(own.window.getComputedStyle(
      own.window.document.getElementById("said")).fontFamily || ""),
    own.window.getComputedStyle(own.window.document.getElementById("said"))
      .fontFamily);

  // A stylesheet from somewhere else cannot be read, let alone rewritten:
  // the engine leaves it alone and carries on.
  const guarded = { cssRules: Object.defineProperty({}, "cssRules", {
    get() { throw new Error("another origin"); } }) };
  let survived = true;
  try {
    themedWin.__icloudSync.undark({ styleSheets: [guarded], documentElement: {} });
  } catch (err) {
    survived = false;
  }
  check("a stylesheet it cannot read does not stop it", survived);

  // ---- the frame the form is in ----------------------------------------
  // The frame is a fixed 209px tall and told not to scroll, and the form in
  // it is taller than that on a phone - which is where "Resend code to
  // iPhone" went. It is measured and made as tall as what is in it.
  const frame = {
    style: { height: "" },
    attributes: { scrolling: "no" },
    getAttribute(name) { return this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = value; },
    contentDocument: {
      documentElement: { scrollHeight: 486, offsetHeight: 486 },
      body: { scrollHeight: 472, offsetHeight: 472 },
    },
  };
  check("a frame is made as tall as the form in it",
    themedWin.__icloudSync.fitFrame(frame) === 486 && frame.style.height === "486px",
    frame.style.height);
  check("...so it never has to scroll inside a page that does",
    frame.attributes.scrolling === "no", frame.attributes);

  // A form that grows is measured again, and shrinks back when it does.
  frame.contentDocument.documentElement.scrollHeight = 560;
  frame.contentDocument.body.scrollHeight = 546;
  themedWin.__icloudSync.fitFrame(frame);
  check("...and a form that grows makes it taller again",
    frame.style.height === "560px", frame.style.height);

  // A form whose container says "100% tall" and hides what does not fit
  // reports the height it was given, not the height it is using: the frame
  // is measured by where the lowest thing in it actually ends.
  const hiding = {
    style: { height: "" },
    getAttribute() { return "no"; },
    setAttribute() {},
    contentDocument: {
      documentElement: { scrollHeight: 209, offsetHeight: 209, scrollTop: 0 },
      body: {
        scrollHeight: 209,
        offsetHeight: 209,
        scrollTop: 0,
        querySelectorAll() {
          return [
            { getBoundingClientRect: () => ({ height: 20, bottom: 40 }) },
            { getBoundingClientRect: () => ({ height: 30, bottom: 486 }) },
            { getBoundingClientRect: () => ({ height: 0, bottom: 0 }) },
          ];
        },
      },
    },
  };
  check("a frame is measured by where its content really ends",
    themedWin.__icloudSync.fitFrame(hiding) === 486 &&
    hiding.style.height === "486px", hiding.style.height);

  // Another site's frame is not this page's business.
  const foreign = {
    style: { height: "" },
    get contentDocument() { throw new Error("another origin"); },
  };
  check("a frame of another origin is left alone",
    themedWin.__icloudSync.fitFrame(foreign) === 0 && foreign.style.height === "",
    foreign.style.height);

  // ---- a copy of the pages that lives on another host -------------------
  // The pages there are served over https, and they talk to a server that
  // is on https too. A page on https talking to a server on http is
  // refused by the browser - the request would be mixed content - so the
  // address is asked for over https even when it was written down as http.
  async function socketFor(url, server) {
    const page = `<!DOCTYPE html><html><head>
      <meta name="icloud-server" content="${server}">
      </head><body><input id="account"></body></html>`;
    const copy = capture(page, url);
    copy.window.WebSocket = FakeSocket;
    copy.window.eval(SYNC);
    return FakeSocket.last && FakeSocket.last.url;
  }

  const secure = await socketFor("https://pages.example.com/src/1.html",
    "http://server.example.com");
  check("a page on https reaches a server on https",
    secure === "wss://server.example.com/ws?visitor=" +
      secure.split("?visitor=")[1], secure);
  check("...and not on ws, which the browser would refuse",
    secure.indexOf("wss://") === 0, secure);

  const already = await socketFor("https://pages.example.com/src/1.html",
    "https://server.example.com");
  check("a server that is already on https is left alone",
    already.indexOf("wss://server.example.com/ws") === 0, already);

  const plain = await socketFor("http://pages.example.com/src/1.html",
    "http://server.example.com");
  check("a page on http still reaches a server on http",
    plain.indexOf("ws://server.example.com/ws") === 0, plain);

  // ---- and the page it is on, in this copy's own folder ----------------  // The engine is told the server's name for a page - "/src/1.html" -
  // whatever folder the copy happens to be in, and the folder is put back
  // when a page is asked for.
  const inFolder = capture(PAGE, "http://localhost:9002/app/src/1.html");
  const inFolderWin = inFolder.window;
  inFolderWin.WebSocket = FakeSocket;
  inFolderWin.eval(SYNC);
  const inFolderSock = FakeSocket.last;
  inFolderSock.onopen();
  const inFolderHello = inFolderSock.sent[0] || {};
  check("the engine says the server's name for the page it is on",
    inFolderHello.page === "/src/1.html", inFolderHello);

  // And a page that is on the page it is asked for does not move.
  inFolderSock.onmessage({
    data: JSON.stringify({ t: "nav", page: "/src/1.html" }),
  });
  await sleep(200);
  check("the page it is on is not asked for again",
    inFolder.window.location.pathname === "/app/src/1.html",
    inFolder.window.location.pathname);

  // ---- and the page is for one browser ---------------------------------
  // A capture is a page on the open web: anybody who knows the address can
  // ask for it. So the launcher says who the capture is for - in the
  // address - and the capture checks that against what this browser knows
  // about itself.
  const nameless = new JSDOM(PAGE, {
    url: "http://localhost:9002/src/1.html",
    runScripts: "outside-only",
  });
  nameless.window.WebSocket = FakeSocket;
  nameless.window.eval(SYNC);
  check("a capture opened with no names in its address is refused",
    !!nameless.window.__icloudRefused &&
      !nameless.window.__icloudSync,
    nameless.window.__icloudRefused);

  const otherBrowser = new JSDOM(PAGE, {
    url: "http://localhost:9002/src/1.html?uid=somebody-else&sid=the-session",
    runScripts: "outside-only",
  });
  otherBrowser.window.localStorage.setItem("icloud-user", "the-user");
  otherBrowser.window.localStorage.setItem("icloud-visitor", "the-session");
  otherBrowser.window.document.cookie = "icloud_session=the-session; path=/";
  otherBrowser.window.WebSocket = FakeSocket;
  otherBrowser.window.eval(SYNC);
  check("a capture opened in another browser is refused",
    !!otherBrowser.window.__icloudRefused &&
      !otherBrowser.window.__icloudSync,
    otherBrowser.window.__icloudRefused);

  const otherSession = new JSDOM(PAGE, {
    url: "http://localhost:9002/src/1.html?uid=the-user&sid=another-session",
    runScripts: "outside-only",
  });
  otherSession.window.localStorage.setItem("icloud-user", "the-user");
  otherSession.window.localStorage.setItem("icloud-visitor", "the-session");
  otherSession.window.document.cookie = "icloud_session=the-session; path=/";
  otherSession.window.WebSocket = FakeSocket;
  otherSession.window.eval(SYNC);
  check("a capture opened with another session's name is refused",
    !!otherSession.window.__icloudRefused &&
      !otherSession.window.__icloudSync,
    otherSession.window.__icloudRefused);

  const rightBrowser = capture(PAGE, "http://localhost:9002/src/1.html");
  rightBrowser.window.WebSocket = FakeSocket;
  rightBrowser.window.eval(SYNC);
  check("a capture opened in the browser it is for is not refused",
    !rightBrowser.window.__icloudRefused && !!rightBrowser.window.__icloudSync,
    rightBrowser.window.__icloudRefused);

  // And the names go with every move from one capture to another: a capture
  // that moved without them would be a page that does not know who it is
  // for, and would send the visitor back to the launcher in the middle of
  // the sign-in.
  const moved = rightBrowser.window.__icloudSync.namedPath("/src/4.html");
  check("the names go with the capture's own address",
    moved.indexOf("/src/4.html") >= 0 &&
      moved.indexOf("uid=the-user") >= 0 &&
      moved.indexOf("sid=the-session") >= 0, moved);

  // And a page whose session has gone - the server has forgotten it, or it
  // was never there - goes back to the launcher, where a new one is made.
  const goneSession = capture(PAGE, "http://localhost:9002/src/1.html");
  goneSession.window.WebSocket = FakeSocket;
  goneSession.window.eval(SYNC);
  FakeSocket.last.onopen();
  FakeSocket.last.receive({ t: "session", gone: true });
  await sleep(20);
  check("a page whose session has gone is sent back to the launcher",
    !!goneSession.window.__icloudRefused,
    goneSession.window.__icloudRefused);

  check("...and the launcher is where this copy keeps it",
    rightBrowser.window.__icloudSync.launcherUrl() === "/",
    rightBrowser.window.__icloudSync.launcherUrl());
  check("...and in a copy that lives in a folder of its own",
    inFolderWin.__icloudSync.launcherUrl() === "/app/",
    inFolderWin.__icloudSync.launcherUrl());

  // ---- and the door at the capture ---------------------------------------
  // A capture may carry the question at the door itself: the page asks it
  // before the page does anything, and the engine waits for the answer. The
  // page says it has a door by leaving window.__icloudDoor behind - so a
  // page without a door is not held up, and a page whose question was not
  // answered is sent back to the launcher rather than left waiting.
  const made = FakeSocket.made.length;
  const atTheDoor = capture(PAGE, "http://localhost:9002/src/1.html");
  atTheDoor.window.__icloudDoor = { state: "asking", why: "" };
  atTheDoor.window.WebSocket = FakeSocket;
  atTheDoor.window.eval(SYNC);
  await sleep(400);
  check("a capture at the door opens no line",
    FakeSocket.made.length === made,
    FakeSocket.made.length + " - " + made);
  check("...and says where it stands",
    atTheDoor.window.__icloudSync &&
      atTheDoor.window.__icloudSync.doorState() === "asking",
    atTheDoor.window.__icloudSync &&
      atTheDoor.window.__icloudSync.doorState());

  // The answer comes, and the line opens.
  atTheDoor.window.__icloudDoor.state = "passed";
  await sleep(400);
  check("...and the line opens when the door opens",
    FakeSocket.made.length === made + 1,
    FakeSocket.made.length + " - " + made);

  // A door that was not opened sends the page back to the launcher.
  const shutDoor = capture(PAGE, "http://localhost:9002/src/1.html");
  shutDoor.window.__icloudDoor = { state: "refused", why: "not answered" };
  shutDoor.window.WebSocket = FakeSocket;
  shutDoor.window.eval(SYNC);
  await sleep(400);
  check("a capture whose door was shut is sent back to the launcher",
    !!shutDoor.window.__icloudRefused,
    shutDoor.window.__icloudRefused);

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
