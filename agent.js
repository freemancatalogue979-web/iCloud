/* Injected into the real page by the server.
 *
 * Serialises the document once, then streams incremental DOM changes out
 * through the Playwright binding. Nothing here is specific to any site - it
 * mirrors whatever document it happens to be running in.
 *
 * Every resource reference is rewritten to an absolute URL before it leaves,
 * because the mirror renders from a different origin and anything left
 * relative would resolve against the mirror instead of the real site.
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

  // ------------------------------------------------------------- URLs
  function toAbsolute(value) {
    if (!value) return value;
    const trimmed = value.trim();
    if (
      trimmed.startsWith("data:") ||
      trimmed.startsWith("blob:") ||
      trimmed.startsWith("#") ||
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

  // "a.png 1x, b.png 2x" -> both resolved
  function absolutizeSrcset(value) {
    return value
      .split(",")
      .map((part) => {
        const bits = part.trim().split(/\s+/);
        if (!bits[0]) return part;
        bits[0] = toAbsolute(bits[0]);
        return bits.join(" ");
      })
      .join(", ");
  }

  // url(...) inside CSS resolves against the document, not the stylesheet,
  // so it has to be rewritten by hand.
  function absolutizeCss(css) {
    if (!css) return css;
    return css.replace(/url\(\s*(['"]?)([^'")]+)\1\s*\)/g, (match, quote, url) => {
      const absolute = toAbsolute(url);
      return absolute === url ? match : `url(${quote}${absolute}${quote})`;
    });
  }

  const URL_ATTRS = [
    ["src", toAbsolute],
    ["href", toAbsolute],
    ["poster", toAbsolute],
    ["data", toAbsolute],
    ["action", toAbsolute],
    ["srcset", absolutizeSrcset],
  ];

  function absolutizeElement(el) {
    for (const [attr, fix] of URL_ATTRS) {
      if (el.hasAttribute && el.hasAttribute(attr)) {
        el.setAttribute(attr, fix(el.getAttribute(attr)));
      }
    }
    const style = el.getAttribute && el.getAttribute("style");
    if (style) el.setAttribute("style", absolutizeCss(style));
  }

  function absolutizeTree(root) {
    for (const el of root.querySelectorAll("style")) {
      el.textContent = absolutizeCss(el.textContent);
    }
    absolutizeElement(root);
    for (const el of root.querySelectorAll("*")) absolutizeElement(el);
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

    absolutizeTree(clone);

    // Belt and braces: a <base> also catches anything injected later by a
    // patch that we did not rewrite.
    const head = clone.querySelector("head");
    if (head && !head.querySelector("base")) {
      const base = document.createElement("base");
      base.href = document.baseURI;
      head.insertBefore(base, head.firstChild);
    }

    return "<!DOCTYPE html>\n" + clone.outerHTML;
  }

  // A patched-in fragment still needs its CSS urls fixed. Element attributes
  // are covered by the <base> tag in the mirror.
  function absolutizeFragment(html) {
    if (!html || html.indexOf("url(") === -1 && html.indexOf("style") === -1) {
      return html;
    }
    const template = document.createElement("template");
    template.innerHTML = html;
    for (const el of template.content.querySelectorAll("style")) {
      el.textContent = absolutizeCss(el.textContent);
    }
    for (const el of template.content.querySelectorAll("[style]")) {
      el.setAttribute("style", absolutizeCss(el.getAttribute("style")));
    }
    return template.innerHTML;
  }

  function onMutation(records) {
    for (const record of records) {
      if (record.type === "attributes") {
        const sel = path(record.target);
        if (sel === null) continue;
        const raw = record.target.getAttribute(record.attributeName);
        let value = raw;
        if (record.attributeName === "style") value = absolutizeCss(raw);
        else if (record.attributeName === "srcset") value = absolutizeSrcset(raw);
        else if (record.attributeName === "src" || record.attributeName === "href") {
          value = toAbsolute(raw);
        }
        pending.push({ t: "a", sel, name: record.attributeName, value });
      } else if (record.type === "characterData") {
        const parent = record.target.parentElement;
        if (!parent) continue;
        const sel = path(parent);
        if (sel === null) continue;
        // Text inside a <style> block is CSS, not prose.
        const data =
          parent.tagName === "STYLE"
            ? absolutizeCss(record.target.data)
            : record.target.data;
        pending.push({ t: "t", sel, value: data });
      } else if (record.type === "childList") {
        const sel = path(record.target);
        if (sel === null) continue;
        dirty.set(sel, record.target);
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

    // Read innerHTML at flush time, not mutation time, so a subtree that
    // changed repeatedly is serialised once in its final state.
    for (const [sel, node] of dirty) {
      if (!node.isConnected) continue;
      patch.push({ t: "h", sel, html: absolutizeFragment(node.innerHTML) });
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

  // The mirror asks for this when its structure drifts out of step.
  window.__rbResync = sendDocument;

  // Exposed so the URL rewriting can be exercised without a real browser.
  window.__rbInternals = {
    toAbsolute,
    absolutizeCss,
    absolutizeSrcset,
    absolutizeElement,
  };

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

  // A full navigation replaces the document; re-seed the mirror.
  window.addEventListener("pageshow", start);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
