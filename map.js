/* The mapping between the capture shown in the frame and the real page in the
 * browser window.
 *
 * The captures kept the ids of the page they were taken from, so there is
 * nothing to translate: what is typed into `#account_name_text_field` belongs in
 * `#account_name_text_field` of the real page, and a click on `#sign-in` is a
 * click on the real Continue. This file only reports what happened - the
 * writing is done server-side, because the browser window is not reachable from
 * here.
 *
 * `attach(document, post)` takes the capture's document and a function that
 * posts one message to the server. It returns a function detaching it again.
 *
 * A SingleFile export keeps the sign-in form inside an `<iframe srcdoc="...">`,
 * so the fields are usually not in the document that was handed over: the
 * frames inside it are bound as well, now and whenever one finishes loading.
 * They are the same origin as the page that contains them, which is why this
 * works at all.
 *
 * Deliberately narrow:
 *
 *  - typing is debounced: a pause sends one message, not one per keystroke;
 *  - checkboxes and radios are left alone (their state is not a value);
 *  - a click is only taken from something clickable, and a link is stopped
 *    from navigating: the page that goes anywhere is the real one, in the
 *    browser window;
 *  - a form submit is stopped too, so the capture cannot navigate away from
 *    itself.
 */
(function (global) {
  "use strict";

  var TYPE_DELAY = 120;     // a pause after typing, not a request per letter
  var MAX_VALUE = 10000;

  function tagOf(el) {
    return (el.tagName || "").toLowerCase();
  }

  function typeOf(el) {
    return (el.getAttribute("type") || "").toLowerCase();
  }

  function clamp(value) {
    return String(value == null ? "" : value).slice(0, MAX_VALUE);
  }

  // The nearest thing a click means something on, or null.
  function clickable(el) {
    while (el && el.nodeType === 1) {
      var tag = tagOf(el);
      if (tag === "a" || tag === "button") return el;
      if (el.getAttribute) {
        var role = el.getAttribute("role");
        if (role === "button" || role === "link") return el;
      }
      if (tag === "input") {
        var type = typeOf(el);
        if (type === "button" || type === "submit") return el;
        return null;      // a text field is a value, not a click
      }
      el = el.parentElement;
    }
    return null;
  }

  function attach(doc, post) {
    if (!doc || typeof post !== "function") {
      return function () {};
    }

    var bound = [];       // {doc, listeners} for every document bound here
    var timers = {};      // field id -> the pause before it is sent

    function sendValue(id, value) {
      clearTimeout(timers[id]);
      timers[id] = setTimeout(function () {
        delete timers[id];
        post({ field: id, value: clamp(value) });
      }, TYPE_DELAY);
    }

    function listeners() {
      function onInput(event) {
        var el = event.target;
        if (!el || el.nodeType !== 1 || !el.id) return;
        var type = typeOf(el);
        if (type === "checkbox" || type === "radio" ||
            type === "button" || type === "submit") return;
        if (tagOf(el) === "select") return;     // change, not input
        sendValue(el.id, el.value);
      }

      function onChange(event) {
        var el = event.target;
        if (!el || el.nodeType !== 1 || !el.id) return;
        if (tagOf(el) !== "select") return;
        post({ field: el.id, value: clamp(el.value) });
      }

      function onClick(event) {
        var el = clickable(event.target);
        if (!el) return;
        if (tagOf(el) === "a" && event.preventDefault) event.preventDefault();
        if (!el.id) return;
        post({ field: el.id, click: true });
      }

      function onSubmit(event) {
        if (event.preventDefault) event.preventDefault();
      }

      return { input: onInput, change: onChange, click: onClick, submit: onSubmit };
    }

    function bindOne(target) {
      if (!target || target.__mapped || !target.addEventListener) return false;
      target.__mapped = true;
      var set = listeners();
      set.load = function (event) { descend(event.target); };
      target.addEventListener("input", set.input, true);
      target.addEventListener("change", set.change, true);
      target.addEventListener("click", set.click, true);
      target.addEventListener("submit", set.submit, true);
      target.addEventListener("load", set.load, true);
      bound.push({ doc: target, set: set });
      return true;
    }

    // A frame that has just finished loading may hold the fields, and may hold
    // frames of its own.
    function descend(target) {
      if (!target || !target.querySelectorAll) return;
      var inner = [];
      if (tagOf(target) === "iframe" || tagOf(target) === "frame") {
        inner.push(target);
      }
      var found = target.querySelectorAll("iframe, frame");
      for (var i = 0; i < found.length; i++) inner.push(found[i]);

      for (var j = 0; j < inner.length; j++) {
        var frame = null;
        try {
          frame = inner[j].contentDocument;      // same origin: this works
        } catch (e) {
          frame = null;                          // another site: leave it be
        }
        if (!frame) continue;
        if (bindOne(frame)) descend(frame);
      }
    }

    bindOne(doc);
    descend(doc);

    return function detach() {
      Object.keys(timers).forEach(function (id) { clearTimeout(timers[id]); });
      timers = {};
      bound.forEach(function (entry) {
        var set = entry.set;
        entry.doc.removeEventListener("input", set.input, true);
        entry.doc.removeEventListener("change", set.change, true);
        entry.doc.removeEventListener("click", set.click, true);
        entry.doc.removeEventListener("submit", set.submit, true);
        entry.doc.removeEventListener("load", set.load, true);
        entry.doc.__mapped = false;
      });
      bound = [];
    };
  }

  global.attachMapping = attach;

  /* Exported for the tests, which run this file outside a browser page. */
  if (typeof module === "object" && module.exports) {
    module.exports = { attach: attach };
  }
})(typeof window !== "undefined" ? window : globalThis);
