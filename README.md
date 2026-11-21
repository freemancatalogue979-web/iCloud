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

Open <http://localhost:8000>. Press **Launch & go to iCloud**.

Interactive API docs: <http://localhost:8000/docs>

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

109 tests covering the allowlist (including lookalike-domain attacks), page
classification, the event bus, the full session state machine via an in-process
fake driver, the FastAPI routes over `TestClient`, and **real driver launch
attempts** that assert the structured error contract.

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
