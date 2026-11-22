/* Injected into every frame of the real page by the server.
 *
 * Capture is modelled on the SingleFile extension, and the mutation stream on
 * PhantomStream (github.com/fullselfbrowsing/PhantomStream). The two ideas that
 * matter, taken from PhantomStream's src/capture/index.js:
 *
 *   - Every node gets a STABLE ID held in a WeakMap, so identity survives no
 *     matter how much the tree around it moves. PhantomStream ships ids in a
 *     `nodeIds` sidecar array paralleling the HTML; here they are written onto
 *     the serialised CLONE as data-rb-nid attributes instead, because a sidecar
 *     has to line up with the order the mirror's parser happens to produce
 *     elements and a <template shadowrootmode> is consumed by Chromium but not
 *     by browsers without declarative shadow DOM - one extra element silently
 *     shifts every later id. The clone is thrown away, so the live page is
 *     still never marked up.
 *   - Mutations are granular ops - add / rm / attr / text / value - addressed by
 *     those ids, not "replace this subtree's innerHTML". Subtree replacement is
 *     both enormous (one class toggle re-sends a whole menu of inline SVGs) and
 *     lossy: it destroys scroll position, focus, transitions, and every nested
 *     frame inside the replaced range.
 *
 * Index paths were tried first and are wrong for this. Insert one node at the
 * start of a container and every sibling's path shifts, so later patches land
 * on the wrong element.
 *
 * Form state is a side channel. Typing into an <input> changes `.value`, which
 * is not a DOM mutation - MutationObserver never fires for it. PhantomStream
 * listens for input/change in the capture phase and emits a `value` op; without
 * that, a sign-in form mirrors as permanently empty.
 *
 * From SingleFile: cross-origin frames are captured by postMessaging each frame
 * and letting it serialise itself, and shadow roots are serialised so web
 * components are not hollow. <base> is removed.
 *
 * Resource URLs go through the server's /res proxy rather than being inlined as
 * data: URIs, because this is a live stream rather than a saved file.
 */
(() => {
  if (window.__rbInstalled) return;
  window.__rbInstalled = true;

  const FRAME_REPLY_TIMEOUT = 1200;
  // A full sweep for shadow roots that appear with no DOM mutation to hang off.
  // Expensive, so rare; the per-flush scan of newly added subtrees does the
  // rest, and that one costs only what actually changed.
  const SHADOW_FULL_SCAN_MS = 5000;
  const RESEED_MIN_GAP = 2000;
  const FRAME_SELECTOR = 'iframe, frame, object[type="text/html"][data]';
  const WIN_ID = "data-rb-frame";
  const NID = "data-rb-nid";
  /* The computed-style snapshot is delivered as a stylesheet of zero
   * specificity rules rather than as inline style attributes. Inline styles
   * beat every stylesheet rule, so pinning them froze the mirror: the live
   * page would toggle a class, the mirror would take the attribute op, and
   * the snapshot's colour and size would still win - the page would look
   * frozen. A :where() rule has specificity zero, so the page's own CSS
   * decides and the snapshot only fills in what the mirror could not load. */
  const FALLBACK_ATTR = "data-rb-fallback";
  /* A shadow root is not an element, so it cannot carry an id of its own -
   * but a mutation inside one still has to be delivered, and it has to be
   * delivered to the right place. The root's address is derived from the
   * host's, so both sides can work it out from the ids already in the
   * document without a side channel. */
  const SHADOW_SUFFIX = "/shadow";
  let nextOwnId = 1;
  const RB = "__rb_ft_";
  const INIT_REQUEST = RB + "initRequest";
  const INIT_RESPONSE = RB + "initResponse";

  const TOP = window === window.top;

  let windowId = TOP ? "0" : null;
  let held = [];
  let lastDocSent = 0;
  let capturing = false;

  // --------------------------------------------------------- node identity
  // WeakMap so ids die with their nodes; the mirror holds the other end.
  const elementToId = new WeakMap();
  let nextId = 1;

  function ensureId(node) {
    if (!node || node.nodeType !== 1) return null;
    let id = elementToId.get(node);
    if (!id) {
      id = "n" + nextId++;
      elementToId.set(node, id);
    }
    return id;
  }

  /* Our own inserted elements - the snapshot stylesheet - get ids from a
   * separate series so they can never collide with the page's, and so the
   * mirror can still address every element in its document. */
  function ownId() {
    return "rbf" + nextOwnId++;
  }

  // The address of the shadow root attached to a host element.
  function shadowIdOf(host) {
    if (!host) return null;
    const nid = elementToId.get(host);
    return nid ? nid + SHADOW_SUFFIX : null;
  }

  // The address of anything a mutation can be reported against.
  function addressOf(node) {
    if (!node) return null;
    if (node.nodeType === 1) return ensureId(node);
    // A shadow root. Its host is an element and has an id of its own.
    if (node.nodeType === 11 && node.host) return shadowIdOf(node.host);
    return null;
  }

  function emit(message) {
    try {
      window.__rbEmit(message);
    } catch (e) {
      /* binding not ready yet */
    }
  }

  function send(message) {
    if (windowId) emit(message);
    else held.push(message);
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

  /* ------------------------------------------------- computed styles
   * The mirror's styling otherwise depends entirely on /res fetching the
   * site's stylesheets, which fails silently the moment a sheet is
   * CORS-blocked, session-scoped, or redirected - and the page renders as bare
   * markup. Inlining resolved styles means the mirror looks right whether or
   * not any of that arrives.
   *
   * Curated list, taken from PhantomStream: iterating all 300+ computed
   * properties per element is what crushed them on heavy pages. */
  const STYLE_DEFAULTS = {
    display: "block", position: "static", opacity: "1", visibility: "visible",
    overflow: "visible", transform: "none", "box-shadow": "none", "z-index": "auto",
    float: "none", clear: "none", cursor: "auto", "pointer-events": "auto",
    "text-align": "start", "vertical-align": "baseline", "font-style": "normal",
    "font-variant": "normal", "text-transform": "none", "white-space": "normal",
    "word-break": "normal", "overflow-wrap": "normal", "list-style-type": "disc",
    "border-collapse": "separate", resize: "none",
  };

  const CURATED_PROPS = [
    "display", "position", "top", "right", "bottom", "left", "float", "clear",
    "box-sizing", "width", "height", "min-width", "min-height", "max-width",
    "max-height", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding-top", "padding-right", "padding-bottom", "padding-left",
    "flex-direction", "flex-wrap", "justify-content", "align-items", "align-self",
    "align-content", "flex-grow", "flex-shrink", "flex-basis", "order", "gap",
    "grid-template-columns", "grid-template-rows", "grid-column", "grid-row",
    "grid-auto-flow", "grid-gap", "column-gap", "row-gap",
    "background-color", "background-image", "background-position",
    "background-size", "background-repeat", "color", "opacity", "visibility",
    "border-top-width", "border-right-width", "border-bottom-width",
    "border-left-width", "border-top-style", "border-right-style",
    "border-bottom-style", "border-left-style", "border-top-color",
    "border-right-color", "border-bottom-color", "border-left-color",
    "border-radius", "box-shadow",
    // outline needs its longhands too: the shorthand alone can serialise with
    // a visible width and colour while outline-style:none is what suppresses
    // it, which would box every element on the page.
    "outline", "outline-style", "outline-width", "outline-color",
    "font-family", "font-size", "font-weight", "font-style", "line-height",
    "letter-spacing", "word-spacing", "text-align", "text-decoration",
    "text-transform", "text-indent", "text-overflow", "white-space",
    "overflow", "overflow-x", "overflow-y", "transform", "transform-origin",
    "z-index", "cursor", "pointer-events", "vertical-align", "object-fit",
  ];

  // Inherited properties: if the parent already carries the same value, the
  // child gets it for free in the mirror too, so repeating it is pure payload.
  const INHERITED_PROPS = new Set([
    "color", "font-family", "font-size", "font-weight", "font-style",
    "line-height", "letter-spacing", "word-spacing", "text-align",
    "text-transform", "text-indent", "white-space", "word-break",
    "overflow-wrap", "visibility", "cursor", "list-style-type",
  ]);

  const NO_OPS = new Set([
    "0px", "normal", "none", "auto", "0s", "0px 0px", "rgba(0, 0, 0, 0)",
    "transparent", "0% 0%", "repeat", "visible", "start", "baseline", "clip",
    "fill", "row", "nowrap", "content-box", "separate", "50% 50%", "match-parent",
    "static", "currentColor", "currentcolor none",
  ]);

  function computedOf(el) {
    if (!el || !window.getComputedStyle) return null;
    try {
      return window.getComputedStyle(el);
    } catch (e) {
      return null;
    }
  }

  function propValue(computed, prop) {
    if (!computed) return "";
    try {
      return computed.getPropertyValue(prop);
    } catch (e) {
      return "";
    }
  }

  // Properties whose initial value depends on the element or its box, so a
  // single probe element would give a misleading answer for them. Everything
  // else in CURATED_PROPS has one element-independent initial value, which is
  // what makes reading the defaults off a probe safe.
  const PROBE_UNSAFE = new Set(["display", "width", "height"]);

  // Colours that default to currentColor, so their resolved value simply
  // repeats the element's own colour.
  const COLOR_CURRENT_PROPS = new Set([
    "outline-color", "border-top-color", "border-right-color",
    "border-bottom-color", "border-left-color",
  ]);

  // The browser knows its own defaults; a hand-written list does not, and
  // getting one wrong is invisible - the element simply carries a declaration
  // that says nothing. flex-grow:0, flex-shrink:1, order:0, outline-width:3px
  // and border-*-color were all leaking onto every element on the page because
  // none of them appear in a list written against jsdom, whose
  // getComputedStyle does not report them at all.
  let defaultStyle = null;

  function styleDefaults(doc) {
    if (defaultStyle) return defaultStyle;
    defaultStyle = {};
    const probe = doc.createElement("div");
    // Hidden so it cannot perturb the layout of the page being captured.
    probe.setAttribute("style", "display:none !important");
    (doc.body || doc.documentElement).appendChild(probe);
    const computed = computedOf(probe);
    if (computed) {
      for (const prop of CURATED_PROPS) {
        if (!PROBE_UNSAFE.has(prop)) defaultStyle[prop] = propValue(computed, prop);
      }
    }
    probe.remove();
    return defaultStyle;
  }

  function computedStyleText(el) {
    const computed = computedOf(el);
    if (!computed) return "";
    const inheritedFrom = computedOf(el.parentElement);
    const defaults = styleDefaults(el.ownerDocument || document);
    /* A flex or grid item's width and height are worked out by the layout,
     * not by the element. Pinning the number the layout produced turns it
     * into the item's base size, and the mirror then lays the row out
     * differently from the page - one item narrower, everything after it
     * shifted. The layout is left to do the same job again. */
    const parentDisplay = propValue(inheritedFrom, "display");
    const isFlexItem = parentDisplay === "flex" || parentDisplay === "inline-flex" ||
      parentDisplay === "grid" || parentDisplay === "inline-grid";
    // A border or outline colour left at currentColor resolves to the
    // element's own colour, which the mirror would reproduce anyway.
    const ownColor = propValue(computed, "color");
    const outlineStyle = propValue(computed, "outline-style");
    const hasTransform = !NO_OPS.has(propValue(computed, "transform"));
    const out = [];
    for (const prop of CURATED_PROPS) {
      const value = propValue(computed, prop);
      if (!value) continue;
      if (STYLE_DEFAULTS[prop] === value) continue;
      // Equal to what the browser would have produced with nothing set.
      if (defaults[prop] === value) continue;
      // A resolved transform-origin is an absolute offset derived from the
      // element's box. Carrying it freezes the mirror to the original layout
      // and breaks the transform outright if the box differs by a pixel, so it
      // only travels with a transform that actually uses it.
      if (prop === "transform-origin" && !hasTransform) continue;
      if (isFlexItem && (prop === "width" || prop === "height")) continue;
      // outline-style:none is the initial value; the longhands that go with it
      // (outline-width:3px, outline-color) only add bytes.
      if (outlineStyle === "none" && prop.indexOf("outline") === 0) continue;
      if (COLOR_CURRENT_PROPS.has(prop) && value === ownColor) continue;
      // Skip the values that mean "nothing was set" - auto, none, normal, 0px.
      // Without this an element carries ~1.3KB of initial values that say
      // nothing, which is the whole payload. A property whose real default IS
      // one of these is still skipped above by STYLE_DEFAULTS.
      if (NO_OPS.has(value)) continue;
      if (INHERITED_PROPS.has(prop) && propValue(inheritedFrom, prop) === value) continue;
      out.push(prop + ":" + value);
    }
    return out.join(";");
  }

  const URL_ATTRS = [
    ["src", toProxy],
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
    // href is two different things. On <link> it names a resource, and routing
    // it through the proxy is what makes the site's stylesheet load with its
    // cookies. On <a> it is navigation - proxying it turns every link into a
    // fetch of raw HTML, so those are only resolved to an absolute URL.
    if (el.hasAttribute("href")) {
      const href = el.getAttribute("href");
      el.setAttribute("href", el.tagName === "LINK" ? toProxy(href) : toAbsolute(href));
    }
    const style = el.getAttribute("style");
    if (style) el.setAttribute("style", proxyCss(style));
  }

  // -------------------------------------------------------- serialising
  /* The live page is never modified. cloneNode(true) gives a structurally
   * identical tree, so the original and the clone are walked in lockstep by
   * child index and all edits land on the clone. Ids are recorded in pre-order,
   * which is the order the serialiser emits elements and the order the mirror's
   * querySelectorAll returns them - that agreement is what makes the sidecar
   * work without touching the live DOM. */
  // Cap on the size of a canvas snapshot. A big canvas would otherwise
  // dominate the whole capture as base64.
  const CANVAS_MAX_BYTES = 262144;
  const CANVAS_ATTR = "data-rb-canvas";

  function canvasPixels(el) {
    try {
      if (!el || typeof el.toDataURL !== "function") return null;
      const url = el.toDataURL();
      if (!url || url === "data:,") return null; // nothing was drawn
      if (url.length > CANVAS_MAX_BYTES) return null;
      return url;
    } catch (e) {
      // SecurityError on a tainted canvas. Nothing to carry.
      return null;
    }
  }

  // Copy the live form state onto the clone so a fresh parse of the captured
  // markup starts out showing what the real page shows.
  function carryLiveState(orig, clone) {
    const tag = orig.tagName;
    if (tag === "INPUT") {
      const type = (orig.getAttribute("type") || "text").toLowerCase();
      if (type === "checkbox" || type === "radio") {
        if (orig.checked !== orig.hasAttribute("checked")) {
          if (orig.checked) clone.setAttribute("checked", "");
          else clone.removeAttribute("checked");
        }
        return;
      }
      if (type === "file" || type === "submit" || type === "button") return;
      const value = orig.value;
      if (typeof value === "string" && value !== orig.getAttribute("value")) {
        clone.setAttribute("value", value);
      }
      return;
    }
    if (tag === "TEXTAREA") {
      const value = orig.value;
      if (typeof value === "string" && value !== orig.textContent) {
        clone.textContent = value;
      }
      return;
    }
    if (tag === "OPTION") {
      if (orig.selected && !orig.hasAttribute("selected")) {
        clone.setAttribute("selected", "");
      } else if (!orig.selected && orig.hasAttribute("selected")) {
        clone.removeAttribute("selected");
      }
      return;
    }
    if (tag === "SELECT") {
      // Mark the option that is really selected, which is not always the one
      // the markup names - a script may have changed it. The clone's options
      // are different objects from the original's, so they are matched by
      // position, never by identity.
      const index = orig.selectedIndex;
      if (!orig.options || index < 0 || index >= orig.options.length) return;
      const kids = clone.options ? Array.from(clone.options) : [];
      if (index >= kids.length) return;
      for (let i = 0; i < kids.length; i++) {
        if (i === index) kids[i].setAttribute("selected", "");
        else if (kids[i].hasAttribute("selected")) kids[i].removeAttribute("selected");
      }
    }
  }

  // The snapshot stylesheet: one zero-specificity rule per element.
  function fallbackCss(styles) {
    if (!styles || styles.size === 0) return "";
    const out = [];
    for (const [nid, css] of styles) {
      if (!css) continue;
      out.push(':where([' + NID + '="' + nid + '"]){' + css + '}');
    }
    return out.join("\n");
  }

  function makeFallbackSheet(doc, styles) {
    const css = fallbackCss(styles);
    if (!css) return null;
    const style = doc.createElement("style");
    style.setAttribute(FALLBACK_ATTR, "");
    style.setAttribute(NID, ownId());
    style.textContent = css;
    return style;
  }

  // Put the snapshot stylesheet at the top of the document, so the page's own
  // rules - which are never zero-specificity - are what the browser reads
  // first and the snapshot only fills the gaps.
  function injectFallback(root, styles) {
    const sheet = makeFallbackSheet(root.ownerDocument, styles);
    if (!sheet) return;
    /* Last, not first: the page's own rules are never zero-specificity, so
     * they win wherever the sheet sits, and leaving the page's first element
     * alone keeps the mirror's document in the same order as the page's. */
    const head = root.querySelector ? root.querySelector("head") : null;
    if (head) head.appendChild(sheet);
    else root.appendChild(sheet);
  }

  function cleanClone(clone) {
    const tag = clone.tagName;
    if (tag === "SCRIPT") return false; // nothing runs in the mirror
    if (tag === "BASE") return false;   // the mirror's base is not the site's
    for (const attr of Array.from(clone.attributes || [])) {
      if (/^on/i.test(attr.name)) clone.removeAttribute(attr.name);
    }
    rewriteElement(clone);
    return true;
  }

  function walkChildren(origParent, cloneParent, ctx) {
    // Snapshot both. childNodes is live, so removing a node from the clone
    // would shift every later index and desync the two walks.
    const origKids = Array.from(origParent.childNodes);
    const cloneKids = Array.from(cloneParent.childNodes);
    for (let i = 0; i < origKids.length; i++) {
      const orig = origKids[i];
      const clone = cloneKids[i];
      if (!clone) break;
      if (orig.nodeType === 3) continue; // text is content, keep it
      if (orig.nodeType !== 1) {
        clone.remove();
        continue;
      }
      if (!cleanClone(clone)) {
        clone.remove();
        continue;
      }
      walk(orig, clone, ctx);
    }
  }

  /* Ids go on the clone as attributes rather than travelling in an ordered
   * sidecar. The sidecar has to line up with the order the mirror's parser
   * produces elements, and a <template shadowrootmode> is consumed by Chromium
   * but left in place by browsers without declarative shadow DOM - one extra
   * element silently shifts every later id. Attributes have no ordering to get
   * wrong, and the clone is thrown away, so the live page is still untouched. */
  function walk(orig, clone, ctx) {
    const nid = ensureId(orig);
    clone.setAttribute(NID, nid);

    /* The resolved styles, with any url() inside them routed through the
     * proxy the same way a stylesheet's would be. They are recorded rather
     * than written onto the element: the element keeps the author's own
     * inline style, and the snapshot becomes a fallback rule. */
    const computed = computedStyleText(orig);
    if (computed && ctx.styles) ctx.styles.set(nid, proxyCss(computed));

    if (clone.tagName === "STYLE") {
      clone.textContent = proxyCss(orig.textContent);
    }

    // A canvas carries its picture in pixels, not in markup, so cloning the
    // element copies an empty box. toDataURL is the only way to carry it, and
    // it throws if the canvas was tainted by a cross-origin image - in which
    // case there is nothing to carry and the box simply stays blank.
    if (clone.tagName === "CANVAS") {
      const pixels = canvasPixels(orig);
      if (pixels) clone.setAttribute(CANVAS_ATTR, pixels);
    }

    // The value of a form control lives in a property, not in the attribute.
    // A script that does input.value = "x" leaves the attribute saying
    // something else, and the mirror would then show the attribute - which is
    // the text the page was served with, not what the user actually sees.
    carryLiveState(orig, clone);

    const root = orig.shadowRoot;
    if (root) {
      const template = clone.ownerDocument.createElement("template");
      template.setAttribute("shadowrootmode", "open");
      // A ShadowRoot itself is not clonable - cloneNode on one throws
      // NotSupportedError - so clone its children into the template instead.
      const origShadowKids = Array.from(root.childNodes);
      for (const kid of origShadowKids) template.content.appendChild(kid.cloneNode(true));
      const cloneShadowKids = Array.from(template.content.childNodes);
      // A document stylesheet does not reach into a shadow tree, so the
      // snapshot for shadow content has to live inside the shadow root.
      const shadowCtx = { frameIds: ctx.frameIds, styles: new Map() };
      for (let i = 0; i < cloneShadowKids.length; i++) {
        const orig2 = origShadowKids[i];
        const clone2 = cloneShadowKids[i];
        if (!orig2 || !clone2) continue;
        if (orig2.nodeType === 3) continue;
        if (orig2.nodeType !== 1) { clone2.remove(); continue; }
        if (!cleanClone(clone2)) { clone2.remove(); continue; }
        walk(orig2, clone2, shadowCtx);
      }
      /* A shadow root's own <style> elements travel with the capture, so the
       * only styles it cannot bring are the ones in adoptedStyleSheets - a
       * constructable stylesheet, which is not markup and does not serialise.
       * Only then is a snapshot worth adding.
       *
       * It goes into the shadow's own last <style> element if it has one.
       * Adding an element to a shadow tree is not invisible: :first-child and
       * :last-child stop matching what they matched. Appending the rules to a
       * stylesheet that is already there adds no element at all, and because
       * the rules have zero specificity the shadow's own rules still win. */
      const adopted = root.adoptedStyleSheets;
      if (adopted && adopted.length) {
        const css = fallbackCss(shadowCtx.styles);
        if (css) {
          const own = template.content.querySelectorAll("style");
          if (own.length) {
            const last = own[own.length - 1];
            last.appendChild(clone.ownerDocument.createTextNode("\n" + css));
          } else {
            const shadowStyle = makeFallbackSheet(clone.ownerDocument, shadowCtx.styles);
            if (shadowStyle) template.content.appendChild(shadowStyle);
          }
        }
      }
      /* Light DOM is not rendered once a shadow root exists - unless the
       * shadow root has a slot, in which case the light DOM is what the slot
       * shows. Dropping it then blanks content the real page displays. The
       * light children are kept alongside the template, and the browser does
       * the slot assignment itself when it parses the declarative shadow
       * root. */
      const hasSlot = !!root.querySelector && !!root.querySelector("slot");
      if (!hasSlot) while (clone.firstChild) clone.firstChild.remove();
      clone.appendChild(template);
      return;
    }

    if (clone.matches && clone.matches(FRAME_SELECTOR)) {
      const assigned = ctx.frameIds.get(orig);
      if (assigned) {
        clone.setAttribute(WIN_ID, assigned);
        // The mirror fills this in with the frame's own captured document.
        clone.removeAttribute("src");
        clone.removeAttribute("data");
        clone.setAttribute("sandbox", "allow-same-origin");
      }
      return;
    }

    walkChildren(orig, clone, ctx);
  }

  // frameIds defaults because walk() reads it unconditionally, and this is
  // exported on __rbInternals where a caller may not have one.
  function serialiseDocumentOf(doc, frameIds = new Map()) {
    const clone = doc.documentElement.cloneNode(true);
    cleanClone(clone);
    const ctx = { frameIds, styles: new Map() };
    walk(doc.documentElement, clone, ctx);
    injectFallback(clone, ctx.styles);
    return { html: "<!DOCTYPE html>" + clone.outerHTML };
  }

  // One added node, with the sidecar the mirror needs to adopt it.
  function serialiseAdded(node) {
    const template = document.createElement("template");
    template.content.appendChild(node.cloneNode(true));
    const clone = template.content.firstChild;
    const ctx = { frameIds: new Map(), styles: new Map() };
    if (node.nodeType === 1) {
      if (!cleanClone(clone)) return null;
      walk(node, clone, ctx);
    }
    return { html: template.innerHTML, styles: styleMap(ctx.styles) };
  }

  // The snapshot for added nodes travels with the op, so the mirror can add
  // the matching fallback rules to the sheet for whichever root the node
  // lands in.
  function styleMap(styles) {
    const out = {};
    if (!styles) return out;
    for (const [nid, css] of styles) if (css) out[nid] = css;
    return out;
  }

  /* All of a node's children, with their sidecar. Used when a childList change
   * involves text nodes: those carry no id, so they cannot be added or removed
   * individually, and leaving them alone is how "one" + "two" becomes "onetwo".
   * Scoped to one parent's own children, not the subtree below it. */
  function serialiseChildren(node) {
    const template = document.createElement("template");
    const originals = Array.from(node.childNodes);
    for (const kid of originals) template.content.appendChild(kid.cloneNode(true));
    const clones = Array.from(template.content.childNodes);
    const ctx = { frameIds: new Map(), styles: new Map() };
    for (let i = 0; i < clones.length; i++) {
      const orig = originals[i];
      const cl = clones[i];
      if (!orig || !cl) continue;
      if (orig.nodeType === 3) continue; // text is content, keep it
      if (orig.nodeType !== 1) { cl.remove(); continue; }
      if (!cleanClone(cl)) { cl.remove(); continue; }
      walk(orig, cl, ctx);
    }
    return { html: template.innerHTML, styles: styleMap(ctx.styles) };
  }

  // Frame elements found by the last collectFrames(), with their windowIds.
  const frameEls = [];

  /* Depth-first over the document and every shadow root. Returns a map of frame
   * element -> the windowId minted for it, and fills frameEls. */
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
          if (doc && doc.documentElement) {
            const made = serialiseDocumentOf(doc, new Map());
            finish([{ windowId: entry.id, ...made }]);
          } else {
            finish([]);
          }
        } catch (e) {
          finish([]);
        }
      }, FRAME_REPLY_TIMEOUT);

      try {
        entry.el.contentWindow.postMessage({ rb: INIT_REQUEST, windowId: entry.id }, "*");
      } catch (e) {
        finish([]);
      }
    });
  }

  async function capture() {
    const frameIds = collectFrames();
    const mine = serialiseDocumentOf(document, frameIds);
    const frames = [{ windowId, ...mine }];
    const replies = await Promise.all(frameEls.map(requestFrame));
    for (const list of replies) {
      for (const frame of list) {
        if (frame && frame.windowId && frame.html) frames.push(frame);
      }
    }
    return frames;
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
    const backlog = held;
    held = [];
    for (const message of backlog) emit(message);
  }

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || data.rb !== INIT_REQUEST) return;
    event.stopPropagation();
    // event.source is null in some environments (sandboxed frames, and jsdom),
    // but in this protocol the requester is always our own parent.
    replyTo(event.source || window.parent, data.windowId);
  });

  // --------------------------------------------------------- mutations
  let observer = null;
  let queued = null;
  let rafId = null;
  const observedRoots = new Set();

  function onMutate(records) {
    if (!queued) queued = [];
    for (const record of records) queued.push(record);
    // Batched to the paint cadence rather than a fixed timer, so a burst of
    // changes lands in one frame instead of a trickle of them.
    // Called as a method of window on purpose: lifting requestAnimationFrame
    // off the object and calling it unbound throws "Illegal invocation" in
    // Chromium, which would silently stop every update after the first one.
    if (rafId === null) {
      rafId = window.requestAnimationFrame
        ? window.requestAnimationFrame.call(window, flush)
        : setTimeout(flush, 16);
    }
  }

  function flush() {
    rafId = null;
    const records = queued || [];
    queued = null;
    if (records.length === 0) return;

    const ops = [];
    const seenAttr = new Set();
    const addedElements = new Set();
    // Two passes: first find the parents whose children include bare text
    // nodes, because those have to be resynced as a unit and must not also get
    // granular add/rm ops for the same change.
    const textParents = new Set();
    for (const record of records) {
      if (record.type !== "childList") continue;
      for (const n of record.addedNodes) if (n.nodeType !== 1) { textParents.add(record.target); break; }
      for (const n of record.removedNodes) if (n.nodeType !== 1) { textParents.add(record.target); break; }
    }

    for (const record of records) {
      if (record.type === "childList") {
        const parentId = addressOf(record.target);
        if (!parentId) continue;
        if (textParents.has(record.target)) continue;
        for (const removed of record.removedNodes) {
          const id = removed.nodeType === 1 ? elementToId.get(removed) : null;
          if (id) ops.push({ op: "rm", id });
        }
        for (const added of record.addedNodes) {
          if (added.nodeType === 1) addedElements.add(added);
          const made = serialiseAdded(added);
          if (!made) continue;
          // Insertion point: the next element sibling still in the tree. Text
          // siblings cannot be addressed by id, so this can be off by a text
          // node - a resync clears that.
          const next = added.nodeType === 1 ? added.nextElementSibling : null;
          const op = {
            op: "add",
            parent: parentId,
            html: made.html,
            before: next ? ensureId(next) : null,
          };
          if (made.styles && Object.keys(made.styles).length) op.styles = made.styles;
          ops.push(op);
        }
      } else if (record.type === "attributes") {
        const id = ensureId(record.target);
        if (!id) continue;
        if (record.attributeName === WIN_ID || record.attributeName === NID) continue;
        const key = id + "|" + record.attributeName;
        if (seenAttr.has(key)) continue; // last write wins
        seenAttr.add(key);
        ops.push({
          op: "attr",
          id,
          name: record.attributeName,
          value: record.target.getAttribute(record.attributeName),
        });
      } else if (record.type === "characterData") {
        const parent = record.target.parentElement;
        const id = parent ? ensureId(parent) : null;
        if (!id) continue;
        ops.push({
          op: "text",
          parent: id,
          index: Array.prototype.indexOf.call(parent.childNodes, record.target),
          text: record.target.data,
        });
      }
    }

    // Parents whose children include unaddressable text nodes are rewritten as
    // a unit, read from the live DOM at flush time so a burst costs one copy.
    for (const parent of textParents) {
      const id = addressOf(parent);
      if (!id) continue;
      const made = serialiseChildren(parent);
      const cop = { op: "children", parent: id, html: made.html };
      if (made.styles && Object.keys(made.styles).length) cop.styles = made.styles;
      ops.push(cop);
    }

    scanShadowRoots(addedElements);

    if (ops.length === 0) return;
    send({ kind: "mut", windowId, ops });
  }

  /* Typing into a field changes .value, which is not a DOM mutation - no
   * MutationObserver will ever report it. Without this side channel a sign-in
   * form mirrors as permanently blank. */
  function onValueEvent(event) {
    const el = event.target;
    if (!el || el.nodeType !== 1) return;
    const id = ensureId(el);
    if (!id) return;
    send({
      kind: "mut",
      windowId,
      ops: [{ op: "value", id, value: el.value, checked: !!el.checked }],
    });
  }

  const OBSERVE = {
    childList: true,
    attributes: true,
    characterData: true,
    subtree: true,
  };

  function observeRoot(root) {
    if (!root || observedRoots.has(root)) return;
    observedRoots.add(root);
    // Defensive: every caller today runs after start() has built the observer,
    // but this executes inside a page we do not control and a throw here would
    // break that page rather than the mirror.
    if (observer) observer.observe(root, OBSERVE);
  }

  // Shadow roots attach themselves without any observable DOM mutation, so they
  // are found by scanning on a slow tick.
  let lastScan = 0;
  /* Shadow roots are not observable - attaching one is not a DOM mutation - so
   * they have to be looked for. Walking the whole document twice a second on a
   * page with thousands of elements taxes the site we are supposed to be
   * watching, so the routine work is scanning only the subtrees a flush just
   * saw added, with a slow full sweep as a backstop. */
  function observeShadowRootsIn(root) {
    if (!root || root.nodeType !== 1) return;
    if (root.shadowRoot) observeRoot(root.shadowRoot);
    if (!root.querySelectorAll) return;
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) observeRoot(el.shadowRoot);
    }
  }

  function scanShadowRoots(added) {
    if (added) {
      for (const node of added) observeShadowRootsIn(node);
    }
    const now = Date.now();
    if (now - lastScan < SHADOW_FULL_SCAN_MS) return;
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
    queued = null;
    try {
      const frames = await capture();
      // A frame that does not know its windowId yet cannot be addressed by the
      // mirror; its parent asks it again once ids are assigned.
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
    if (document.body) observeRoot(document.body);
    document.addEventListener("input", onValueEvent, true);
    document.addEventListener("change", onValueEvent, true);
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
    collectFrames,
    ensureId,
    serialiseDocumentOf,
    serialiseAdded,
    serialiseChildren,
    computedStyleText,
    WIN_ID,
    FRAME_SELECTOR,
  };
})();
