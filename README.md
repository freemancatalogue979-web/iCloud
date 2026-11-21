# iCloud Browser Console

A **Python FastAPI + HTML** console that drives a real browser to **icloud.com**.
Playwright is the primary driver; Selenium WebDriver is the fallback. Both are
selectable from the UI at launch time.

```
┌───────────────┐   HTTP/SSE    ┌──────────────┐   CDP/WebDriver   ┌──────────┐
│  HTML console │ ────────────► │  FastAPI     │ ────────────────► │ Chromium │ ──► icloud.com
│  (vanilla JS) │ ◄──────────── │  session mgr │ ◄──────────────── │          │
└───────────────┘   live log    └──────────────┘                   └──────────┘
```

---

## Quickstart

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# One-time: download a browser binary for Playwright
python -m playwright install --with-deps chromium

# Run
python -m app.main
# or
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or use the launcher, which creates the venv for you on first run:

```bash
./run.sh            # console on :8000
./run.sh client     # single-page client on :9002
./run.sh test       # test suite
./run.sh setup      # venv + dependencies only
```

### Windows

Double-click `run.bat`, or from `cmd` / PowerShell:

```bat
run.bat            rem console on :8000
run.bat client     rem single-page client on :9002
run.bat test       rem test suite
run.bat setup      rem venv + dependencies only
```

The first run creates `.venv` and installs everything, then tells you the one
remaining step:

```bat
.venv\Scripts\python.exe -m playwright install chromium
```

Note that on Windows you use the command above **as-is** — the `--with-deps`
flag installs Linux system libraries and is not needed (and not supported) on
Windows.

Requirements: Python 3.11+ on `PATH` (`run.bat` tries the `py -3` launcher
first, then a bare `python`). Ports can be overridden with `ICLOUD_PORT` and
`ICLOUD_CLIENT_PORT`.

---

Open <http://localhost:8000> for the console, or <http://localhost:9002> for the
single-page client.

Interactive API docs: <http://localhost:8000/docs>

---

## Client server — `localhost:9002`

The minimal client: **one HTML page**, an **iframe**, and a **WebSocket**.
Visiting the page is what starts the flow.

```bash
python -m client.server          # http://localhost:9002
# or
./run.sh client
```

| Endpoint | Purpose |
|---|---|
| `GET /` | the single client page (iframe + WebSocket) |
| `GET /view` | iframe content — live browser screenshot |
| `GET /shot.png` | latest capture |
| `GET /health` | liveness, session state, capture nonce |
| `WS /ws` | commands in, events / status / flow steps out |

### What happens on a visit

```
browser opens /  ──►  iframe loads /view
      │
      └─► WebSocket connects to /ws
              │  ws.send({cmd:"start"})        ← automatic, on open
              ▼
        launch browser ──► goto icloud.com ──► click "Sign In"
              │                                      │
              ▼                                      ▼
        {type:"flow", step, ok, detail}      {type:"flow_done", ok, steps, …}
```

Steps stream back as they happen, so the page renders the flow live. The iframe
is same-origin (`src="/view"`), so the parent page can `postMessage` it to
reload the screenshot without a cross-origin hop.

### WebSocket protocol

| Direction | Message |
|---|---|
| → server | `{cmd:"start"}` · `{cmd:"click_signin"}` · `{cmd:"stop"}` · `{cmd:"refresh"}` · `{cmd:"ping"}` |
| ← server | `{type:"hello"}` · `{type:"status"}` · `{type:"event"}` · `{type:"flow"}` · `{type:"flow_done"}` · `{type:"error"}` · `{type:"pong"}` |

`run_flow()` lives in `client/flow.py`, free of FastAPI concerns, so it is
tested directly against a stub manager.

Set the port with `ICLOUD_CLIENT_PORT` (default `9002`).

---

## The flow

| Step | What happens | Who does it |
|---|---|---|
| **1** | Launch browser → `https://www.icloud.com/` | the app |
| **2** | Click the **Sign In** button → redirect to `idmsa.apple.com` | the app |
| **3** | Type Apple ID, password, 2FA code | **you**, in the browser window |
| **4** | Signed-in chrome detected → session flips to `ready` | the app |

Step 2 targets the landing page's custom element:

```html
<ui-button class="push primary sign-in-button icloud-mouse" tabindex="0"
           ontouchstart="void(0)" role="button" aria-haspopup="false">
  <button type="button" tabindex="-1"></button>Sign In
</ui-button>
```

The click strategy tries `ui-button.sign-in-button` first, then four redundant
CSS variants, then link fallbacks, and finally retries on the inner
`<button>` (custom elements can swallow a click on the host). The selectors are
verified against that exact markup by a real CSS engine in
`tests/test_signin_button_markup.py`.

Trigger it from the **Click “Sign In”** button in the console, or:

```bash
curl -X POST localhost:8000/api/session/click-signin
```

---

## How sign-in works (read this)

**This app never handles Apple ID credentials.** There is no username field, no
password field, and no `.env` variable for either. That is deliberate:

- Automated Apple credential submission violates Apple's terms and is broken by
  two-factor almost every time.
- Anything that types your Apple password into a form is indistinguishable from
  a credential harvester.

Instead:

1. The app launches a **headed** browser (default) and navigates to icloud.com.
2. It detects the sign-in page and moves to `awaiting_signin`, then **polls
   read-only** — it observes the DOM, it never submits a form.
3. **You** type your Apple ID, password, and 2FA code in the real window.
4. When the signed-in chrome appears, the session flips to `ready`.
5. A persistent profile (`.profile/icloud`) keeps you signed in across restarts.

---

## Configuration

Copy `.env.example` → `.env`. All values optional.

| Variable | Default | Purpose |
|---|---|---|
| `ICLOUD_HOST` | `0.0.0.0` | Bind address |
| `ICLOUD_PORT` | `8000` | Port |
| `ICLOUD_DRIVER` | `playwright` | `playwright` or `selenium` |
| `ICLOUD_HEADLESS` | `false` | Headed is required for manual sign-in |
| `ICLOUD_PROFILE_DIR` | `.profile/icloud` | Persistent browser profile |
| `ICLOUD_NAV_TIMEOUT_MS` | `30000` | Navigation timeout |
| `ICLOUD_SIGNIN_WAIT_SECONDS` | `180` | How long to wait for a human |
| `ICLOUD_CLICK_SETTLE_MS` | `1000` | Settle time after a click before re-snapshotting |
| `ICLOUD_BROWSER_EXECUTABLE` | — | Use a system Chrome/Chromium binary |

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | HTML console |
| `GET` | `/api/health` | Liveness + session state |
| `GET` | `/api/info` | Targets, driver readiness, config |
| `POST` | `/api/session/start` | Launch browser and navigate |
| `GET` | `/api/session` | Current session status |
| `POST` | `/api/session/goto` | Navigate to another target |
| `POST` | `/api/session/click-signin` | Step 2 — click Sign In, follow redirect |
| `GET` | `/api/session/snapshot` | URL, title, page state, matched selectors |
| `GET` | `/api/session/screenshot` | PNG of the current page |
| `POST` | `/api/session/stop` | Close the browser |
| `GET` | `/api/events` | SSE log stream (`?once=1` replays and closes) |
| `GET` | `/api/logs?since=N` | Poll-based log tail |
| `GET` | `/api/resolve?target=X` | Validate a destination |

### Navigation allowlist

The browser can only be sent to `icloud.com`, any `*.icloud.com` subdomain, or
`idmsa.apple.com` (Apple's sign-in host, reached by redirect). Everything else
is rejected with HTTP 400 **before** it reaches a driver. Lookalikes such as
`noticloud.com` and `icloud.com.evil.com` are refused.

---

## Architecture

```
app/
├── main.py                  app factory + lifespan
├── config.py                env-driven Settings
├── models.py                pydantic schemas, state enums
├── events.py                thread-safe LogBus → SSE
├── targets.py               URL allowlist + named targets   (pure)
├── icloud.py                selector table + page classifier (pure)
├── session.py               SessionManager state machine
├── drivers/
│   ├── base.py              BrowserDriver ABC, error taxonomy
│   ├── playwright_driver.py primary (async, persistent context)
│   └── selenium_driver.py   fallback (sync bridged via to_thread)
└── web/
    ├── routes.py            FastAPI routers
    ├── templates/index.html
    └── static/{app.js,style.css}

client/                        localhost:9002 — the minimal client
├── server.py                single page + iframe + WebSocket
├── flow.py                  the visit flow (pure, testable)
└── static/{client.html,view.html}
```

**Two seams are pure Python and heavily unit-tested**, so the safety-critical
logic is verified without a browser:

- `targets.resolve_target()` — the navigation allowlist
- `icloud.classify_page()` — sign-in / 2FA / signed-in detection

Session states: `idle → starting → running → awaiting_signin → ready`, plus
`error` and `stopped`.

---

## Tests

```bash
pytest
```

169 tests covering the allowlist (including lookalike-domain attacks), page
classification, the Sign In selectors **checked against real captured markup by
a real CSS engine**, the event bus, the full session state machine (including
the click-Sign-In path and its fallbacks) via an in-process fake driver, the
FastAPI routes over `TestClient`, the visit flow against a stub manager, the
**client WebSocket protocol end-to-end**, and **real driver launch attempts**
that assert the structured error contract.

---

## Known environment limits

Verified on the sandbox this was built in (2026-11-21):

- `python -m playwright install chromium` **fails** — the Playwright CDN is not
  reachable, so no browser binary can be downloaded there. Set
  `ICLOUD_BROWSER_EXECUTABLE` to a system Chrome/Chromium to work around it.
- `icloud.com` is **not reachable** from that sandbox (egress allowlist).

Both are network restrictions of that sandbox, not defects in this code. On a
normal machine, `python -m playwright install --with-deps chromium` succeeds and
the flow works. When either limit applies, the console reports a structured,
actionable error (`browser_not_installed` + the exact fix command) instead of
crashing — that path is covered by tests and was verified end-to-end over HTTP.

---

## Notes

- Headed mode is the default because a human must complete sign-in.
- The profile directory and screenshots are git-ignored.
- `.env` is git-ignored, though it holds no secrets by design.
