/* Injected into every frame of the real page by the server.
 *
 * Modelled on how the SingleFile extension captures a page
 * (github.com/gildas-lormeau/singlefile, single-file-core
 * processors/frame-tree/content/content-frame-tree.js):
 *
 *   1. The top frame walks its document and gives every frame element a
 *      positional id: parent's id + "." + its index. SingleFile calls these
 *      windowIds and mints them the same way ("0", "0.1", "0.1.0").
 *   2. It postMessages an init request into each frame's contentWindow. This
 *      works cross-origin - which is the whole point, because the sign-in form
 *      lives in an iframe on idmsa.apple.com that the parent cannot read.
 *   3. Each frame serialises itself and replies to event.source. The parent
 *      matches a reply back to its element by the id it already wrote on it.
 *      No URL matching, no guessing.
 *   4. Replies that never come are given a timeout, then the frame is skipped.
 *
 * Two things SingleFile does that matter and that a plain outerHTML dump misses:
 *
 *   - Shadow roots. <ui-button> and friends are web components whose visible
 *     markup lives in a shadow root, which outerHTML does not contain. They are
 *     serialised as declarative <template shadowrootmode="open">, which Chromium
 *     parses into a live shadow root with no scripting in the mirror.
 *   - <base> removal. SingleFile's cleanupPage() strips every <base>, because a
 *     copied page has a different base URI than the original.
 *
 * After the capture, each frame keeps its own MutationObserver running and
 * streams deltas tagged with its windowId.
 *
 * Resource URLs are the one place this diverges from SingleFile: it inlines
 * everything as data: URIs because it produces a file. This produces a live
 * stream, so URLs are routed through the server's /res proxy instead, which
 * also carries the site's cookies and avoids re-encoding megabytes on every
 * resync.
 */
(() => {
  if (window.__rbInstalled) return;
  window.__rbInstalled = true;

  const DEBOUNCE_MS = 30;
  const FRAME_REPLY_TIMEOUT = 1200;
  const SHADOW_SCAN_MS = 500;
  const RESEED_MIN_GAP = 2000;
  const FRAME_SELECTOR = 'iframe, frame, object[type="text/html"][data]';
  const WIN_ID = "data-rb-frame";
  const RB = "__rb_ft_";
  const INIT_REQUEST = RB + "initRequest";
  const INIT_RESPONSE = RB + "initResponse";

  const TOP = window === window.top;

  let observer = null;
  let timer = null;
  let pending = [];
  // childList patches are coalesced per parent, so a subtree that changes 200
  // times in a burst produces one entry, not 200.
  const dirty = new Map();
  const observedRoots = new Set();
  // Patches that happened before our parent told us our windowId.
  let windowId = TOP ? "0" : null;
  let held = [];
  let lastDocSent = 0;
  let capturing = false;

  function emit(message) {
    try {
      window.__rbEmit(message);
    } catch (e) {
      /* binding not ready yet */
    }
  }

  // --------------------------------------------------------------- URLs
  function toAbsolute(value) {
    if (!value) return value;
    const trimmed = value.trim();
    if (
      trimmed.startsWith("data:") ||
      trimmed.startsWith("blob:") ||
      trimmed.startsWith("#") ||
      trimmed.startsWith("javascript:") ||
      /^[a-z][a-z0-9+.-]*:/i.test(trimmed)
    ) {
      return value;
    }
    try {
      return new URL(trimmed, document.baseURI).href;
    } catch (e) {
      return value;
    }
  }

  function proxyFor(absolute) {
    return "/res?url=" + encodeURIComponent(absolute);
  }

  // Resolve, then route through the proxy. Non-http references are left alone.
  function toProxy(value) {
    const absolute = toAbsolute(value);
    if (!/^https?:\/\//i.test(absolute)) return absolute;
    return proxyFor(absolute);
  }

  // "a.png 1x, b.png 2x" -> both proxied
  function proxySrcset(value) {
    return value
      .split(",")
      .map((part) => {
        const bits = part.trim().split(/\s+/);
        if (!bits[0]) return part;
        bits[0] = toProxy(bits[0]);
        return bits.join(" ");
      })
      .join(", ");
  }

  // url() inside CSS resolves against the document, not the stylesheet, so it
  // has to be rewritten by hand.
  function proxyCss(css) {
    if (!css) return css;
    return css.replace(/url\(\s*(['"]?)([^'")]+)\1\s*\)/g, (match, quote, url) => {
      const rewritten = toProxy(url);
      return rewritten === url ? match : `url(${quote}${rewritten}${quote})`;
    });
  }

  const URL_ATTRS = [
    ["src", toProxy],
    ["href", toProxy],
    ["poster", toProxy],
    ["data", toProxy],
    ["action", toProxy],
    ["srcset", proxySrcset],
  ];

  function rewriteElement(el) {
    if (!el.getAttribute) return;
    for (const [attr, fix] of URL_ATTRS) {
      if (el.hasAttribute(attr)) el.setAttribute(attr, fix(el.getAttribute(attr)));
    }
    const style = el.getAttribute("style");
    if (style) el.setAttribute("style", proxyCss(style));
  }

  // -------------------------------------------------------- serialising
  /* The live page is never modified. cloneNode(true) gives a structurally
   * identical tree, so the original and the clone can be walked in lockstep by
   * child index, and all edits land on the clone. The browser then does the
   * serialising, which means attribute escaping and void elements are right by
   * construction rather than by hand. */
  function cleanClone(clone) {
    const tag = clone.tagName;
    if (tag === "SCRIPT") return false; // nothing runs in the mirror
    if (tag === "BASE") return false;   // the mirror's base is not the site's
    if (tag === "STYLE") {
      // read from the live node, written to the clone, further down
    } else {
      for (const attr of Array.from(clone.attributes || [])) {
        if (/^on/i.test(attr.name)) clone.removeAttribute(attr.name);
      }
    }
    rewriteElement(clone);
    return true;
  }

  function walkChildren(origParent, cloneParent, ids) {
    // Snapshot both. childNodes is live, so removing a node from the clone
    // would shift every later index and desync the two walks.
    const origKids = Array.from(origParent.childNodes);
    const cloneKids = Array.from(cloneParent.childNodes);
    for (let i = 0; i < origKids.length; i++) {
      const orig = origKids[i];
      const clone = cloneKids[i];
      if (!clone) break;
      if (orig.nodeType === 3) continue;
      if (orig.nodeType !== 1) {
        clone.remove();
        continue;
      }
      if (!cleanClone(clone)) {
        clone.remove();
        continue;
      }
      walk(orig, clone, ids);
    }
  }

  function walk(orig, clone, ids) {
    if (clone.tagName === "STYLE") {
      clone.textContent = proxyCss(orig.textContent);
    }

    const root = orig.shadowRoot;
    if (root) {
      const doc = clone.ownerDocument;
      const template = doc.createElement("template");
      template.setAttribute("shadowrootmode", "open");
      // A ShadowRoot itself is not clonable - cloneNode on one throws
      // NotSupportedError - so clone its children into the template instead.
      const origShadowKids = Array.from(root.childNodes);
      for (const kid of origShadowKids) template.content.appendChild(kid.cloneNode(true));
      const cloneShadowKids = Array.from(template.content.childNodes);
      for (let i = 0; i < cloneShadowKids.length; i++) {
        const orig2 = origShadowKids[i];
        const clone2 = cloneShadowKids[i];
        if (!orig2 || !clone2) continue;
        if (orig2.nodeType === 3) continue; // text is content, keep it
        if (orig2.nodeType !== 1) { clone2.remove(); continue; }
        if (!cleanClone(clone2)) { clone2.remove(); continue; }
        walk(orig2, clone2, ids);
      }
      // Light DOM is not rendered once a shadow root exists.
      while (clone.firstChild) clone.firstChild.remove();
      clone.appendChild(template);
      return;
    }

    if (clone.matches && clone.matches(FRAME_SELECTOR)) {
      const assigned = ids.get(orig);
      if (assigned) {
        clone.setAttribute(WIN_ID, assigned);
        // The mirror fills this in with the frame's own captured document.
        clone.removeAttribute("src");
        clone.removeAttribute("data");
        clone.setAttribute("sandbox", "allow-same-origin");
      }
      return;
    }

    walkChildren(orig, clone, ids);
  }

  function serialiseDocument() {
    const clone = document.documentElement.cloneNode(true);
    walk(document.documentElement, clone, collectFrames());
    return "<!DOCTYPE html>" + clone.outerHTML;
  }

  // Frame elements found by the last collectFrames(), with their windowIds.
  const frameEls = [];

  /* Depth-first over the document and every shadow root, in the same order a
   * querySelectorAll would visit them. Returns a map of frame element -> the
   * windowId minted for it, and fills frameEls. */
  function collectFrames() {
    const ids = new Map();
    frameEls.length = 0;
    let counter = 0;
    const seen = new Set();

    function visit(root) {
      if (!root || seen.has(root)) return;
      seen.add(root);
      const elements = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (const el of elements) {
        if (el.matches(FRAME_SELECTOR)) {
          const id = windowId + "." + counter++;
          ids.set(el, id);
          frameEls.push({ el, id });
        }
        const sr = el.shadowRoot;
        if (sr) visit(sr);
      }
    }

    visit(document);
    return ids;
  }

  // ---------------------------------------------------------- frame tree
  function requestFrame(entry) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (frames) => {
        if (settled) return;
        settled = true;
        clearTimeout(guard);
        resolve(frames);
      };

      const onMessage = (event) => {
        const data = event.data;
        if (!data || data.rb !== INIT_RESPONSE || data.windowId !== entry.id) return;
        window.removeEventListener("message", onMessage);
        finish(data.frames || []);
      };
      window.addEventListener("message", onMessage);

      const guard = setTimeout(() => {
        window.removeEventListener("message", onMessage);
        // A frame that never answers is unreachable. Read it directly if the
        // same-origin policy happens to allow it, otherwise give up on it.
        try {
          const doc = entry.el.contentDocument;
          if (doc && doc.documentElement) finish([{ windowId: entry.id, html: doc.documentElement.outerHTML }]);
          else finish([]);
        } catch (e) {
          finish([]);
        }
      }, FRAME_REPLY_TIMEOUT);

      try {
        entry.el.contentWindow.postMessage(
          { rb: INIT_REQUEST, windowId: entry.id },
          "*"
        );
      } catch (e) {
        finish([]);
      }
    });
  }

  async function capture() {
    const ids = collectFrames();
    const frames = [{ windowId, html: serialiseDocumentWith(ids) }];
    const replies = await Promise.all(frameEls.map(requestFrame));
    for (const list of replies) {
      for (const frame of list) {
        if (frame && frame.windowId && frame.html) frames.push(frame);
      }
    }
    return frames;
  }

  function serialiseDocumentWith(ids) {
    const clone = document.documentElement.cloneNode(true);
    walk(document.documentElement, clone, ids);
    return "<!DOCTYPE html>" + clone.outerHTML;
  }

  // A child frame answers its parent, recursively, with its own subtree.
  async function replyTo(source, id) {
    windowId = id;
    const frames = await capture();
    try {
      source.postMessage({ rb: INIT_RESPONSE, windowId: id, frames }, "*");
    } catch (e) {
      /* parent went away */
    }
    // Flush anything that mutated before we knew who we were.
    const backlog = held;
    held = [];
    for (const patch of backlog) emit({ kind: "mut", windowId, patch });
  }

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || data.rb !== INIT_REQUEST || !event.source) return;
    event.stopPropagation();
    replyTo(event.source, data.windowId);
  });

  // --------------------------------------------------------- mutation
  function indexAmongElements(parent, node) {
    if (parent.children) return Array.prototype.indexOf.call(parent.children, node);
    let i = 0;
    for (const kid of parent.childNodes) {
      if (kid.nodeType === 1) {
        if (kid === node) return i;
        i++;
      }
    }
    return -1;
  }

  // Index path from <body>. "#" steps into a shadow root, so a path can cross
  // into a web component and back out.
  function path(node) {
    const parts = [];
    let n = node;
    let guard = 0;
    // parentNode, not parentElement: a shadow root's children have no
    // parentElement at all, only the shadow root as their parentNode.
    while (n && n !== document.body && guard++ < 500) {
      const parent = n.parentNode;
      if (!parent) break;
      if (parent.nodeType === 11) {
        // Record the position inside the shadow root before stepping out to its
        // host, or the path names the host and nothing more.
        parts.unshift(indexAmongElements(parent, n));
        parts.unshift("#");
        n = parent.host;
        continue;
      }
      parts.unshift(indexAmongElements(parent, n));
      n = parent;
    }
    return parts.join("/");
  }

  function flush() {
    timer = null;
    const patch = pending;
    pending = [];

    const seen = new Set();
    for (const [node, kind] of dirty) {
      dirty.delete(node);
      if (!node.isConnected) continue;
      const sel = path(node);
      const key = sel + "|" + kind;
      if (seen.has(key)) continue;
      seen.add(key);

      if (kind === "childList") {
        patch.push({ t: "h", sel, html: fragmentFor(node) });
      } else {
        patch.push({ t: "a", sel, name: kind, value: node.getAttribute(kind) });
      }
    }
    dirty.clear();
    scanShadowRoots();

    if (patch.length === 0) return;

    // Sent at any size. Re-seeding the whole document instead looks like a
    // flash, and a page that mutates constantly would loop on it.
    if (!windowId) {
      for (const p of patch) held.push([p]);
      return;
    }
    emit({ kind: "mut", windowId, patch });
  }

  // Re-reads a subtree at flush time, so a burst of changes costs one copy
  // instead of one per change.
  function fragmentFor(node) {
    const template = document.createElement("template");
    const ids = collectFrames();
    const originals = Array.from(node.childNodes);
    for (const kid of originals) template.content.appendChild(kid.cloneNode(true));
    const clones = Array.from(template.content.childNodes);
    for (let i = 0; i < clones.length; i++) {
      const orig = originals[i];
      const cl = clones[i];
      if (!orig || !cl) continue;
      if (orig.nodeType === 3) continue; // text is content, keep it
      if (orig.nodeType !== 1) { cl.remove(); continue; }
      if (!cleanClone(cl)) { cl.remove(); continue; }
      walk(orig, cl, ids);
    }
    return template.innerHTML;
  }

  function onMutate(records) {
    for (const record of records) {
      if (record.type === "childList") {
        if (!dirty.has(record.target)) dirty.set(record.target, "childList");
      } else if (record.type === "attributes") {
        dirty.set(record.target, record.attributeName);
      } else if (record.type === "characterData") {
        const parent = record.target.parentElement;
        if (parent && !dirty.has(parent)) dirty.set(parent, "childList");
      }
    }
    if (!timer) timer = setTimeout(flush, DEBOUNCE_MS);
  }

  const OBSERVE = { childList: true, attributes: true, characterData: true, subtree: true };

  function observeRoot(root) {
    if (!root || observedRoots.has(root)) return;
    observedRoots.add(root);
    observer.observe(root, OBSERVE);
  }

  // Shadow roots attach themselves without any DOM mutation to observe, so
  // they are found by scanning - but only the subtrees that actually changed,
  // plus a slow sweep for ones that appear out of nowhere.
  let lastScan = 0;
  function scanShadowRoots() {
    const now = Date.now();
    const full = now - lastScan > SHADOW_SCAN_MS;
    if (!full) return;
    lastScan = now;
    for (const el of document.querySelectorAll("*")) {
      if (el.shadowRoot) observeRoot(el.shadowRoot);
    }
  }

  async function sendDocument(force) {
    // Rate-limit re-seeds: without this a chatty page strobes the mirror.
    const now = Date.now();
    if (!force && lastDocSent && now - lastDocSent < RESEED_MIN_GAP) return;
    lastDocSent = now;
    if (capturing) return;
    capturing = true;
    pending = [];
    dirty.clear();
    try {
      const frames = await capture();
      // A frame that does not know its windowId yet has no way to be addressed
      // by the mirror. Its parent will ask it again once ids are assigned, so
      // there is nothing useful to send now.
      if (windowId && TOP) emit({ kind: "doc", windowId: "0", frames });
    } catch (e) {
      /* never break the page we are mirroring */
    }
    capturing = false;
  }

  // The mirror asks for this when its structure drifts out of step. Forced, so
  // a genuine desync is never swallowed by the rate limit.
  window.__rbResync = () => sendDocument(true);

  function start() {
    if (observer) return; // started once, however many ways we get triggered
    observer = new MutationObserver(onMutate);
    observeRoot(document);
    observeRoot(document.body);
    scanShadowRoots();
    sendDocument(true);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }

  window.__rbInternals = {
    toProxy,
    proxyCss,
    proxySrcset,
    path,
    collectFrames,
    serialiseDocument,
    fragmentFor,
    WIN_ID,
    FRAME_SELECTOR,
  };
})();
