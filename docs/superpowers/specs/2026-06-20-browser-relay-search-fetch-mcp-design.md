# browser-relay-mcp — Design Spec

- **Date:** 2026-06-20
- **Status:** Approved (design); pending implementation plan
- **Location:** `~/personal/localvily`
- **Package / project name:** `browser-relay-mcp`
- **MCP server id:** `browser-relay`
- **Relay port:** `15552` (distinct from google-ai-scraper's `15551` so both run side-by-side)

---

## 1. Problem & motivation

Automated pipelines (RAG, research agents, STORM-style article generation) need to
**search the web and read full page content at high volume**. Every metered option hits a
ceiling: hosted search APIs (Tavily, Serper, Brave) exhaust free tiers and meter paid
calls; self-hosted SearXNG over free upstreams gets rate-limited / IP-blocked under
sustained automated volume (~25–30 searches/task) within minutes. Confirmed unusable for
batch fan-out.

The unlimited, already-authenticated resource is the user's own browser. This project
exposes it as a programmatic **search + fetch** backend behind a generic MCP surface,
generalizing the proven `~/personal/google-ai-scraper` relay (Chrome extension + FastAPI
relay + FastMCP server scraping Google AI Overviews) into "scrape a generic SERP + extract
full readable page text."

It supports **two access modes behind one MCP surface**:
- **Attended relay** — drives the user's real, logged-in Chrome via an extension. Unlimited
  for SERP burst because it *is* a human session (no evasion needed); but it has no evasion,
  so it stalls on Cloudflare-style challenges.
- **Unattended cloak** — an embedded stealth Chromium (persistent profile) that runs without
  the user watching and can read hostile / bot-protected pages. Automated, so it re-enters
  bot-detection territory; mitigated by stealth + a human-in-the-loop handoff.

Both modes share a **headful escalation**: on a CAPTCHA or login wall, the relevant browser
window is surfaced to the user and the need is communicated *through the MCP*
(`action_required` → user acts → `resume`).

## 2. Goals / non-goals

### Goals
- Generic, reusable MCP surface: `search`, `fetch`, `search_and_fetch`, `resume`, `health`.
- Two interchangeable **drivers** behind that surface: `relay` (default) and `cloak`.
- Unlimited / unmetered for the attended relay path; no API keys.
- Tolerate a burst of dozens–hundreds of search+fetch calls per task with **0 rate-limit
  failures and 0 silent-empty results** (validated on the relay driver — acceptance #3).
- Read bot-protected / JS-heavy pages via the cloak driver's stealth browser.
- Human-in-the-loop handoff for CAPTCHA/login, fully expressed through MCP tool results.
- Fail loud when a driver/browser is down (clear error, never hang, never fake-empty).
- Local-only: all hops on localhost; no third-party services in the data path.

### Non-goals (confirmed)
- **No Firefox extension** (Chrome only for the relay driver).
- **No image generation** (a google-ai-scraper feature that does not fit a web-access tool).
- **No thread / follow-up state** — `search`/`fetch` are stateless request→response.
  (The only stateful concept is a *paused job* awaiting human action; see §10.)
- **No automated/programmatic CAPTCHA solving** and **no third-party CAPTCHA-solving
  services** — challenges are handed to a human via escalation, never solved by us.
- **No result ranking/reranking, dedup, or citation formatting** — downstream concerns.

> **Reversed from the prior draft:** "no headless operation" and "no anti-bot evasion" are
> removed — the cloak driver is exactly an unattended, stealth (evasion-hardened) browser.
> Note the cloak driver runs **headed-but-backgrounded**, *not* true-headless, by design
> (see §8): true headless is more detectable — undermining the evasion that is the point —
> and can't show a window for the human handoff.

### "Full parity except Firefox" interpretation
Parity = **distribution & polish** parity with google-ai-scraper, not copying
image-gen/threads: Chrome extension (load-unpacked dev **and** Web Store packaging); `uvx` /
PyPI distribution; stdio **and** SSE MCP transports; mature shared-backend infra
(auto-spawn, file-lock, version-kill, parent watchdog); popup + options UI; README +
privacy-policy docs.

## 3. Drivers overview

One MCP surface, a `driver` parameter selects the backend (`relay` default | `cloak`).

| | `relay` (attended) | `cloak` (unattended) |
|---|---|---|
| What it is | the user's real logged-in Chrome, via the extension | embedded stealth Chromium (Playwright persistent context), evasion-hardened |
| Where it runs | Chrome + extension; backend relays via poll | in-process in the FastAPI backend (drives Playwright directly) |
| "Unlimited" because… | it **is** a human session — engines tolerate it | stealth + residential IP + real cookies *reduce* blocks, don't eliminate them |
| Best at | **SERP burst** (acceptance #3), zero evasion needed | **bot-protected / hostile pages**, **unattended** runs |
| Weakness | stalls on Cloudflare-style challenges | automated SERP patterns can still be challenged |
| Window | the user's normal browser, background tabs | headed-but-backgrounded; surfaced to front on escalation |
| Auth | whatever the user is logged into in Chrome | persistent profile dir; log in once headful, reused thereafter |

**Shared DOM-facing logic (single source of truth).** SERP parsing, block detection, and
Readability extraction live in plain JS modules under `extension/` (`engines/*.js`,
`extract.js`, `lib/Readability.js`). The **relay** driver injects them via
`chrome.scripting.executeScript`; the **cloak** driver reads the same files and injects them
via Playwright `page.evaluate`. Drivers differ only in *how they obtain a loaded DOM* and
*how they manage tabs/escalation* — parsing results are identical across both.

## 4. Architecture

```
                                          ┌─ driver=relay → queue → /pending poll → Chrome extension → background tabs (attended)
MCP client ─stdio/SSE─▶ FastMCP server ─httpx─▶ FastAPI relay :15552 (job router) ─┤
 (Claude / STORM)        search / fetch /            │                            └─ driver=cloak → in-proc Playwright stealth Chromium
                         search_and_fetch /          │                                              (persistent profile, headed-bg)
                         resume / health             ▼
                                          action registry: resume_token → paused job (open tab/page kept alive, TTL)
                                                         ▲ on CAPTCHA/login: window surfaced + action_required returned
```

### Request lifecycle — `relay` driver (inherited from google-ai-scraper)
1. MCP tool → relay endpoint; relay enqueues a job, blocks the HTTP request on an
   `asyncio.Event` (timeout).
2. Extension polls `/pending`, gets a **batch** of jobs (bounded by in-flight caps +
   search spacing).
3. Extension drives background tab(s), injects shared JS, `POST /result/{job_id}`.
4. Relay sets the event; the blocked request returns the result.

### Request lifecycle — `cloak` driver
1. MCP tool → relay endpoint with `driver=cloak`; relay dispatches to the in-process cloak
   driver (async Playwright), respecting the same concurrency caps.
2. Cloak driver navigates its stealth browser, injects the **same** shared JS via
   `page.evaluate`, returns the result inline (no extension, no polling).

### Escalation flow (either driver)
On block/login detection: surface the relevant window to the user (relay: set its tab
`active`; cloak: `bring_to_front`), **keep the tab/page open**, register a paused job keyed
by `resume_token`, and return `action_required`. `resume(resume_token)` re-checks: if the
challenge cleared, finish the operation and return the result; else `action_required` again;
if the token expired (TTL), return an error.

### Reuse map (vs google-ai-scraper)

| Reused **as-is** | Built **new** |
|---|---|
| Backend auto-spawn + file-lock + version-kill + parent watchdog | Job router by `driver`; in-process cloak (Playwright) execution path |
| MV3 keep-alive polling via `chrome.alarms`; blocking-request → event → `/result` | Pluggable SERP parsers (Bing first) + block/CAPTCHA detection (shared JS) |
| `/health` connectivity via last-poll-age; HTTP code → friendly message mapping | Readability.js in-page extraction (+ `innerText` fallback), shared by both drivers |
| stdio + SSE; uvx packaging; popup/options scaffolding; `withGoogleTab` tab pattern | **Batch `/pending`** dispatch + adaptive-split caps; **action registry + `resume`** |

## 5. MCP tool contract

All tools return a JSON string. Every result carries a top-level `status`:
- `"ok"` — normal success (payload as below).
- `"action_required"` — human action needed: `{status, action, url, message, resume_token, driver}`
  where `action ∈ {"solve_captcha","login"}`.
- `"error"` — hard failure: `{status, error}`.

```jsonc
search(query: str, k: int = 10, engine: str = "bing", driver: str = "relay")
ok → { status:"ok", query, engine, driver, count, results:[ {title, url, snippet} ] }

fetch(url: str, include_html: bool = false, driver: str = "relay")
ok → { status:"ok", url, driver, title, text, excerpt, length, html? }

search_and_fetch(query: str, k: int = 5, engine: str = "bing", driver: str = "relay")
ok → { status:"ok", query, engine, driver, count,
       results:[ {title, url, snippet, text, length, fetch_error:null} ] }
   // search step may escalate (→ action_required for the whole call).
   // a per-result fetch that needs human action sets fetch_error:"action_required: <url>"
   // (the batch stays non-blocking); the caller can fetch(url, driver) individually to
   // trigger that page's handoff. a normal page failure sets its own fetch_error.

resume(resume_token: str)
→ the resumed operation's result: ok | action_required (same token) | error (expired/unknown)

health()
ok → { status:"ok",
       drivers: { relay: {extension_connected, extension_status, last_poll_age_seconds},
                  cloak: {browser_up, available, profile_path} },
       search_queued, fetch_queued, in_flight, max_fetch_tabs, engine,
       pending_actions:[ {resume_token, action, url, driver, since} ],
       version }
```

`driver` defaults to `"relay"` (the validated unlimited primary); agents opt into `"cloak"`
for bot-protected pages or unattended runs.

## 6. Relay (FastAPI) — endpoints, job model, dispatch, action registry

### Endpoints
| Endpoint | Method | Purpose |
|---|---|---|
| `/search?q=&k=&engine=&driver=` | GET | Enqueue/dispatch a search job; block until result / action_required / timeout |
| `/fetch?url=&include_html=&driver=` | GET | Enqueue/dispatch a fetch job; block until result / action_required / timeout |
| `/resume/{resume_token}` | POST | Continue a paused job; returns ok / action_required / error |
| `/pending` | GET | **relay driver only**: extension polls; returns a batch of jobs + tabs to close |
| `/result/{job_id}` | POST | **relay driver only**: extension posts a result *or* an `action_required` signal |
| `/health` | GET | Status, both drivers, queue depths, `pending_actions`, version |
| `/version` | GET | Running backend version (drives version-kill on upgrade) |

### Job model & routing
- Job: `{job_id, driver, kind ∈ {search,fetch}, payload, event, result}`.
- `driver=relay` → enqueue to `search_queue`/`fetch_queue`, served by `/pending`.
- `driver=cloak` → handed to the in-process cloak executor (async task), bounded by the same caps.
- In-flight counters per kind, decremented on `/result`, inline completion, or timeout.

### Batch dispatch (relay) — the one real change vs google-ai-scraper's single-job `/pending`
`/pending` returns up to available capacity:
- `search`: ≤ `SEARCH_CONCURRENCY` (default **1**) in-flight, gated by
  `now - last_search_dispatch ≥ SEARCH_MIN_SPACING_MS` (default **500ms**).
- `fetch`: ≤ `FETCH_CAP` (default **5**) in-flight.
- Also drains a `close_queue` of tab IDs. Response: `{ jobs:[...], close_tabs:[...] }`.

### Action registry (escalation)
- `resume_token → {driver, kind, original_params, tab_or_page_handle, created_at}`.
- Created when a job escalates; the open tab/page is kept alive.
- `ACTION_TTL` (default **300s**): on expiry, drop the entry and close the tab/page; a later
  `resume` returns `error:"action expired"`. The original call already returned
  `action_required`, so nothing hangs.

### Backpressure & timeouts (fail loud)
- Over-cap calls **block in queue** (do not fail); return when capacity frees.
- `QUERY_TIMEOUT` (search ~110s) / `FETCH_TIMEOUT` (~60s) cover MV3 wake + tab work →
  `504` with a cause-naming message; MCP maps to `{status:"error", error}`.
- Extension-down: `/health` reports `relay.extension_connected=false` past the poll
  threshold; relay-driver blocking calls then time out with a clear message (never hang).

### Config knobs (env + options UI)
| Knob | Default | Meaning |
|---|---|---|
| `FETCH_CAP` | 5 | Max parallel fetch tabs/pages |
| `SEARCH_CONCURRENCY` | 1 | Max in-flight searches |
| `SEARCH_MIN_SPACING_MS` | 500 | Min gap between dispatched searches |
| `FETCH_SETTLE_MS` | 800 | Wait after load `complete` before extracting (JS render) |
| `QUERY_TIMEOUT` / `FETCH_TIMEOUT` | ~110s / ~60s | Blocking-call timeouts |
| `ACTION_TTL` | 300s | How long a paused (awaiting-human) job survives |
| `DEFAULT_ENGINE` | `bing` | Default SERP engine |
| `CLOAK_PROFILE_DIR` | `<project>/.cloak-profile` | Persistent stealth profile path |

## 7. Driver: extension relay (Chrome, MV3)

- **manifest:** `permissions: tabs, scripting, alarms, storage`; `host_permissions: <all_urls>`
  + localhost. **No** broad `content_scripts` — inject shared JS on demand into the specific
  background tab (cleaner privacy).
- **search:** use a **dedicated background search tab we own** (tracked by id, created if
  absent, never the user's own tabs), navigate to the engine SERP URL (`active:false`), wait
  for results DOM, inject the engine parser → `{title,url,snippet}[]` or a block signal.
- **fetch:** create a background tab → wait `complete` + `FETCH_SETTLE_MS` → inject
  `Readability.js` + `extract.js` → `{title,text,excerpt,length}`, fallback
  `body.innerText` → close tab. Pool up to `FETCH_CAP`.
- **block/login detected:** instead of returning data, `POST /result` with an
  `action_required` signal **and** set the tab `active` (visible) so the user sees the
  challenge; keep the tab open. Backend registers the paused action and returns
  `action_required`. On `resume`, the extension re-checks the same tab: cleared → parse/
  extract and return; else action_required again.

## 8. Driver: cloak (stealth Chromium, in-process)

- **Engine:** Playwright **persistent context** (`user_data_dir = CLOAK_PROFILE_DIR`) with the
  stealth hardening that the `cloakbrowser-setup` skill applies (anti-fingerprint, Turnstile/
  reCAPTCHA-resilient). Prefer the real Chrome channel for max stealth + real-profile access.
- **Mode:** **headed-but-backgrounded**, *not* true-headless — true headless is more
  detectable (defeats the evasion purpose) and cannot show a window for the human handoff. The
  window stays unfocused/minimized during normal operation and is brought to front only on
  escalation.
- **search/fetch:** navigate the page, inject the **same** shared JS (`engines/*.js`,
  `extract.js`, `Readability.js`) via `page.evaluate`, return results inline. Fetch parallelism
  uses multiple pages up to `FETCH_CAP`.
- **escalation:** on block/login → `page.bring_to_front()`, keep the page open, register the
  paused action, return `action_required`. `login` persists via the profile: the user logs in
  once in the surfaced window; later headless fetches reuse those cookies.
- **availability:** if the browser/profile fails to launch (e.g., profile locked by another
  run), `health.cloak.available=false`; cloak-driver calls return a clear error; the relay
  driver is unaffected.

## 9. Shared DOM logic & engine parser interface

Plain JS, injected by both drivers (single source of truth), under `extension/`:
```js
// engines/<name>.js
export const name = "bing";
export function serpUrl(query, k) { /* → string */ }
export function detectBlock(doc) { /* → boolean (challenge/CAPTCHA/login-wall?) */ }
export function parse(doc, k) { /* → [{title, url, snippet}] */ }
// extract.js: extract(doc) → {title, text, excerpt, length} (Readability + innerText fallback)
```
A registry (`engines/index.js`) maps `engine` name → module; v1 ships `bing`. Adding
`duckduckgo`/`google` later is a new file + registry entry — uniform output + block-detection
contract, so the MCP/relay surface is unchanged. The cloak driver loads these same files from
disk and evaluates them in-page (slight coupling: cloak reads from `extension/`; accepted for
v1 to keep one source and avoid a build step).

## 10. Escalation protocol (`action_required` + `resume`)

1. Driver detects a challenge/login wall (shared `detectBlock`).
2. Surface the window (relay: tab `active`; cloak: `bring_to_front`); keep tab/page open.
3. Register paused job → `resume_token`; return
   `{status:"action_required", action, url, message, resume_token, driver}`.
4. Agent relays `message` to the user; user solves the CAPTCHA / logs in.
5. Agent calls `resume(resume_token)`:
   - cleared → finish the original search/fetch, return `{status:"ok", ...}`;
   - not cleared → `{status:"action_required", ... same token}`;
   - expired/unknown → `{status:"error", error:"action expired"}`.
6. `health.pending_actions` lists all open requests for observability.

Degradation: with no human present, the agent simply never resumes; the paused job expires at
`ACTION_TTL` and the tab/page closes. Nothing blocks indefinitely (the original call already
returned `action_required`). An autonomous pipeline can treat `action_required` as a soft
skip / fall back to the other driver.

## 11. Concurrency / throughput envelope (documented; to be measured)

| Op | Per-call (est.) | Concurrency | Rate (est.) |
|---|---|---|---|
| search (relay) | ~1.5–3 s | 1 (~500ms spacing) | ~20–30 / min |
| fetch (relay) | ~2–4 s | up to 5 parallel | ~75–150 / min |
| search/fetch (cloak) | similar + ~1–3 s first-call browser warmup; slightly higher per-call from stealth | same caps | measured separately |

**50 search+fetch burst (relay driver):** est. **~2–4 min**, **0 rate-limit failures** —
gentle near-serial search cadence + parallel fetch of unrelated URLs. The relay driver is the
validated primary for acceptance #3; the cloak driver is measured independently (its value is
hostile-page access + unattended operation, not burst-SERP throughput).

## 12. Error handling matrix (fail loud, never silent-empty)

| Condition | Behavior |
|---|---|
| Relay extension down | `health.relay` disconnected; relay-driver blocking call → `504` "browser/extension not connected"; tool → `{status:error}` (never hangs) |
| Cloak browser/profile won't launch | `health.cloak.available=false`; cloak-driver calls → `{status:error}`; relay unaffected |
| SERP/login challenge | `{status:"action_required", ...}` + window surfaced (NOT empty, NOT error) |
| Real zero-result SERP | `{status:"ok", count:0, results:[]}` (legitimate empty, clearly flagged) |
| Page fetch nav error / timeout / non-HTML | `fetch` → `{status:error}`; in `search_and_fetch` → per-result `fetch_error`, batch survives |
| Readability null + empty innerText | `{status:"error", error:"no extractable content"}` |
| `resume` token expired/unknown | `{status:"error", error:"action expired"}` |
| Relay unreachable from MCP | MCP auto-spawns/reuses backend; if still unreachable → `{status:error, error:"Cannot connect to relay ..."}` |

## 13. STORM adapter (acceptance #5, ~5 lines)

`adapters/storm.py` maps `search_and_fetch` output → STORM/dspy retriever shape:
```python
def to_storm(r):  # r = parsed search_and_fetch() output dict
    if r.get("status") != "ok":
        return []
    return [{"url": x["url"], "title": x["title"],
             "description": x.get("snippet", ""),
             "snippets": chunk(x["text"])}          # chunk(): split text into ~1k-char chunks
            for x in r["results"] if x.get("text")]
```

## 14. Project layout

```
~/personal/localvily/
  README.md
  CLAUDE.md                         # project memory (created during impl)
  .gitignore                        # incl. .cloak-profile/, .venv, __pycache__
  pyproject.toml                    # uvx entry: browser-relay-mcp; deps incl. playwright
  extension/
    manifest.json
    background.js                   # poll loop, batch dispatch, tab pool, search/fetch, relay escalation
    engines/{index,bing}.js         # SHARED SERP parsers + detectBlock (also used by cloak)
    extract.js                      # SHARED Readability invocation + innerText fallback
    lib/Readability.js              # Mozilla Readability (vendored; shared)
    popup.html/js/css, options.html/js, icons/
  server/
    browser_relay/
      __init__.py                   # __version__
      app.py                        # FastAPI relay: job router, queues, batch dispatch, action registry, resume
      drivers/
        __init__.py
        cloak.py                    # Playwright stealth persistent context: nav, inject shared JS, detect, headful surface, resume
      mcp_server/
        __init__.py
        server.py                   # FastMCP tools (driver param, resume) + backend spawn/lock/version/watchdog
    pyproject.toml, README.md
  adapters/storm.py                 # ~5-line adapter + chunk()
  tests/{unit,acceptance,fixtures}/ # fixtures: Bing SERP HTML, CAPTCHA page, sample article HTML
  chrome-store-assets/              # Web Store packaging (parity; lands after core works)
  docs/privacy-policy.md
  docs/superpowers/specs/2026-06-20-browser-relay-search-fetch-mcp-design.md   # this file
```

## 15. Testing strategy → acceptance-criteria map

### Unit (no live browser)
- **Relay queue logic:** caps, search spacing, batch dispatch, backpressure, timeout→504,
  action registry + resume + TTL — using a fake extension client (polls `/pending`, posts
  `/result` incl. an `action_required` signal).
- **SERP parser (shared JS):** parse saved Bing SERP fixture → ≥8 `{title,url,snippet}`; parse
  a CAPTCHA/challenge fixture → `detectBlock` true (asserts no silent-empty).
- **Extractor (shared JS):** Readability on a saved JS-heavy article fixture → `text` ≥1000
  chars, body prose, no nav/footer.
- **Adapter:** `to_storm` maps a sample dict → correct `{url,title,description,snippets[]}`;
  returns `[]` on non-ok status.

### Live acceptance (one per PRD criterion)
1. `search("consistent hashing", 10)` (relay) → ≥8 results.
2. `fetch(<JS-heavy article URL>)` → clean `text` ≥1000 chars (body, not chrome).
3. **50 sequential search+fetch (relay)** → 0 rate-limit failures, 0 silent-empty.
4. `health()` correctly reports connected vs disconnected per driver.
5. 5-line adapter turns MCP output into STORM shape.

### Driver-specific / escalation
- **cloak happy path:** launch persistent context; search + fetch return ok using shared JS.
- **escalation (both drivers):** force a challenge → assert `action_required` shape + window
  surfaced → simulate resolution → `resume` returns ok; assert TTL expiry → `error`.
- **cloak login persistence:** log in once headful; a later run reuses cookies (no re-login).

## 16. Distribution / packaging (parity)

- `pyproject.toml` console entry `browser-relay-mcp` → `browser_relay.mcp_server.server:main`;
  `playwright` dependency (+ `playwright install chromium`, or use the `chrome` channel).
- `uvx browser-relay-mcp` runs the MCP server; it auto-spawns/reuses the shared relay
  (file-lock + version-kill + watchdog, inherited).
- MCP client config:
  ```json
  { "mcpServers": { "browser-relay": { "command": "uvx", "args": ["browser-relay-mcp"] } } }
  ```
- Chrome extension: load-unpacked for dev; `chrome-store-assets/` + zip for Web Store (after
  the 50-burst acceptance test passes).
- `.cloak-profile/` (persistent stealth profile) git-ignored. The `cloakbrowser-setup` skill is
  consulted during implementation to finalize the exact stealth launch config.

## 17. Implementation phasing (within v1)

All phases ship in v1; this ordering validates the core premise before adding cloak complexity.
1. **Relay core** — FastAPI relay (queues, batch dispatch, backpressure), extension (search/
   fetch, shared JS, Bing parser, Readability), MCP tools (`search`/`fetch`/`search_and_fetch`/
   `health`, `driver=relay`). Gate: **acceptance #1–#5 pass on the relay driver** (esp. the
   50-burst #3).
2. **Escalation protocol** — action registry + `resume` + `action_required`; relay-driver
   block→escalate (focus tab). Gate: escalation tests pass on relay.
3. **Cloak driver** — Playwright stealth persistent context, shared-JS injection, `driver=cloak`
   on all tools, cloak escalation (bring-to-front) + login persistence, `health.cloak`.
4. **Packaging parity** — Web Store assets/zip, uvx/PyPI polish, popup/options engine selector
   + driver/status display, docs.

## 18. Open / deferred items
- `driver="auto"` (try relay, fall back to cloak on block) — deferred enhancement.
- Additional engines (`duckduckgo`, `google`) behind the same parser interface — Bing is the
  v1 default and burst-safety baseline.
- Exact cloak stealth mechanism (embedded Playwright + stealth hardening vs. reusing
  CloakBrowser's setup) finalized at implementation by reading the `cloakbrowser-setup` skill;
  the design treats cloak as an embedded, directly-driven stealth browser.
- Per-result fetch escalation inside `search_and_fetch` is intentionally *not* interactive in
  v1 (records `action_required` as a `fetch_error`; explicit `fetch(url)` triggers the handoff).
- Measured throughput numbers replace the §11 estimates once the relay runs live.
