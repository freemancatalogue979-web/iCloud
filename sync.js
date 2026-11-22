/* Runs inside every captured page served from /src.
 *
 * The page is a static capture, so there is no real browser behind it to type
 * into. What this does instead is make the page shared: what you type, where
 * you click and which page you move to go to the server, and the server hands
 * them to every other viewer - and to the next viewer that loads, so what has
 * already been done is still there.
 *
 * Deliberately narrow:
 *
 *  - A click is relayed as a marker, not replayed. Replaying it in another tab
 *    would press the same button twice.
 *  - Programmatic changes are applied under `applying`, and setting `.value`
 *    does not fire `input`, so nothing echoes back and forth.
 *  - The document is otherwise left alone: this is the captured page, and the
 *    only things added are form state and a click marker.
 */
(function () {
  "use strict";

  if (window.__icloudSync) return;

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

  // ------------------------------------------------------------------
  // Element identity
  //
  // The server has to be able to name an element in a page it has not parsed.
  // A selector built from ids and positions is stable across the identical
  // copy of the same captured file, which is all that is needed.
  // ------------------------------------------------------------------

  function escapeId(id) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(id);
    }
    return id.replace(/([^\w-])/g, "\\$1");
  }

  function keyFor(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return "#" + escapeId(el.id);

    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
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

  // ------------------------------------------------------------------
  // Sending
  // ------------------------------------------------------------------

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

  function connect() {
    var scheme = location.protocol === "https:" ? "wss:" : "ws:";
    var url = scheme + "//" + location.host + "/ws";

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
        page: location.pathname,
        from: document.referrer,
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

  function stateOf(el) {
    var tag = el.tagName;
    if (tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") return null;
    var type = (el.type || "").toLowerCase();
    return {
      key: keyFor(el),
      value: "value" in el ? String(el.value) : "",
      checked: type === "checkbox" || type === "radio" ? !!el.checked : false,
      immediate: type === "checkbox" || type === "radio" || tag === "SELECT",
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
  }

  function onFieldEvent(event) {
    if (applying) return;

    var el = event.target;
    if (!el || el.nodeType !== 1) return;

    var now = stateOf(el);
    if (!now) return;

    clearTimeout(timers.get(el));

    if (now.immediate) {
      // One event, one answer.
      emit(el);
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

    var el = null;
    try {
      el = document.querySelector(entry.key);
    } catch (err) {
      return;   // a selector the browser will not take is simply not ours
    }
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
      if (url.pathname.indexOf("/src/") !== 0) return "";
      if (!/\.html?$/i.test(url.pathname)) return "";
      return url.pathname;
    } catch (err) {
      return "";
    }
  }

  function beacon(page) {
    try {
      if (!navigator.sendBeacon || typeof Blob !== "function") return;
      navigator.sendBeacon(
        "/nav",
        new Blob([JSON.stringify({ page: page })], { type: "application/json" })
      );
    } catch (err) {
      /* the WebSocket message already carried it */
    }
  }

  function onClick(event) {
    var target = event.target;
    if (!target || target.nodeType !== 1) return;

    var link = target.closest ? target.closest("a[href]") : null;
    if (link) {
      var href = link.getAttribute("href") || "";
      var next = pageFor(href);
      if (next && next !== location.pathname) {
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
  }

  // ------------------------------------------------------------------
  // Receiving
  // ------------------------------------------------------------------

  function handle(message) {
    if (!message || typeof message.t !== "string") return;

    if (message.t === "state") {
      if (message.page && message.page !== location.pathname) {
        // The server is the truth about which page is on screen.
        leaving = true;
        location.replace(message.page);
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

    if (message.t === "nav") {
      if (leaving || !message.page || message.page === location.pathname) return;
      leaving = true;
      location.replace(message.page);
      return;
    }

    if (message.t === "value") {
      apply(message);
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
    var pending = early;
    early = [];
    for (var i = 0; i < pending.length; i++) handle(pending[i]);
  }

  function onSubmit(event) {
    // There is no backend behind a captured form: submitting it would replace
    // the page with the app's 404 and end the shared session. The page's own
    // handlers still run - this only cancels the navigation.
    event.preventDefault();
  }

  document.addEventListener("input", onFieldEvent, true);
  document.addEventListener("change", onFieldEvent, true);
  document.addEventListener("click", onClick, true);
  document.addEventListener("submit", onSubmit, true);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }

  connect();

  // One handle, so the page can be inspected from the console without
  // reaching into a closure.
  window.__icloudSync = { keyFor: keyFor, apply: apply };
})();
