/* mirror.js - the client half of the mirror.
 *
 * The page this script runs in IS the captured page. The server writes the
 * capture to 1.html, the client loads that file, and this script keeps it in
 * step with the real browser.
 *
 * Two jobs, and only two:
 *
 *   - SHOW THE TRUTH. When the real page changes, the server sends a fresh
 *     capture and this script rebuilds the page from it. The old design
 *     patched a live DOM with changes addressed to node ids. A patch has to
 *     land on exactly the right node, and a patch that lands on the wrong
 *     one corrupts the page quietly instead of failing. A capture cannot be
 *     addressed wrongly - it is the page.
 *
 *   - CARRY INPUT BACK. Clicks and keystrokes are forwarded to the real
 *     browser by position, so the browser does what the user asked. Nothing
 *     here knows what page is loaded: no selectors, no site-specific code.
 *
 * The script is injected into the captured document by the server, so it is
 * loaded before the captured page's own body is parsed. It waits for the
 * document to be ready before it does anything.
 */
(() => {
  if (window.__rbMirror) return;

  /* The engine's own elements are marked so that rebuilding the page never
   * removes the script that is doing the rebuilding. */
  const ENGINE_ID = "rb-engine";
  const STYLE_ID = "rb-style";
  /* Everything that came from a capture is marked, so a later capture can
   * take exactly that away and leave the rest alone. */
  const CAPTURED = "data-rb-captured";
  const WIN_ID = "data-rb-frame";
  const NID = "data-rb-nid";
  const CANVAS_ATTR = "data-rb-canvas";

  /* The size the page was captured at. The mirror lays the page out at this
   * size and the client scales it to fit, so a click in the mirror is at the
   * same place on the page as it is on the real thing. */
  let captured = { w: 0, h: 0 };
  let lastCapture = null;
  let ready = false;

  /* windowId -> { el, doc } for the frames under the top one. */
  const frameDocs = new Map();

  function post(path, body) {
    fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(() => {});
  }

  function tell(message) {
    try {
      window.parent.postMessage(message, "*");
    } catch (e) {
      /* no parent to tell */
    }
  }

  // --------------------------------------------------------------- frames
  /* The captured page's frame elements carry the id their content arrived
   * under. The content is put back with srcdoc, which keeps it same-origin
   * with us, so a click inside it can be measured and forwarded like any
   * other click. */
  function parentDocOf(windowId) {
    const cut = windowId.lastIndexOf(".");
    const parentId = cut === -1 ? "0" : windowId.slice(0, cut);
    if (parentId === "0") return document;
    const entry = frameDocs.get(parentId);
    return entry ? entry.doc : null;
  }

  function frameElementOf(windowId) {
    const parentDoc = parentDocOf(windowId);
    if (!parentDoc) return null;
    try {
      return parentDoc.querySelector("[" + WIN_ID + "=\"" + windowId + "\"]");
    } catch (e) {
      return null;
    }
  }

  function attachFrames(frames) {
    for (const frame of frames) {
      const id = frame.windowId;
      if (!id || id === "0") continue;
      const el = frameElementOf(id);
      if (!el) continue;
      const known = frameDocs.get(id);
      if (known && known.el === el && known.html === frame.html) continue;
      el.setAttribute("sandbox", "allow-same-origin");
      el.onload = () => {
        const doc = el.contentDocument;
        if (!doc) return;
        frameDocs.set(id, { el: el, doc: doc, html: frame.html });
        prepareCanvases(doc);
        wireInput(doc);
        // A frame's own frames go in after it has loaded.
        const mine = (frames || []).filter((f) => f.windowId && f.windowId.startsWith(id + "."));
        if (mine.length) attachFrames(mine);
      };
      el.srcdoc = frame.html;
    }
  }

  // ------------------------------------------------------------- rebuild
  /* Where the caret was, so a rebuild does not throw it away. The agent
   * gives every element an id that is stable for as long as the element
   * lives, so the same field is found again in the next capture. */
  function rememberFocus() {
    const el = document.activeElement;
    if (!el || el === document.body) return null;
    const key = el.getAttribute && el.getAttribute(NID);
    if (!key) return null;
    const out = { key: key, start: null, end: null };
    try {
      out.start = el.selectionStart;
      out.end = el.selectionEnd;
    } catch (e) {
      /* not a text control */
    }
    return out;
  }

  function restoreFocus(saved) {
    if (!saved) return;
    const el = document.querySelector("[" + NID + "=\"" + saved.key + "\"]");
    if (!el) return;
    try {
      el.focus({ preventScroll: true });
      if (saved.start !== null && saved.start !== undefined) {
        el.setSelectionRange(saved.start, saved.end);
      }
    } catch (e) {
      /* the element cannot take the focus - nothing to do */
    }
  }

  function clearCaptured(root) {
    for (const el of Array.from(root.querySelectorAll("[" + CAPTURED + "]"))) {
      el.remove();
    }
  }

  /* The captured page, in the two parts the mirror needs.
   *
   * The agent hands the head and the body over separately, because they
   * have to go back into the document the way a parser puts them there. A
   * declarative shadow root - <template shadowrootmode="open"> - only becomes
   * a shadow root when a parser sees it, and innerHTML does not let a parser
   * see one. */
  function partsOf(frame) {
    const out = {
      head: frame.headHtml || "",
      body: frame.bodyHtml || "",
      htmlAttrs: frame.htmlAttrs || {},
      bodyAttrs: frame.bodyAttrs || {},
    };
    if (frame.headHtml !== undefined && frame.bodyHtml !== undefined) return out;
    // An older capture has only the whole document. Cut the parts out of it.
    const head = /<head[^>]*>([\s\S]*?)<\/head>/i.exec(frame.html || "");
    const body = /<body[^>]*>([\s\S]*?)<\/body>/i.exec(frame.html || "");
    out.head = head ? head[1] : "";
    out.body = body ? body[1] : "";
    return out;
  }

  function swapHead(headHtml) {
    clearCaptured(document.head);
    if (!headHtml) return;
    // Parsed, not imported: a stylesheet or a meta tag has to be a real
    // element of the document, and the page's own rules have to apply to
    // the page the mirror is showing.
    const holder = document.createElement("div");
    holder.innerHTML = headHtml;
    while (holder.firstChild) {
      const el = holder.firstChild;
      holder.removeChild(el);
      if (el.nodeType === 1) {
        if (el.tagName === "SCRIPT" || el.tagName === "BASE") continue;
        el.setAttribute(CAPTURED, "");
      }
      document.head.appendChild(el);
    }
  }

  /* A canvas keeps its picture in pixels, so a captured <canvas> is an empty
   * box. The capture ships the picture as a data URL and the mirror puts the
   * pixels back. The attribute is taken off once it has been used, so a later
   * scan only finds canvases that have not been painted yet.
   *
   * Chromium also hides a canvas's intrinsic size in a document that is not
   * allowed to run scripts, which leaves an inline canvas with no box at all
   * - the specified width is ignored, because without an intrinsic size it is
   * not a replaced element any more. Making it an inline-block gives it a
   * box, and the capture's width and height then apply. */
  function prepareCanvases(root) {
    if (!root || !root.querySelectorAll) return;
    for (const canvas of root.querySelectorAll("canvas")) {
      const url = canvas.getAttribute(CANVAS_ATTR);
      if (url) {
        canvas.removeAttribute(CANVAS_ATTR);
        const img = new Image();
        img.onload = () => {
          try {
            const ctx = canvas.getContext("2d");
            if (!ctx) return;
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.drawImage(img, 0, 0);
          } catch (e) { /* nothing to paint with */ }
        };
        img.onerror = () => {};
        img.src = url;
      }
      const view = canvas.ownerDocument && canvas.ownerDocument.defaultView;
      if (!view) continue;
      let display = "";
      try { display = view.getComputedStyle(canvas).display; } catch (e) { continue; }
      if (display === "inline") canvas.style.display = "inline-block";
    }
  }

  function swapBody(bodyHtml) {
    const saved = rememberFocus();
    /* setHTMLUnsafe parses the markup with a parser that knows about
     * declarative shadow DOM, so a web component keeps its shadow root.
     * innerHTML does not, so it is only the fallback. */
    if (typeof document.body.setHTMLUnsafe === "function") {
      document.body.setHTMLUnsafe(bodyHtml || "");
    } else {
      document.body.innerHTML = bodyHtml || "";
    }
    prepareCanvases(document.body);
    restoreFocus(saved);
  }

  /* Mark what the file was served with, so the first rebuild takes exactly
   * that away. */
  function markInitial() {
    for (const el of Array.from(document.head.children)) {
      if (el.id === ENGINE_ID || el.id === STYLE_ID) continue;
      el.setAttribute(CAPTURED, "");
    }
    for (const el of Array.from(document.body.children)) {
      el.setAttribute(CAPTURED, "");
    }
  }

  function render(capture) {
    if (!capture || !capture.frames || !capture.frames.length) return false;
    const top = capture.frames.find((f) => f.windowId === "0") || capture.frames[0];
    if (!top || !top.html) return false;

    const parts = partsOf(top);
    // The page's own class on <html> is how a site themes itself.
    for (const name of ["class", "lang", "dir"]) {
      const value = parts.htmlAttrs[name];
      if (value) document.documentElement.setAttribute(name, value);
      else document.documentElement.removeAttribute(name);
    }
    for (const name of ["class", "style"]) {
      const value = parts.bodyAttrs[name];
      if (value) document.body.setAttribute(name, value);
      else document.body.removeAttribute(name);
    }
    swapHead(parts.head);
    swapBody(parts.body);
    attachFrames(capture.frames);

    if (capture.viewport && capture.viewport.w) {
      captured = { w: capture.viewport.w, h: capture.viewport.h };
    }
    lastCapture = capture;
    tell({ rb: "size", w: captured.w, h: captured.h });

    if (!ready) {
      ready = true;
      // The client shows a spinner until the mirror says it has something.
      tell({ rb: "seeded" });
    }
    return true;
  }

  // --------------------------------------------------------------- input
  /* Where the pointer was, in the captured page's own coordinates.
   *
   * A click inside a frame is reported by that frame, so its coordinates are
   * relative to the frame. Each frame's own offset is added as the frames are
   * climbed out of, which gives the position in the page - the only
   * coordinates the server can use. */
  function pointInPage(ev) {
    let x = ev.clientX;
    let y = ev.clientY;
    let win = ev.target && ev.target.ownerDocument
      ? ev.target.ownerDocument.defaultView
      : null;
    while (win && win.frameElement) {
      const r = win.frameElement.getBoundingClientRect();
      x += r.left;
      y += r.top;
      win = win.parent;
    }
    return { x: x, y: y };
  }

  const NAMED = new Set(["Enter", "Tab", "Backspace", "Delete", "Escape",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End",
    "PageUp", "PageDown"]);

  function isField(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  }

  function wireInput(doc) {
    if (!doc || doc.__rbWired) return;
    doc.__rbWired = true;

    doc.addEventListener("mousedown", (ev) => {
      const pt = pointInPage(ev);
      const w = captured.w || window.innerWidth;
      const h = captured.h || window.innerHeight;
      // A fraction of the page, not pixels: the server is driving a window
      // of whatever size, and a fraction lands in the same place whatever
      // that size is.
      post("/click", { x: pt.x / w, y: pt.y / h });
      // A field keeps the focus so the caret shows where the user is
      // typing. Everything else does not, so that a click cannot start a
      // drag or a text selection.
      if (!isField(ev.target)) ev.preventDefault();
    }, true);

    doc.addEventListener("keydown", (ev) => {
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      if (NAMED.has(ev.key)) {
        post("/key", { key: ev.key });
        ev.preventDefault();
      } else if (ev.key.length === 1) {
        post("/keys", { text: ev.key });
        // The mirror never types into itself. The real browser types, and
        // what it has typed comes back as the truth.
        ev.preventDefault();
      }
    }, true);

    // A link in the mirror would navigate the mirror away from the mirror.
    doc.addEventListener("click", (ev) => {
      const el = ev.target;
      if (el && el.closest && el.closest("a[href]")) ev.preventDefault();
    }, true);
  }

  /* The one thing that is not a change to the page: a form control's value.
   * Typing changes .value, which is not a DOM change, so it never causes a
   * capture, and it is far too frequent to want one. It arrives as a small
   * message of its own and is put straight onto the element - the element is
   * found by the id the agent gave it, which is the same id it carries in the
   * capture, so it is the same field and not a field that looks like it. */
  function applyValue(message) {
    if (!message || !message.id) return;
    const doc = message.windowId === "0"
      ? document
      : (frameDocs.get(message.windowId) || {}).doc;
    if (!doc) return;
    const el = doc.querySelector("[" + NID + "=\"" + message.id + "\"]");
    if (!el) return;
    if (message.value !== undefined && el.value !== message.value) {
      el.value = message.value;
    }
    if (message.checked !== undefined && el.checked !== message.checked) {
      el.checked = message.checked;
    }
  }

  // -------------------------------------------------------------- stream
  function connect() {
    if (typeof WebSocket !== "function") return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let ws = null;
    try {
      ws = new WebSocket(proto + "//" + location.host + "/ws");
    } catch (e) {
      setTimeout(connect, 800);
      return;
    }
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.kind === "doc") render(msg);
      else if (msg.kind === "value") applyValue(msg);
    };
    ws.onclose = () => setTimeout(connect, 800);
  }

  function start() {
    markInitial();
    wireInput(document);
    // The capture that is already on the server is the first thing to show;
    // the stream keeps it up to date from then on.
    if (typeof fetch === "function") {
      fetch("/capture", { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .then((capture) => { if (capture) render(capture); })
        .catch(() => {});
    }
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }

  /* Exposed so a test can drive the same code the browser runs. */
  window.__rbMirror = {
    render: render,
    applyValue: applyValue,
    wireInput: wireInput,
    start: start,
    metrics: () => ({ w: captured.w, h: captured.h }),
  };

  /* Seed the mirror with a capture, the way the old view page did. It is
   * the same call the stream makes, so a test drives the shipped path. */
  window.seed = function (frames, viewport) {
    return render({ kind: "doc", frames: frames || [], viewport: viewport || null });
  };

  /* The size the page was captured at. The scale is applied by whoever is
   * showing the page - the client, which fits it to the window. */
  window.mirrorMetrics = function () {
    return { scale: 1, w: captured.w, h: captured.h };
  };
})();
