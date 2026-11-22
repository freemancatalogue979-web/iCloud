/* Exercises map.js - the bridge between the capture in the frame and the real
 * page in the browser window - in jsdom.
 *
 * map.js never touches the browser: it is handed a document and a function
 * that posts one message. That is exactly what is stubbed here, so the suite
 * sees the messages the server would receive.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const MAP = fs.readFileSync(path.join(__dirname, "..", "map.js"), "utf8");

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

// A capture, attached to a postbox. `attach` is the shipped one.
function capture(html) {
  const dom = new JSDOM(`<!DOCTYPE html><html><body>${html}</body></html>`, {
    url: "http://localhost:9002/src/1.html",
    runScripts: "dangerously",
  });
  dom.window.eval(MAP);

  const posts = [];
  const post = (message) => posts.push(message);
  const doc = dom.window.document;
  const detach = dom.window.attachMapping(doc, post);

  return { dom, win: dom.window, doc, posts, post, detach };
}

function fire(win, el, type, extra) {
  const event = new win.Event(type, { bubbles: true, cancelable: true, ...extra });
  el.dispatchEvent(event);
  return event;
}

(async () => {
  // ---- typing ----------------------------------------------------------
  const typed = capture('<input id="account_name_text_field">');
  const field = typed.doc.getElementById("account_name_text_field");
  field.value = "someone@example.com";
  fire(typed.win, field, "input");

  check("nothing is sent while the typing is still going on", typed.posts.length === 0,
    JSON.stringify(typed.posts));

  await sleep(200);
  check("a pause in the typing sends the field and its value",
    typed.posts.length === 1 &&
      typed.posts[0].field === "account_name_text_field" &&
      typed.posts[0].value === "someone@example.com",
    JSON.stringify(typed.posts));

  // Three keystrokes inside the debounce are one message.
  typed.posts.length = 0;
  for (const value of ["a", "ab", "abc"]) {
    field.value = value;
    fire(typed.win, field, "input");
  }
  await sleep(200);
  check("a burst of keystrokes is one message, with the last value",
    typed.posts.length === 1 && typed.posts[0].value === "abc",
    JSON.stringify(typed.posts));

  typed.posts.length = 0;
  field.value = "x".repeat(12000);
  fire(typed.win, field, "input");
  await sleep(200);
  check("an enormous value is cut down before it is sent",
    typed.posts.length === 1 && typed.posts[0].value.length === 10000,
    typed.posts[0] && typed.posts[0].value.length);

  // ---- what is not a value --------------------------------------------
  const check1 = capture('<input id="remember" type="checkbox">');
  const box = check1.doc.getElementById("remember");
  box.checked = true;
  fire(check1.win, box, "input");
  fire(check1.win, box, "change");
  await sleep(200);
  check("a checkbox is not sent as a value", check1.posts.length === 0,
    JSON.stringify(check1.posts));

  const anon = capture('<input type="text">');
  anon.doc.querySelector("input").value = "no id";
  fire(anon.win, anon.doc.querySelector("input"), "input");
  await sleep(200);
  check("a field with no id has nothing to map onto", anon.posts.length === 0,
    JSON.stringify(anon.posts));

  const choices = capture('<select id="country"><option>a</option></select>');
  const select = choices.doc.getElementById("country");
  select.value = "a";
  fire(choices.win, select, "change");
  await sleep(200);
  check("a choice is sent when it changes",
    choices.posts.length === 1 && choices.posts[0].field === "country",
    JSON.stringify(choices.posts));

  // ---- clicking --------------------------------------------------------
  const buttons = capture(
    '<form><button id="sign-in"><span>Continue</span></button></form>' +
      '<button id="swp">Sign in with iPhone</button>'
  );
  const signIn = buttons.doc.getElementById("sign-in");
  fire(buttons.win, signIn.querySelector("span"), "click");
  await sleep(200);
  check("a click on the label is a click on the button",
    buttons.posts.length === 1 &&
      buttons.posts[0].field === "sign-in" &&
      buttons.posts[0].click === true,
    JSON.stringify(buttons.posts));

  buttons.posts.length = 0;
  fire(buttons.win, buttons.doc.getElementById("swp"), "click");
  await sleep(200);
  check("another button maps to itself",
    buttons.posts.length === 1 && buttons.posts[0].field === "swp",
    JSON.stringify(buttons.posts));

  const quiet = capture('<div id="root"><p id="copy">hello</p></div>');
  fire(quiet.win, quiet.doc.getElementById("copy"), "click");
  await sleep(200);
  check("a click on something that is not a control is not sent",
    quiet.posts.length === 0, JSON.stringify(quiet.posts));

  const links = capture('<a id="help" href="#help">Need help?</a>');
  const link = links.doc.getElementById("help");
  const event = fire(links.win, link, "click");
  await sleep(200);
  check("a link does not navigate the capture away from itself",
    event.defaultPrevented, "not prevented");
  check("and it still maps its click",
    links.posts.length === 1 && links.posts[0].field === "help",
    JSON.stringify(links.posts));

  const submitting = capture('<form id="f"><button id="go">Go</button></form>');
  const submit = fire(submitting.win, submitting.doc.getElementById("f"), "submit");
  check("a form submit is stopped", submit.defaultPrevented, "not prevented");

  // ---- attaching -------------------------------------------------------
  const twice = capture('<input id="account_name_text_field">');
  twice.win.attachMapping(twice.doc, twice.post);
  const twiceField = twice.doc.getElementById("account_name_text_field");
  twiceField.value = "once";
  fire(twice.win, twiceField, "input");
  await sleep(200);
  check("attaching twice still sends one message per change",
    twice.posts.length === 1, JSON.stringify(twice.posts));

  const leaving = capture('<input id="account_name_text_field">');
  leaving.detach();
  const gone = leaving.doc.getElementById("account_name_text_field");
  gone.value = "after detach";
  fire(leaving.win, gone, "input");
  await sleep(200);
  check("nothing is sent after detaching", leaving.posts.length === 0,
    JSON.stringify(leaving.posts));

  // ---- the form inside a frame -----------------------------------------
  // A SingleFile export keeps the sign-in form in an <iframe srcdoc="...">, so
  // the document handed over is not the one with the fields: the frame inside
  // it has to be bound too.
  const nested = capture('<iframe id="aid-auth-widget" srcdoc="<body></body>"></iframe>');
  const inner = nested.doc.getElementById("aid-auth-widget").contentDocument;
  check("a frame inside the capture is bound as well",
    !!inner && inner.__mapped === true, inner && inner.__mapped);

  nested.detach();
  check("detaching lets the frames go too",
    !inner || inner.__mapped === false, inner && inner.__mapped);

  const nothing = capture("");
  check("a document that is not there yet is not an error",
    typeof nothing.win.attachMapping(null, nothing.post) === "function");

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("FAIL " + (err && err.stack ? err.stack : err));
  console.log(`\n${passed} passed, ${failed + 1} failed`);
  process.exit(1);
});
