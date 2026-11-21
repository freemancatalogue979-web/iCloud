/* Injected into the real page by the server.
 *
 * Serialises the document once, then streams incremental DOM changes out
 * through the Playwright binding. Nothing here is specific to any site - it
 * mirrors whatever document it happens to be running in.
 */
(() => {
  if (window.__rbInstalled) return;
  window.__rbInstalled = true;

  const DEBOUNCE_MS = 30;

  let observer = null;
  let timer = null;
  let pending = [];
  // childList patches are coalesced per parent, so a subtree that changes 200
  // times in a burst produces one entry, not 200.
  const dirty = new Map();

  function emit(message) {
    try {
      window.__rbEmit(message);
    } catch (e) {
      /* binding not ready yet */
    }
  }

  // Index path from <body>. Unambiguous, and cheap to resolve on the mirror.
  function path(node) {
    const parts = [];
    let n = node;
    while (n && n !== document.body) {
      const parent = n.parentElement;
      if (!parent) return null;
      parts.unshift(Array.prototype.indexOf.call(parent.children, n));
      n = parent;
    }
    return parts.join("/");
  }

  function serialize() {
    const clone = document.documentElement.cloneNode(true);
    // Scripts must not run twice - once here, once in the mirror.
    clone.querySelectorAll("script").forEach((n) => n.remove());
    return "<!DOCTYPE html>\n" + clone.outerHTML;
  }

  function onMutation(records) {
    for (const record of records) {
      if (record.type === "attributes") {
        const sel = path(record.target);
        if (sel === null) continue;
        pending.push({
          t: "a",
          sel,
          name: record.attributeName,
          value: record.target.getAttribute(record.attributeName),
        });
      } else if (record.type === "characterData") {
        const parent = record.target.parentElement;
        if (!parent) continue;
        const sel = path(parent);
        if (sel === null) continue;
        pending.push({ t: "t", sel, value: record.target.data });
      } else if (record.type === "childList") {
        const sel = path(record.target);
        if (sel === null) continue;
        // Store only the newest HTML for this subtree.
        dirty.set(sel, record.target.innerHTML);
      }
    }
    schedule();
  }

  function schedule() {
    if (timer !== null) return;
    timer = setTimeout(flush, DEBOUNCE_MS);
  }

  function flush() {
    timer = null;

    const patch = pending;
    pending = [];

    for (const [sel, html] of dirty) {
      patch.push({ t: "h", sel, html });
    }
    dirty.clear();

    if (patch.length === 0) return;

    // If the patch got big, a fresh document is cheaper than the deltas.
    const size = JSON.stringify(patch).length;
    if (size > 400_000) {
      sendDocument();
      return;
    }

    emit({ kind: "mut", patch });
  }

  function sendDocument() {
    pending = [];
    dirty.clear();
    emit({
      kind: "doc",
      html: serialize(),
      url: location.href,
      title: document.title,
      width: window.innerWidth,
      height: window.innerHeight,
    });
  }

  function start() {
    if (observer) observer.disconnect();
    sendDocument();
    observer = new MutationObserver(onMutation);
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
  }

  // The mirror asks for this when its structure drifts out of step.
  window.__rbResync = sendDocument;

  // A full navigation replaces the document; re-seed the mirror.
  window.addEventListener("pageshow", start);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
