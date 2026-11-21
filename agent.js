/* Injected into the real page by the server.
 *
 * Serialises the document once, then streams incremental DOM changes out
 * through the Playwright binding. Nothing here is specific to any site - it
 * mirrors whatever document it happens to be running in.
 *
 * Every resource is rewritten to go back through the server's /res proxy.
 * Pointing straight at the real origin mostly works for CSS and images, but
 * fails for fonts (they need CORS headers) and for anything that needs the
 * site's cookies. Routing through /res sidesteps both, and lets the server
 * rewrite url()s inside proxied stylesheets.
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

  function rewriteTree(root) {
    if (root.querySelectorAll) {
      for (const el of root.querySelectorAll("style")) {
        el.textContent = proxyCss(el.textContent);
      }
      for (const el of root.querySelectorAll("*")) rewriteElement(el);
    }
    rewriteElement(root);
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

    rewriteTree(clone);

    // Any <base> the site ships would point at the mirror's origin, so drop
    // it. Everything is absolute or proxied now, so it is not needed.
    clone.querySelectorAll("base").forEach((n) => n.remove());

    return "<!DOCTYPE html>\n" + clone.outerHTML;
  }

  // Patched-in fragments get the same treatment as the full document; there is
  // no <base> left to lean on.
  function rewriteFragment(html) {
    if (!html) return html;
    if (html.indexOf("url(") === -1 && html.indexOf("src") === -1 &&
        html.indexOf("href") === -1 && html.indexOf("style") === -1) {
      return html;
    }
    const template = document.createElement("template");
    template.innerHTML = html;
    rewriteTree(template.content);
    return template.innerHTML;
  }

  function onMutation(records) {
    for (const record of records) {
      if (record.type === "attributes") {
        const sel = path(record.target);
        if (sel === null) continue;
        const raw = record.target.getAttribute(record.attributeName);
        let value = raw;
        if (record.attributeName === "style") value = proxyCss(raw);
        else if (record.attributeName === "srcset") value = proxySrcset(raw);
        else if (
          record.attributeName === "src" ||
          record.attributeName === "href" ||
          record.attributeName === "poster" ||
          record.attributeName === "data"
        ) {
          value = toProxy(raw);
        }
        pending.push({ t: "a", sel, name: record.attributeName, value });
      } else if (record.type === "characterData") {
        const parent = record.target.parentElement;
        if (!parent) continue;
        const sel = path(parent);
        if (sel === null) continue;
        // Text inside a <style> block is CSS, not prose.
        const data =
          parent.tagName === "STYLE" ? proxyCss(record.target.data) : record.target.data;
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
      patch.push({ t: "h", sel, html: rewriteFragment(node.innerHTML) });
    }
    dirty.clear();

    if (patch.length === 0) return;

    // Send the patch however big it is. Re-seeding the whole document instead
    // looks like a flash, and a page that mutates constantly would loop on it.
    // Bandwidth is local, so a large delta is cheaper than a reload.
    emit({ kind: "mut", patch });
  }

  let lastDocSent = 0;

  function sendDocument(force) {
    // Rate-limit re-seeds. Without this a chatty page re-seeds several times a
    // second and the mirror strobes.
    const now = Date.now();
    if (!force && lastDocSent && now - lastDocSent < 2000) return;
    lastDocSent = now;

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

  // The mirror asks for this when its structure drifts out of step. Forced, so
  // a genuine desync is never swallowed by the rate limit.
  window.__rbResync = () => sendDocument(true);

  // Exposed so the URL rewriting can be exercised without a real browser.
  window.__rbInternals = {
    toAbsolute,
    toProxy,
    proxyFor,
    proxyCss,
    proxySrcset,
    rewriteElement,
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
