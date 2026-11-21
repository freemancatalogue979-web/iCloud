/* iCloud Browser Console — frontend logic. No build step, no dependencies. */

const $ = (id) => document.getElementById(id);

const STATE_META = {
  idle:            { cls: "pill-dim",  label: "idle" },
  starting:        { cls: "pill-warn", label: "starting" },
  running:         { cls: "pill-ok",   label: "running" },
  awaiting_signin: { cls: "pill-warn", label: "awaiting sign-in" },
  ready:           { cls: "pill-ok",   label: "ready" },
  error:           { cls: "pill-err",  label: "error" },
  stopped:         { cls: "pill-dim",  label: "stopped" },
};

let INFO = null;
let SESSION = { state: "idle" };
let evtSource = null;

/* ---------------------------------------------------------------- fetch */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { detail: text }; }
  if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
  return body;
}

/* ------------------------------------------------------------------ log */
function addLog(level, message, ts) {
  const list = $("log");
  const empty = list.querySelector(".empty");
  if (empty) empty.remove();

  const li = document.createElement("li");
  const time = ts ? ts.slice(11, 19) : new Date().toISOString().slice(11, 19);

  const t = document.createElement("span");
  t.className = "t";
  t.textContent = time;

  const lv = document.createElement("span");
  lv.className = `lv lv-${level}`;
  lv.textContent = level.toUpperCase();

  const m = document.createElement("span");
  m.className = "m";
  m.textContent = message;

  li.append(t, lv, m);
  list.appendChild(li);

  // Keep the DOM bounded.
  while (list.children.length > 400) list.removeChild(list.firstChild);
  list.scrollTop = list.scrollHeight;
}

function setConn(up) {
  const pill = $("conn-pill");
  pill.textContent = up ? "live" : "offline";
  pill.className = `pill ${up ? "pill-ok" : "pill-err"}`;
}

/* ------------------------------------------------------------------ SSE */
function connectStream() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource("/api/events");

  evtSource.onopen = () => setConn(true);
  evtSource.onerror = () => {
    setConn(false);
    // EventSource auto-reconnects; just reflect the state.
  };

  evtSource.addEventListener("log", (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    addLog(data.level, data.message, data.ts);
  });
}

/* ------------------------------------------------------------- rendering */
function renderState(state) {
  const meta = STATE_META[state] || { cls: "pill-dim", label: state || "unknown" };
  const pill = $("state-pill");
  pill.textContent = meta.label;
  pill.className = `pill ${meta.cls}`;

  const active = state === "running" || state === "awaiting_signin" || state === "ready";
  const starting = state === "starting";
  $("btn-start").disabled = active || starting;
  $("btn-stop").disabled = !active;
  $("btn-refresh").disabled = !active;
  $("btn-shot").disabled = !active;

  document.querySelectorAll(".chip").forEach((c) => { c.disabled = !active; });
}

function renderSnapshot(snap) {
  if (!snap) {
    $("f-state").textContent = "—";
    $("f-url").textContent = "—";
    $("f-title").textContent = "—";
    $("f-matched").textContent = "—";
    return;
  }
  $("f-state").textContent = snap.page_state || "—";
  $("f-url").textContent = snap.url || "—";
  $("f-title").textContent = snap.title || "(none)";
  $("f-matched").textContent = (snap.matched_selectors || []).join(", ") || "(none)";
}

function renderBanner(status) {
  const banner = $("banner");
  if (status.error) {
    banner.hidden = false;
    banner.className = "banner banner-err";
    banner.innerHTML = `<strong>Failed:</strong> ${escapeHtml(status.error)}`
      + (status.hint ? `<br><span class="hint">Fix: ${escapeHtml(status.hint)}</span>` : "");
    return;
  }
  if (status.state === "awaiting_signin") {
    banner.hidden = false;
    banner.className = "banner banner-warn";
    banner.textContent = status.hint || "Complete the Apple sign-in in the browser window.";
    return;
  }
  if (status.state === "ready") {
    banner.hidden = false;
    banner.className = "banner banner-ok";
    banner.textContent = "Signed in — session ready.";
    return;
  }
  banner.hidden = true;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderSession(status) {
  SESSION = status || SESSION;
  renderState(SESSION.state);
  renderSnapshot(SESSION.snapshot);
  renderBanner(SESSION);
}

/* ------------------------------------------------------------- startup */
async function loadInfo() {
  try {
    INFO = await api("/api/info");
  } catch (err) {
    addLog("error", `Could not load service info: ${err.message}`);
    return;
  }

  // Destination dropdown
  const select = $("target");
  select.innerHTML = "";
  Object.values(INFO.targets).forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t.name;
    opt.textContent = `${t.label}  —  ${t.url}`;
    select.appendChild(opt);
  });

  // Quick-nav chips
  const quick = $("quick");
  quick.innerHTML = "";
  Object.values(INFO.targets).forEach((t) => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.textContent = t.label;
    chip.disabled = true;
    chip.onclick = () => doGoto(t.name);
    quick.appendChild(chip);
  });

  // Driver readiness
  const wrap = $("drivers");
  wrap.innerHTML = "";
  INFO.drivers.forEach((d) => {
    const row = document.createElement("div");
    row.className = "driver-row";

    const left = document.createElement("div");
    const name = document.createElement("div");
    name.className = "driver-name";
    name.textContent = d.name;
    const detail = document.createElement("div");
    detail.className = "driver-detail";
    detail.textContent = d.detail;
    left.append(name, detail);

    const pill = document.createElement("span");
    pill.className = `pill ${d.browser_available ? "pill-ok"
      : (d.package_installed ? "pill-warn" : "pill-err")}`;
    pill.textContent = d.browser_available ? "ready"
      : (d.package_installed ? "no browser" : "not installed");

    row.append(left, pill);
    wrap.appendChild(row);
  });

  addLog("info", `Service ready — default driver: ${INFO.default_driver}`);
  INFO.drivers
    .filter((d) => !d.browser_available)
    .forEach((d) => addLog("warn", `${d.name} not launchable yet: ${d.detail}`));
}

/* ------------------------------------------------------------- actions */
function chosenTarget() {
  const custom = $("custom-url").value.trim();
  return custom || $("target").value || "home";
}

async function doStart() {
  $("btn-start").disabled = true;
  $("banner").hidden = true;
  renderState("starting");
  try {
    const status = await api("/api/session/start", {
      method: "POST",
      body: JSON.stringify({
        driver: $("driver").value,
        headless: $("headless").checked,
        target: chosenTarget(),
        wait_for_signin: true,
      }),
    });
    renderSession(status);
  } catch (err) {
    addLog("error", `Start failed: ${err.message}`);
    const banner = $("banner");
    banner.hidden = false;
    banner.className = "banner banner-err";
    banner.textContent = `Start failed: ${err.message}`;
    renderState("idle");
  }
}

async function doGoto(target) {
  try {
    const status = await api("/api/session/goto", {
      method: "POST",
      body: JSON.stringify({ target }),
    });
    renderSession(status);
  } catch (err) {
    addLog("error", `Navigate failed: ${err.message}`);
  }
}

async function doStop() {
  $("btn-stop").disabled = true;
  try {
    const status = await api("/api/session/stop", { method: "POST" });
    renderSession(status);
    $("shot-wrap").hidden = true;
  } catch (err) {
    addLog("error", `Stop failed: ${err.message}`);
  }
}

async function doRefresh() {
  try {
    const snap = await api("/api/session/snapshot");
    renderSnapshot(snap);
    addLog("step", `Snapshot: ${snap.page_state} @ ${snap.url}`);
  } catch (err) {
    addLog("error", `Snapshot failed: ${err.message}`);
  }
}

async function doScreenshot() {
  try {
    const res = await fetch("/api/session/screenshot");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    $("shot").src = URL.createObjectURL(blob);
    $("shot-wrap").hidden = false;
  } catch (err) {
    addLog("error", `Screenshot failed: ${err.message}`);
  }
}

/* ---------------------------------------------------------------- init */
window.addEventListener("DOMContentLoaded", () => {
  addLog("info", "Console loaded");
  $("btn-start").onclick = doStart;
  $("btn-stop").onclick = doStop;
  $("btn-refresh").onclick = doRefresh;
  $("btn-shot").onclick = doScreenshot;
  $("btn-clear").onclick = () => { $("log").innerHTML = ""; };

  loadInfo();
  connectStream();

  // Poll status as a fallback if SSE drops.
  setInterval(async () => {
    try { renderSession(await api("/api/session")); } catch { /* ignore */ }
  }, 4000);
});
